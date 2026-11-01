#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Artem Pronchenko
# SPDX-License-Identifier: GPL-3.0-or-later
r"""
acgf_tool.py -- разбор файлов ``*.acgf`` (внутренний формат движка AGE).

Структура файла
---------------
1. ``*.acgf`` -- **не** картинка и не один сжатый блок, а *бандл*: файл
   состоит из последовательных блоков, каждый со своим заголовком.
   Блоки идут вплотную, без выравнивания и без общего оглавления.

2. Заголовки блоков (все поля -- little-endian ``u32``)::

       'ACGF'       ver(=2) 0 1080 1080 N        24 байта  - «лист»/страница
       00 00 00 00  ver(=1) 0 1080 1080 N        24 байта  - продолжение листа
       'ACIF'       ver(=2) 0 raw w h raw raw comp  36 байт  - картинка
       'SYS4472 '   ...                          вложенный контейнер
       'RIFF'       ...                          вложенный WAV

   У ``ACGF``/``00 00 00 00`` полей размера нет -- полезная нагрузка
   тянется до следующего заголовка. У ``ACIF`` размер **точный**::

       raw  = w * h
       конец блока = offset + 36 + comp

3. ``ACIF`` описывает **8-битное (1 байт на пиксель) изображение** ``w x h``,
   упакованное внутренним кодеком движка. Размеры произвольные, сжатие
   обычно 2–8 раз.

   Кроме ``ACIF`` внутри бандлов встречаются обычные, полностью извлекаемые
   данные: вложенные WAV и текстовые модели DirectX (``xof 0303txt``).

4. Внутренний кодек ``ACIF`` — **LZSS 4096/18/3** (кольцевое окно 4096 байт,
   начальная позиция записи ``0xFEE``, флаги байтом LSB-first с маркером
   ``0x100``, совпадение = 2 байта: позиция ``b0 | ((b1 & 0xF0) << 4)`` —
   абсолютная в окне, длина ``(b1 & 0x0F) + 3``). Параметры восстановлены
   по поведению движка; при ``comp == raw`` данные лежат без сжатия.
   Декодер — :func:`lzss_decompress`, изображение пишется в PNG по
   ``--decode``.

   После распаковки в байтах лежат значения ``0…255``: ``0`` — фон,
   ``255`` — фигура, между ними антиалиасинг, т.е. ``ACIF`` — 8-битная
   маска/альфа (UI-атласы и текст), **а не цветная картинка**.

5. **Цвет лежит в блоке ``page``.** Блок устроен так (запись движка
   «AGFBMP»)::

        +0    24 байта   заголовок листа ('ACGF', ver, 0, 1080, 1080, N)
        +24   1080 байт  запись AGFBMP: 2 | 14 | BITMAPINFOHEADER(40) | палитра
        +1104 12 байт    дескриптор потока пикселей: raw, raw, comp
        +1116 comp байт  LZSS-поток пикселей -> raw байт индексов

   Запись AGFBMP — это почти обычный BMP без магии ``BM``: смещение пикселей
   ``pixoff = 1078`` (2 + 14 + 40 + 1024), палитра — 256 записей **BGRA**.
   Пиксели — 8-битные индексы в палитру, строки идут **снизу вверх** и
   выровнены по 4 байта (``imgsz = stride * h``), как в BMP.

   Иногда запись AGFBMP сжата тем же LZSS вместе с заголовком: тогда с +24
   идёт поток, первые 1080 байт которого — та же запись, а сразу за ним
   (после его 12-байтного дескриптора) — поток пикселей. Оба варианта
   разбирает :func:`decode_page_image`.

6. Из блоков-продолжений листа (``00 00 00 00``) можно собрать остаток
   страницы, если продолжение есть в том же бандле.

Использование
-------------
    python acgf_tool.py --list etc\acgf                   # структура файлов
    python acgf_tool.py --list etc\acgf --csv blocks.csv  # + таблица в CSV
    python acgf_tool.py --check etc\acgf                  # проверка разбора
    python acgf_tool.py --extract etc\out_acgf etc\acgf   # извлечь нагрузки
    python acgf_tool.py --extract etc\png etc\acgf --png-only    # только PNG
    python acgf_tool.py --extract etc\png etc\acgf --png-only --color       # цвет
    python acgf_tool.py --extract etc\png etc\acgf --png-only --color --rgba
    python acgf_tool.py --verify-decode etc\acgf          # доля успешных декодов
    python acgf_tool.py --verify-page etc\acgf            # доля разобранных листов
    python acgf_tool.py --stats etc\acgf                  # сводка по каталогу

Зависимостей нет (только стандартная библиотека, Python 3.9+).
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# разбор блоков
# --------------------------------------------------------------------------

PAGE_SIZE = 1080                 # размер «листа» во всех образцах
PAGE_HDR = 24                    # байт в заголовке ACGF / продолжения
ACIF_HDR = 36                    # байт в заголовке ACIF
AGFBMP_HDR = 1080                # 2 + 14 + BITMAPINFOHEADER(40) + палитра 256x4
DIB_MAGIC = b'\x28\x00\x00\x00'  # размер BITMAPINFOHEADER
ZERO_MAGIC = b'\x00\x00\x00\x00'
SYSFS_MAGIC = b'SYS4472 '


@dataclass
class Block:
    """Один блок внутри ``*.acgf``."""

    kind: str                    # 'page' | 'page_v1' | 'acif' | 'sys4472' | 'riff'
    offset: int                  # смещение заголовка
    size: int                    # полный размер блока вместе с заголовком
    hdr: dict = field(default_factory=dict)
    exact: bool = False          # True, если размер взят из поля, а не по цепочке
    where: str = ''              # 'header' | 'chain' | 'eof'

    @property
    def hdr_len(self) -> int:
        """Сколько байт заголовка отрезать при извлечении полезной нагрузки."""
        if self.kind == 'acif':
            return ACIF_HDR
        if self.kind in ('page', 'page_v1', 'sys4472'):
            return PAGE_HDR
        return 0                 # riff / xmodel / unknown извлекаются целиком

    @property
    def payload_offset(self) -> int:
        return self.offset + self.hdr_len

    @property
    def payload_size(self) -> int:
        return self.size - self.hdr_len


def _page_header(raw: bytes, pos: int) -> dict | None:
    """Заголовок ``ACGF`` (ver 2) или продолжения (magic = 0, ver 1)."""
    if pos + PAGE_HDR > len(raw):
        return None
    magic = raw[pos:pos + 4]
    if magic != b'ACGF' and magic != ZERO_MAGIC:
        return None
    ver, zero, w, h, n = struct.unpack_from('<5I', raw, pos + 4)
    if zero != 0 or w != PAGE_SIZE or h != PAGE_SIZE or not 1 <= n <= PAGE_SIZE:
        return None
    if magic == b'ACGF' and ver != 2:
        return None
    if magic == ZERO_MAGIC and ver != 1:
        return None
    return {'magic': magic, 'ver': ver, 'w': w, 'h': h, 'n': n}


def _acif_header(raw: bytes, pos: int) -> dict | None:
    """Заголовок ``ACIF``: 8-битное изображение w x h, сжатое в ``comp`` байт."""
    if pos + ACIF_HDR > len(raw) or raw[pos:pos + 4] != b'ACIF':
        return None
    ver, zero, raw_sz, w, h, r2, r3, comp = struct.unpack_from('<8I', raw, pos + 4)
    if ver != 2 or zero != 0 or not w or not h:
        return None
    if r2 != raw_sz or r3 != raw_sz:
        return None
    if comp > len(raw) - pos - ACIF_HDR:      # нагрузка за пределами файла
        return None
    return {'ver': ver, 'raw': raw_sz, 'w': w, 'h': h, 'comp': comp,
            'bits_per_px': round(comp * 8 / max(1, raw_sz), 3)}


def _riff_header(raw: bytes, pos: int) -> dict | None:
    """Вложенный RIFF (WAV/XWMA): размер берётся из поля ``size``."""
    if pos + 12 > len(raw) or raw[pos:pos + 4] != b'RIFF':
        return None
    size, form = struct.unpack_from('<I4s', raw, pos + 4)
    if not 4 <= size <= len(raw) - pos - 8:
        return None
    return {'form': form.decode('latin-1', 'replace'), 'size': size}


def probe(raw: bytes, pos: int) -> Block | None:
    """Распознать блок по смещению ``pos`` (строгая проверка инвариантов)."""
    h = _acif_header(raw, pos)
    if h:
        return Block('acif', pos, ACIF_HDR + h['comp'], h, exact=True, where='header')
    h = _page_header(raw, pos)
    if h:
        kind = 'page' if h['magic'] == b'ACGF' else 'page_v1'
        return Block(kind, pos, PAGE_HDR, h)
    if raw[pos:pos + 8] == SYSFS_MAGIC and pos + 40 <= len(raw):
        words = struct.unpack_from('<8I', raw, pos + 8)
        if words[6] == 0x1C and 1 <= words[3] <= 64 and words[5] <= 64:
            return Block('sys4472', pos, PAGE_HDR, {'words': list(words)})
    h = _riff_header(raw, pos)
    if h:
        return Block('riff', pos, 8 + h['size'], h, exact=True, where='header')
    # текстовая модель DirectX (.x) -- встречается внутри бандлов (18 из 3641 файла
    # содержат её; поле размера у текстового варианта отсутствует)
    if raw[pos:pos + 8] in (b'xof 0303', b'xof 0302'):
        return Block('xmodel', pos, 0, {'form': 'text'})
    return None


def find_next_header(raw: bytes, start: int) -> int | None:
    """Найти следующий валидный заголовок после ``start``."""
    best = None
    for magic in (b'ACGF', b'ACIF', b'RIFF', SYSFS_MAGIC):
        i = raw.find(magic, start)
        while i >= 0:
            if probe(raw, i) is not None:
                if best is None or i < best:
                    best = i
                break
            i = raw.find(magic, i + 1)
    sig = struct.pack('<II', PAGE_SIZE, PAGE_SIZE)      # продолжения листа
    i = raw.find(sig, start)
    while i >= 0:
        cand = i - 12
        if cand >= start and _page_header(raw, cand) is not None:
            if best is None or cand < best:
                best = cand
            break
        i = raw.find(sig, i + 1)
    return best


def walk(raw: bytes) -> tuple[list[Block], int | None]:
    """Разобрать бандл целиком. Возвращает (блоки, смещение остановки).

    Неопознанные участки попадают в список как блоки ``unknown`` (карвинг),
    поэтому сумма размеров всегда равна размеру файла.
    """
    blocks: list[Block] = []
    pos = 0
    while pos < len(raw):
        blk = probe(raw, pos)
        if blk is None:
            nxt = find_next_header(raw, pos + 1)
            end = nxt if nxt is not None else len(raw)
            blocks.append(Block('unknown', pos, end - pos, {}, where='gap'))
            pos = end
            continue
        if blk.exact:
            pos += blk.size
        else:
            nxt = find_next_header(raw, pos + blk.hdr_len)
            end = nxt if nxt is not None else len(raw)
            blk.size = end - pos
            blk.where = 'chain' if nxt is not None else 'eof'
            pos = end
        blocks.append(blk)
    return blocks, None


# --------------------------------------------------------------------------
# запись PNG без сторонних библиотек
# --------------------------------------------------------------------------

def write_png_gray(path: str, width: int, data: bytes) -> None:
    """Записать 8-битный grayscale PNG (width x ceil(n/width)) без зависимостей."""
    if width <= 0:
        raise ValueError('width must be positive')
    height = max(1, (len(data) + width - 1) // width)
    _write_png(path, width, height, 0, 1, data)


def write_png_rgb(path: str, width: int, height: int, rgb: bytes) -> None:
    """Записать 8-битный RGB PNG (color type 2)."""
    _write_png(path, width, height, 2, 3, rgb)


def write_png_rgba(path: str, width: int, height: int, rgba: bytes) -> None:
    """Записать 8-битный RGBA PNG (color type 6)."""
    _write_png(path, width, height, 6, 4, rgba)


def _write_png(path: str, width: int, height: int, color_type: int,
               channels: int, data: bytes) -> None:
    """Общая запись PNG: 8 бит на канал, без интерлейса."""
    stride = width * channels
    data = data.ljust(stride * height, b'\x00')
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                  # фильтр None
        raw += data[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack('>I', len(body)) + tag + body +
                struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(chunk(b'IHDR', ihdr))
        f.write(chunk(b'IDAT', zlib.compress(bytes(raw), 9)))
        f.write(chunk(b'IEND', b''))


def _lzss(src: bytes, raw_size: int, off: int = 0) -> tuple[bytes, int]:
    """Ядро LZSS: распаковать из ``src[off:]`` до ``raw_size`` байт.

    Возвращает ``(данные, сколько_байт_входа_израсходовано)`` — второе нужно,
    потому что лист `page` состоит из **двух** потоков подряд: сначала
    заголовок+палитра (1080 байт), затем пиксели.
    """
    N = 4096
    F = 18
    THRESHOLD = 3
    win = bytearray(N)
    r = N - F
    out = bytearray()
    flags = 0
    i = off
    n = len(src)
    while len(out) < raw_size:
        flags >>= 1
        if not flags & 0x100:
            if i >= n:
                break
            flags = src[i] | 0xFF00
            i += 1
        if flags & 1:
            if i >= n:
                break
            c = src[i]
            i += 1
            out.append(c)
            win[r] = c
            r = (r + 1) & 0xFFF
        else:
            if i + 1 >= n:
                break
            b0 = src[i]
            b1 = src[i + 1]
            i += 2
            pos = b0 | ((b1 & 0xF0) << 4)
            length = (b1 & 0x0F) + THRESHOLD
            for k in range(length):
                c = win[(pos + k) & 0xFFF]
                out.append(c)
                win[r] = c
                r = (r + 1) & 0xFFF
                if len(out) >= raw_size:
                    break
    return bytes(out[:raw_size]), i


def lzss_decompress(src: bytes, raw_size: int) -> bytes:
    """LZSS движка AGE: N=4096, F=18, THRESHOLD=3 (окно с абсолютными позициями).

    Параметры восстановлены по поведению движка на реальных данных:

    * кольцевое окно 4096 байт, начальная позиция записи ``r = N - F = 0xFEE``;
    * флаги читаются байтом, биты используются от младшего (LSB-first),
      маркер «байт уже прочитан» -- установленный бит ``0x100``;
    * бит 1 -- литерал (байт пишется в выход и в окно);
    * бит 0 -- совпадение из двух байт ``b0``, ``b1``:

      - позиция в окне: ``pos = b0 | ((b1 & 0xF0) << 4)`` (0..4095, **абсолютная**,
        а не расстояние назад -- из-за этого не подходили классические схемы);
      - длина: ``(b1 & 0x0F) + 3`` (3..18);

    * если в заголовке упакованного блока ``comp == raw``, данные лежат как есть
      (в C-коде это отдельная ветка с ``memcpy``).
    """
    return _lzss(src, raw_size)[0]


def decode_image(payload: bytes, w: int, h: int) -> bytes:
    """Распаковать payload ACIF в буфер ``w*h`` (по одному байту на пиксель).

    Внутри -- LZSS движка (см. :func:`lzss_decompress`). Значения байтов
    в образцах: 0 (фон), 255 (фигура) и промежуточные уровни антиалиасинга.
    То есть это маска/альфа, а не цветная картинка.
    """
    return lzss_decompress(payload, w * h)


def _page_header_record(blk_body: bytes):
    """Вытащить запись AGFBMP из тела блока ``page``.

    Возвращает ``(header_1080, stream, raw_size)`` или ``None``.
    Запись бывает (а) в открытом виде — тогда ``BITMAPINFOHEADER`` виден прямо
    по смещению 16 от тела блока, и (б) сжатой вместе с заголовком: первые
    1080 байт потока и есть запись.
    """
    if len(blk_body) < 32:
        return None
    if blk_body[16:20] == DIB_MAGIC and len(blk_body) >= AGFBMP_HDR + 12:
        hdr, pos = blk_body[:AGFBMP_HDR], AGFBMP_HDR
    else:
        hdr, used = _lzss(blk_body, AGFBMP_HDR)
        if len(hdr) < AGFBMP_HDR or hdr[16:20] != DIB_MAGIC:
            return None
        pos = used
    if pos + 12 > len(blk_body):
        return None
    raw_sz, raw_sz2, comp = struct.unpack_from('<III', blk_body, pos)
    if raw_sz != raw_sz2 or not comp or pos + 12 + comp > len(blk_body):
        return None
    return hdr, blk_body[pos + 12:pos + 12 + comp], raw_sz


def _paletted_to_rgb(w: int, h: int, idx: bytes, palette: bytes, stride: int = 0) -> bytes:
    """Индексы (снизу вверх, строки по ``stride``) + палитра BGRA -> RGB.

    Пиксели в AGFBMP лежат как в BMP: строки снизу вверх и выровнены по
    4 байта (``stride``). На выходе — нормальное изображение сверху вниз.
    """
    stride = max(stride or w, w)
    need = stride * h
    if len(idx) < need:
        idx = idx.ljust(need, b'\x00')
    lut = [palette[v * 4 + 2:v * 4 + 3] + palette[v * 4 + 1:v * 4 + 2] +
           palette[v * 4:v * 4 + 1] for v in range(256)]
    rows = []
    for y in range(h):
        src = (h - 1 - y) * stride                     # строки идут снизу вверх
        rows.append(b''.join([lut[v] for v in idx[src:src + w]]))
    return b''.join(rows)


def _bgr_to_rgb(w: int, h: int, data: bytes, stride: int, bpp: int = 24) -> bytes:
    """Прямой цвет (BGR/BGRA, строки снизу вверх) -> RGB сверху вниз."""
    step = bpp // 8
    need = stride * h
    if len(data) < need:
        data = data.ljust(need, b'\x00')
    rows = []
    for y in range(h):
        src = (h - 1 - y) * stride
        row = data[src:src + w * step]
        rgb = bytearray(w * 3)
        rgb[0::3] = row[2::step]
        rgb[1::3] = row[1::step]
        rgb[2::3] = row[0::step]
        rows.append(bytes(rgb))
    return b''.join(rows)


def decode_page_image(raw: bytes, blk: 'Block'):
    """Цветная картинка из блока ``page``: ``(w, h, rgb)`` или ``None``.

    Структура блока и обе раскладки записи AGFBMP описаны в шапке модуля
    (пункт 5). Размеры берутся из самого блока, а не
    из `ACIF`, поэтому функция не зависит от соседних блоков.

    Поддерживаются 8-битные картинки с палитрой (основной вариант) и 24/32-
    битные без палитры (несколько файлов в наборе).
    """
    body = raw[blk.offset + PAGE_HDR:blk.offset + blk.size]
    parts = _page_header_record(body)
    if parts is None:
        return None
    hdr, stream, raw_sz = parts
    hsz, w, h, planes, bpp, comp, imgsz = struct.unpack_from('<IiiHHII', hdr, 16)
    if hsz != 40 or not 0 < w <= 8192 or not 0 < h <= 8192 or planes != 1:
        return None
    if bpp == 8:
        stride = max(imgsz // h if h else w, w)
        need = stride * h
    elif bpp in (24, 32):
        step = bpp // 8
        stride = (w * step + 3) & ~3
        need = stride * h
    else:
        return None
    if raw_sz < w * h or need > 64 << 20:
        return None
    pixels = lzss_decompress(stream, raw_sz)
    if not pixels:
        return None
    if bpp == 8:
        return w, h, _paletted_to_rgb(w, h, pixels, hdr[56:56 + 1024], stride)
    return w, h, _bgr_to_rgb(w, h, pixels, stride, bpp)


# --------------------------------------------------------------------------
# отчёты
# --------------------------------------------------------------------------

def fmt_size(n: int) -> str:
    if n < 1024:
        return '%d B' % n
    if n < 1024 ** 2:
        return '%.1f KB' % (n / 1024.0)
    if n < 1024 ** 3:
        return '%.1f MB' % (n / 1024.0 ** 2)
    return '%.2f GB' % (n / 1024.0 ** 3)


def describe(blk: Block) -> str:
    """Одна строка описания блока."""
    head = '@%#010x  %-8s ' % (blk.offset, blk.kind)
    if blk.kind in ('page', 'page_v1'):
        return head + 'ver=%d  %dx%d  n=%-4d payload=%-9s (%s)' % (
            blk.hdr['ver'], blk.hdr['w'], blk.hdr['h'], blk.hdr['n'],
            fmt_size(blk.payload_size), blk.where)
    if blk.kind == 'acif':
        h = blk.hdr
        return head + 'ver=%d  %dx%d  raw=%-8d comp=%-8d %.2fx %.3f bit/px [точно]' % (
            h['ver'], h['w'], h['h'], h['raw'], h['comp'],
            h['raw'] / max(1, h['comp']), h['bits_per_px'])
    if blk.kind == 'sys4472':
        w = blk.hdr['words']
        return head + 'kind=%-4d sub=%-3d payload=%-9s (%s)' % (
            w[0], w[2], fmt_size(blk.payload_size), blk.where)
    if blk.kind == 'riff':
        return head + 'form=%s  size=%-9d [точно]' % (blk.hdr['form'], blk.size)
    if blk.kind == 'xmodel':
        return head + 'формат=%s  payload=%-9s (модель DirectX .x)' % (
            blk.hdr.get('form', '?'), fmt_size(blk.payload_size if blk.size else 0))
    if blk.kind == 'unknown':
        return head + 'payload=%-9s (не опознан)' % fmt_size(blk.size)
    return head


def iter_files(paths: list[str]) -> list[str]:
    """Развернуть аргументы CLI в список файлов ``*.acgf``."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith('.acgf'):
                    out.append(os.path.join(p, name))
        else:
            out.append(p)
    return out


def cmd_list(paths: list[str], csv_path: str | None) -> int:
    rows = []
    for path in iter_files(paths):
        raw = open(path, 'rb').read()
        blocks, stop = walk(raw)
        print('%s  %s  блоков: %d' % (os.path.basename(path), fmt_size(len(raw)), len(blocks)))
        for blk in blocks:
            print('   ' + describe(blk))
            rows.append({'file': os.path.basename(path), 'kind': blk.kind,
                         'offset': blk.offset, 'size': blk.size,
                         'payload': blk.payload_size,
                         'w': blk.hdr.get('w', ''), 'h': blk.hdr.get('h', ''),
                         'n': blk.hdr.get('n', ''), 'comp': blk.hdr.get('comp', ''),
                         'exact': int(blk.exact)})
        if stop is not None:
            print('   !! разбор остановлен на %#x (осталось %d байт)' %
                  (stop, len(raw) - stop))
        print()
    if csv_path:
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['file', 'kind', 'offset', 'size',
                                              'payload', 'w', 'h', 'n', 'comp', 'exact'],
                               delimiter=';')
            w.writeheader()
            w.writerows(rows)
        print('CSV: %s (%d строк)' % (csv_path, len(rows)))
    return 0


def cmd_check(paths: list[str]) -> int:
    """Проверка: блоки должны покрывать файл без зазоров."""
    bad = 0
    kinds = collections.Counter()
    unknown_bytes = 0
    files = iter_files(paths)
    for path in files:
        raw = open(path, 'rb').read()
        blocks, stop = walk(raw)
        covered = sum(b.size for b in blocks)
        for b in blocks:
            kinds[b.kind] += 1
            if b.kind == 'unknown':
                unknown_bytes += b.size
        if stop is not None or covered != len(raw):
            bad += 1
            print('%s: покрыто %d из %d, остановка на %s' %
                  (os.path.basename(path), covered, len(raw),
                   '%#x' % stop if stop is not None else '-'))
    print('\nфайлов: %d, покрытие 100%%: %d, с потерями: %d' %
          (len(files), len(files) - bad, bad))
    print('блоки:', dict(kinds))
    print('неопознанных байт: %d (%.4f%% всех данных)' %
          (unknown_bytes, 100.0 * unknown_bytes /
           max(1, sum(os.path.getsize(p) for p in files))))
    return 0 if bad == 0 else 1


def cmd_stats(paths: list[str]) -> int:
    kinds = collections.Counter()
    dims = collections.Counter()
    ratios = []
    bits = []
    files = iter_files(paths)
    for path in files:
        raw = open(path, 'rb').read()
        blocks, _stop = walk(raw)
        for b in blocks:
            kinds[b.kind] += 1
            if b.kind == 'acif':
                dims[(b.hdr['w'], b.hdr['h'])] += 1
                ratios.append(b.hdr['raw'] / max(1, b.hdr['comp']))
                bits.append(b.hdr['bits_per_px'])
    print('файлов: %d' % len(files))
    print('блоки:', dict(kinds))
    if ratios:
        ratios.sort()
        print('ACIF: %d шт., сжатие %.2f ... %.2f x (медиана %.2f), %.2f ... %.2f bit/px' %
              (len(ratios), ratios[0], ratios[-1], ratios[len(ratios) // 2],
               min(bits), max(bits)))
        print('частые размеры ACIF:')
        for (w, h), n in dims.most_common(12):
            print('   %4dx%-4d  %d' % (w, h, n))
    return 0


def cmd_extract(outdir: str, paths: list[str], png_width: int, make_png: bool,
                decode: bool = False, png_only: bool = False,
                rgba: bool = False, color: bool = False) -> int:
    if png_only:
        return extract_png_only(outdir, paths, rgba, color)

    os.makedirs(outdir, exist_ok=True)
    total = 0
    decoded = 0
    for path in iter_files(paths):
        raw = open(path, 'rb').read()
        blocks, _stop = walk(raw)
        stem = os.path.splitext(os.path.basename(path))[0]
        target = os.path.join(outdir, stem)
        os.makedirs(target, exist_ok=True)
        for idx, blk in enumerate(blocks):
            payload = raw[blk.payload_offset:blk.payload_offset + blk.payload_size]
            if not payload:
                continue
            ext = 'bin'
            if blk.kind == 'acif':
                name = '%02d_acif_%dx%d' % (idx, blk.hdr['w'], blk.hdr['h'])
            elif blk.kind == 'riff':
                name = '%02d_riff' % idx
                ext = 'wav'
            elif blk.kind == 'xmodel':
                name = '%02d_xmodel' % idx
                ext = 'x'
            elif blk.kind == 'unknown':
                name = '%02d_unknown_%08x' % (idx, blk.offset)
            else:
                name = '%02d_%s' % (idx, blk.kind)
            with open(os.path.join(target, '%s.%s' % (name, ext)), 'wb') as f:
                f.write(payload)
            total += 1
            if make_png and blk.kind in ('acif', 'page', 'page_v1', 'sys4472'):
                write_png_gray(os.path.join(target, name + '_payload.png'),
                               png_width, payload)
            if decode and blk.kind == 'acif':
                pixels = decode_acif_block(payload, blk.hdr['w'], blk.hdr['h'])
                if pixels is not None:
                    write_png_gray(os.path.join(target, name + '_decoded.png'),
                                   blk.hdr['w'], pixels)
                    decoded += 1
    print('извлечено блоков: %d -> %s' % (total, outdir))
    if decode:
        print('декодировано ACIF: %d' % decoded)
    print('внимание: *.bin -- полезная нагрузка внутреннего кодека, '
          'а *_payload.png -- «сырой» вид этих байт, а не декодированное изображение.')
    return 0


def extract_png_only(outdir: str, paths: list[str], rgba: bool = False,
                     color: bool = False) -> int:
    """«Плоский» режим: в каталоге только PNG с картинками.

    По умолчанию пишется **проверенная маска `ACIF`** (8-битный серый) —
    она декодируется на 100 % файлов.

    С ``color=True`` цвет берётся из блока `page`: палитра 256 x BGRA и
    8-битные индексы записи AGFBMP (см. :func:`decode_page_image`), строки
    идут снизу вверх и выровнены по 4 байта. При ``rgba=True`` к цвету
    добавляется альфа из `ACIF`.
    """
    os.makedirs(outdir, exist_ok=True)
    used: set[str] = set()
    written = color_count = mask_count = failed = 0
    files = iter_files(paths)
    for path in files:
        raw = open(path, 'rb').read()
        blocks, _stop = walk(raw)
        stem = os.path.splitext(os.path.basename(path))[0]
        pages = [b for b in blocks if b.kind in ('page', 'page_v1')]
        acifs = [b for b in blocks if b.kind == 'acif']
        count = max(len(pages), len(acifs), 1)
        for n in range(count):
            image = None
            suffix = '' if count == 1 else '_%d' % n
            page = pages[n] if n < len(pages) else None
            acif = acifs[n] if n < len(acifs) else None
            ew, eh = (acif.hdr['w'], acif.hdr['h']) if acif is not None else (0, 0)
            if color and page is not None:
                got = decode_page_image(raw, page)
                if got:
                    w, h, rgb = got
                    # альфа — из ACIF с такими же размерами (он не всегда
                    # стоит в файле рядом с этим листом, поэтому ищем по размеру)
                    alpha_blk = None
                    if rgba:
                        if acif is not None and (ew, eh) == (w, h):
                            alpha_blk = acif
                        else:
                            alpha_blk = next((a for a in acifs
                                              if (a.hdr['w'], a.hdr['h']) == (w, h)), None)
                    if alpha_blk is not None:
                        apayload = raw[alpha_blk.payload_offset:
                                       alpha_blk.payload_offset + alpha_blk.payload_size]
                        alpha = decode_image(apayload, w, h)
                        buf = bytearray(w * h * 4)
                        for i in range(w * h):
                            buf[i * 4:i * 4 + 3] = rgb[i * 3:i * 3 + 3]
                            buf[i * 4 + 3] = alpha[i]
                        image = ('rgba', w, h, bytes(buf))
                    else:
                        image = ('rgb', w, h, rgb)
            if image is None and acif is not None:
                apayload = raw[acif.payload_offset:acif.payload_offset + acif.payload_size]
                w, h = acif.hdr['w'], acif.hdr['h']
                mask = decode_acif_block(apayload, w, h)
                if mask is not None:
                    image = ('gray', w, h, mask)
            if image is None:
                failed += 1
                continue
            name = stem + suffix
            base, k = name, 1
            while name in used:                     # на случай совпадения имён
                k += 1
                name = '%s_%d' % (base, k)
            used.add(name)
            kind, w, h, buf = image
            target = os.path.join(outdir, name + '.png')
            if kind == 'rgba':
                write_png_rgba(target, w, h, buf)
                color_count += 1
            elif kind == 'rgb':
                write_png_rgb(target, w, h, buf)
                color_count += 1
            else:
                write_png_gray(target, w, buf)
                mask_count += 1
            written += 1
    print('файлов на входе: %d; PNG записано: %d -> %s' % (len(files), written, outdir))
    print('  цветных: %d, масок (серых): %d' % (color_count, mask_count))
    if failed:
        print('  не удалось декодировать: %d' % failed)
    return 0 if written else 1


def decode_acif_block(payload: bytes, w: int, h: int) -> bytes | None:
    """Обёртка: вернуть ``w*h`` уровней или ``None``, если декодер недоступен."""
    try:
        pixels = decode_image(payload, w, h)
    except NotImplementedError:
        return None
    if pixels is None or len(pixels) != w * h:
        return None
    return pixels


def cmd_verify_decode(paths: list[str]) -> int:
    """Прогнать все ACIF и посчитать, сколько декодируется ровно в w*h байт."""
    ok = bad = skipped = 0
    files = iter_files(paths)
    for path in files:
        raw = open(path, 'rb').read()
        blocks, _stop = walk(raw)
        for blk in blocks:
            if blk.kind != 'acif':
                continue
            payload = raw[blk.payload_offset:blk.payload_offset + blk.payload_size]
            pixels = decode_acif_block(payload, blk.hdr['w'], blk.hdr['h'])
            if pixels is None:
                skipped += 1
            else:
                ok += 1
    total = ok + skipped + bad
    print('ACIF: %d; декодировано: %d; не декодировано: %d' % (total, ok, skipped))
    if skipped == total:
        print('декодер не реализован (см. описание формата в шапке модуля)')
    else:
        print('доля успеха: %.1f%%' % (100.0 * ok / max(1, total)))
    return 0 if ok else 1


def cmd_verify_page(paths: list[str]) -> int:
    """Прогнать все блоки ``page``: сколько цветных картинок разбирается.

    Попутно сверяются размеры с соседним `ACIF`: в норме они совпадают
    (лист — это цветная версия той же картинки, `ACIF` — её альфа).
    """
    ok = failed = 0
    same_dims = dims_diff = 0
    stored = packed = 0
    files = iter_files(paths)
    for path in files:
        raw = open(path, 'rb').read()
        blocks, _stop = walk(raw)
        acifs = [b.hdr for b in blocks if b.kind == 'acif']
        n_page = 0
        for blk in blocks:
            if blk.kind not in ('page', 'page_v1'):
                continue
            body = raw[blk.offset + PAGE_HDR:blk.offset + blk.size]
            if body[16:20] == DIB_MAGIC:
                stored += 1
            else:
                packed += 1
            got = decode_page_image(raw, blk)
            if got is None:
                failed += 1
                continue
            ok += 1
            w, h, _rgb = got
            if n_page < len(acifs):
                if (acifs[n_page]['w'], acifs[n_page]['h']) == (w, h):
                    same_dims += 1
                else:
                    dims_diff += 1
            n_page += 1
    total = ok + failed
    print('page: %d; декодировано: %d; не декодировано: %d' % (total, ok, failed))
    print('  заголовок AGFBMP в открытом виде: %d, сжат вместе с картинкой: %d'
          % (stored, packed))
    print('  размеры совпали с ACIF: %d, разошлись: %d' % (same_dims, dims_diff))
    print('  доля успеха: %.1f%%' % (100.0 * ok / max(1, total)))
    return 0 if ok and not failed else (0 if ok else 1)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description='Разбор *.acgf: бандл из листов ACGF, картинок ACIF и вложенных ресурсов')
    ap.add_argument('paths', nargs='+', help='файлы *.acgf или каталоги с ними')
    ap.add_argument('--list', action='store_true', help='показать структуру блоков')
    ap.add_argument('--check', action='store_true', help='проверить покрытие файла блоками')
    ap.add_argument('--stats', action='store_true', help='сводка по набору файлов')
    ap.add_argument('--extract', metavar='OUTDIR', help='извлечь полезные нагрузки блоков')
    ap.add_argument('--csv', metavar='FILE', help='выгрузить таблицу блоков в CSV')
    ap.add_argument('--payload-png-width', type=int, default=256,
                    help='ширина «сырого» PNG-предпросмотра (по умолчанию 256)')
    ap.add_argument('--no-png', action='store_true', help='не писать PNG при --extract')
    ap.add_argument('--decode', action='store_true',
                    help='при --extract дополнительно писать декодированные ACIF в PNG')
    ap.add_argument('--png-only', action='store_true',
                    help='при --extract писать ТОЛЬКО PNG в одну папку '
                         '(по умолчанию — проверенная маска ACIF)')
    ap.add_argument('--color', action='store_true',
                    help='при --png-only брать цвет из блока page (палитра AGFBMP)')
    ap.add_argument('--rgba', action='store_true',
                    help='с --color: добавить альфу из ACIF (RGBA вместо RGB)')
    ap.add_argument('--verify-decode', action='store_true',
                    help='проверить декодер на наборе: доля ACIF, распакованных ровно в w*h')
    ap.add_argument('--verify-page', action='store_true',
                    help='проверить разбор листов page: доля цветных картинок и сверка '
                         'размеров с ACIF')
    args = ap.parse_args(argv)

    if args.png_only and not args.extract:
        ap.error('--png-only требует --extract OUTDIR (например: '
                 '--extract png acgf --png-only)')

    if not (args.list or args.check or args.stats or args.extract
            or args.verify_decode or args.verify_page):
        args.list = True
    if args.list:
        cmd_list(args.paths, args.csv)
    if args.check:
        cmd_check(args.paths)
    if args.stats:
        cmd_stats(args.paths)
    if args.verify_decode:
        return cmd_verify_decode(args.paths)
    if args.verify_page:
        return cmd_verify_page(args.paths)
    if args.extract:
        cmd_extract(args.extract, args.paths, args.payload_png_width,
                    not args.no_png, args.decode or args.png_only, args.png_only,
                    args.rgba, args.color)
    return 0


if __name__ == '__main__':
    sys.exit(main())
