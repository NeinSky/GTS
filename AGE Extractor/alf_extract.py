#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
alf_extract.py -- извлечение данных из томов ``*.ALF`` (AGE Library File).

Структура тома
--------------
1. Том ``DATA<N>.ALF`` -- это **последовательная конкатенация файлов**.
   Общего заголовка, оглавления и выравнивания нет: полезная нагрузка одного
   объекта заканчивается ровно там, где начинается следующий. Проверяется
   командой ``--list``: покрытие тома по типам и отчёт о «зазорах».

2. Найденные форматы и точность определения размера:

   ===============  =========================  ==========================
   тег / формат     что это                    как определяется размер
   ===============  =========================  ==========================
   ``RIFF``         WAV / XWMA                 поле размера RIFF +
                                                разбор чанков ``fmt``/``data``
   ``\0\1\0\0``,    TrueType / OpenType / TTC   директория таблиц sfnt,
   ``OTTO``,                                    точный
   ``true``,
   ``ttcf``
   ``000001BA``     MPEG-1 program stream      поток начинается после
                                               ``000001B9`` (end of
                                               sequence) -- точно
   ``ACGF``         ресурс движка (лист        до следующего ``ACGF``
                    1080x1080, страницы)       (цепочка, эвристика)
   ``SYS4472 ``     контейнер движка, внутри    до следующего ``SYS4472 ``
                    которого лежат ACGF, WAV,  (цепочка, эвристика)
                    шрифты
   ``\x89PNG``,     PNG / JPEG / GIF / BMP /    по структуре файла, точный
   ``\xFF\xD8\xFF``, DDS                      (встречаются редко)
   GIF8, BM, DDS
   ===============  =========================  ==========================

3. Тома бывают трёх видов: контейнеры ``SYS4472`` с вложенными ресурсами
   (внутри — ``ACGF``, WAV, шрифты), цепочка ``ACGF``-бандлов и «плоский»
   поток самодостаточных файлов (WAV/XWMA, MPEG-PS).

4. Инструмент работает как *карвер*: один проход по тому, поиск сигнатур,
   строгая валидация кандидатов (проверка инвариантов заголовков), вырезание
   объектов в отдельные файлы + манифест CSV и отчёт о тех областях, которые
   распознать не удалось.

Использование
-------------
    python alf_extract.py --list etc\example                  # только инвентаризация
    python alf_extract.py etc\example --out etc\out               # извлечь всё
    python alf_extract.py etc\example --out etc\out --types wav,ttf
    python alf_extract.py etc\example --out etc\out --top-level    # без вложенных
    python alf_extract.py etc\example --out etc\out --no-blobs     # без ACGF/SYS4472

Зависимостей нет (только стандартная библиотека, Python 3.9+).
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# общие помощники
# --------------------------------------------------------------------------

CHUNK = 32 << 20          # размер окна чтения при сканировании
COPY_CHUNK = 4 << 20      # размер блока при копировании данных
MAX_JPEG = 512 << 20      # предохранитель для поиска EOI
MAX_PNG_CHUNK = 64 << 20  # предохранитель для чанков PNG


class Source:
    """Файл-том с маленьким кешем: валидаторы читают данные через ``at()``."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.size = os.path.getsize(path)
        self._fp = open(path, 'rb')      # для произвольного доступа (валидаторы)
        self._seq = open(path, 'rb')     # для последовательного сканирования
        self._buf = b''
        self._base = 0

    def close(self) -> None:
        self._fp.close()
        self._seq.close()

    def __enter__(self) -> 'Source':
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def set_buffer(self, data: bytes, base: int) -> None:
        """Кеш текущего окна сканирования (позволяет не дёргать диск)."""
        self._buf = data
        self._base = base

    def at(self, pos: int, n: int) -> bytes:
        """Прочитать ``n`` байт по смещению ``pos`` (короткое чтение допустимо)."""
        if pos < 0 or n <= 0 or pos >= self.size:
            return b''
        if n > self.size - pos:
            n = self.size - pos
        buf, base = self._buf, self._base
        if base <= pos and pos + n <= base + len(buf):
            off = pos - base
            return buf[off:off + n]
        self._fp.seek(pos)
        return self._fp.read(n)

    def copy_to(self, pos: int, size: int, dst: str, hasher=None) -> int:
        """Скопировать диапазон ``[pos, pos+size)`` в файл ``dst``."""
        written = 0
        self._fp.seek(pos)
        with open(dst, 'wb') as out:
            while written < size:
                block = self._fp.read(min(COPY_CHUNK, size - written))
                if not block:
                    break
                out.write(block)
                if hasher is not None:
                    hasher.update(block)
                written += len(block)
        return written


def u16(data: bytes, off: int, endian: str = '<') -> int:
    return struct.unpack_from(endian + 'H', data, off)[0]


def u32(data: bytes, off: int, endian: str = '<') -> int:
    return struct.unpack_from(endian + 'I', data, off)[0]


def is_printable_tag(tag: bytes) -> bool:
    return len(tag) == 4 and all(0x20 <= c <= 0x7E for c in tag)


def human(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0
    return str(n)


# --------------------------------------------------------------------------
# валидаторы («пробы»): каждый возвращает None либо словарь
#   {'type': ..., 'ext': ..., 'size': ..., 'meta': {...}}
# --------------------------------------------------------------------------

RIFF_FORMS = {b'WAVE', b'XWMA', b'AVI ', b'WEBP', b'ACON', b'RMID', b'CDXA',
              b'PAL ', b'RDIB'}
WAVE_FORMATS = {0x0001: 'pcm', 0x0002: 'ms-adpcm', 0x0003: 'ieee-float',
                0x0006: 'a-law', 0x0007: 'mu-law', 0x0011: 'ima-adpcm',
                0x0031: 'gsm', 0x0050: 'mpeg', 0x0055: 'mp3', 0x00FF: 'aac',
                0x0161: 'wma', 0x0162: 'wma-pro', 0x0163: 'wma-lossless'}


def probe_riff(src: Source, pos: int) -> dict | None:
    """RIFF: WAV/XWMA/AVI/WEBP и т.п. Размер берётся из поля ``RIFF size``.

    Это самый надёжный тип в томах: большинство ``DATA*.ALF`` состоит именно
    из склеенных WAV-файлов, и границы восстанавливаются точно.
    """
    head = src.at(pos, 20)
    if len(head) < 20 or head[:4] != b'RIFF':
        return None
    size = u32(head, 4)
    form = head[8:12]
    if size < 4 or size + 8 > src.size - pos:
        return None
    if form not in RIFF_FORMS:
        return None
    chunk_id = head[12:16]
    if chunk_id not in (b'fmt ', b'data', b'LIST', b'JUNK', b'fact', b'bext', b'ds64',
                        b'dpds', b'FLLR', b'PAD '):
        return None
    meta = {'form': form.decode('latin-1')}
    ext = {b'WAVE': 'wav', b'AVI ': 'avi', b'WEBP': 'webp',
           b'XWMA': 'xwma'}.get(form, 'riff')
    if form in (b'WAVE', b'XWMA'):
        # идём по чанкам: вытаскиваем параметры из 'fmt ' и размер данных из 'data'
        p, fmt_found, guard = pos + 12, False, 0
        while guard < 64:
            guard += 1
            hdr = src.at(p, 8)
            if len(hdr) < 8:
                break
            cid, csz = hdr[:4], u32(hdr, 4)
            if not is_printable_tag(cid) or p + 8 + csz > pos + 8 + size:
                break
            if cid == b'fmt ' and 16 <= csz <= 0x10000:
                fmt = src.at(p + 8, min(csz, 40))
                if len(fmt) >= 16:
                    code = u16(fmt, 0)
                    meta.update(format=WAVE_FORMATS.get(code, '0x%04X' % code),
                                channels=u16(fmt, 2), rate=u32(fmt, 4),
                                bits=u16(fmt, 14))
                    fmt_found = True
            if cid == b'data':
                meta['data_bytes'] = csz
                break
            p += 8 + csz + (csz & 1)
        if not fmt_found:
            return None
    return {'type': 'riff', 'ext': ext, 'size': 8 + size, 'meta': meta}


FONT_VERSIONS = {b'\x00\x01\x00\x00', b'OTTO', b'true', b'typ1'}
FONT_CORE_TABLES = {b'head', b'glyf', b'CFF ', b'CFF2', b'cmap', b'bhed', b'vhea'}


def _parse_single_font(src: Source, pos: int, limit: int) -> dict | None:
    """Разобрать директорию таблиц sfnt. ``limit`` -- сколько байт доступно."""
    head = src.at(pos, 12)
    if len(head) < 12 or head[:4] not in FONT_VERSIONS:
        return None
    num_tables = u16(head, 4, '>')
    if not 1 <= num_tables <= 1024:
        return None
    es = num_tables.bit_length() - 1
    sr = 16 << es
    if (u16(head, 6, '>') != sr or u16(head, 8, '>') != es
            or u16(head, 10, '>') != num_tables * 16 - sr):
        return None
    dir_size = 12 + 16 * num_tables
    directory = src.at(pos, dir_size)
    if len(directory) < dir_size:
        return None
    tables = {}
    max_end = 0
    for i in range(num_tables):
        o = 12 + 16 * i
        tag = directory[o:o + 4]
        if not is_printable_tag(tag) or tag in tables:
            return None
        off, length = u32(directory, o + 8, '>'), u32(directory, o + 12, '>')
        if off < dir_size or off % 4 or off + length > limit:
            return None
        tables[tag] = (off, length)
        max_end = max(max_end, off + length)
    if not FONT_CORE_TABLES & set(tables) or max_end <= dir_size:
        return None
    meta = {'kind': {b'\x00\x01\x00\x00': 'truetype', b'OTTO': 'cff',
                     b'true': 'truetype', b'typ1': 'type1'}.get(head[:4], 'sfnt'),
            'num_tables': num_tables}
    if b'name' in tables:
        off, length = tables[b'name']
        name = _font_family(src.at(pos + off, min(length, 1 << 16)))
        if name:
            meta['family'] = name
    return {'size': (max_end + 3) & ~3, 'meta': meta}


def _font_family(raw: bytes) -> str:
    """Достать имя семейства (nameID=1) из таблицы 'name'."""
    if len(raw) < 6:
        return ''
    count, str_off = u16(raw, 2, '>'), u16(raw, 4, '>')
    best = ''
    for i in range(min(count, 512)):
        o = 6 + 12 * i
        if o + 12 > len(raw):
            break
        pid, eid, _lid, nid, ln, so = struct.unpack_from('>6H', raw, o)
        if nid != 1:
            continue
        text = raw[str_off + so:str_off + so + ln]
        try:
            if pid in (0, 3):
                text = text.decode('utf-16-be')
            elif pid == 1:
                text = text.decode('mac-roman')
        except Exception:
            continue
        text = text.strip('\x00 ')
        if text and (not best or pid == 3):
            best = text
    return best


def probe_font(src: Source, pos: int) -> dict | None:
    """TrueType/OpenType/TTC. Размер -- по максимальному концу таблиц."""
    sig = src.at(pos, 4)
    if sig == b'ttcf':
        head = src.at(pos, 12)
        if len(head) < 12:
            return None
        major = u16(head, 4, '>')
        num_fonts = u32(head, 8, '>')
        if major not in (1, 2) or not 1 <= num_fonts <= 4096:
            return None
        offsets = src.at(pos + 12, 4 * num_fonts)
        if len(offsets) < 4 * num_fonts:
            return None
        size, names = 0, []
        for i in range(num_fonts):
            rel = u32(offsets, 4 * i, '>')
            if rel < 12 + 4 * num_fonts:
                return None
            sub = _parse_single_font(src, pos + rel, src.size - pos - rel)
            if sub is None:
                return None
            size = max(size, rel + sub['size'])
            if sub['meta'].get('family'):
                names.append(sub['meta']['family'])
        meta = {'kind': 'ttcf', 'fonts': num_fonts}
        if names:
            meta['families'] = names[:8]
        return {'type': 'font', 'ext': 'ttc', 'size': size, 'meta': meta}
    sub = _parse_single_font(src, pos, src.size - pos)
    if sub is None:
        return None
    return {'type': 'font', 'ext': 'otf' if sig == b'OTTO' else 'ttf',
            'size': sub['size'], 'meta': sub['meta']}

    return str(n)


PNG_SIG = b'\x89PNG\r\n\x1a\n'
MAX_PNG_CHUNK = 64 << 20


def probe_png(src: Source, pos: int) -> dict | None:
    if src.at(pos, 8) != PNG_SIG:
        return None
    p, meta = pos + 8, {}
    while True:
        hdr = src.at(p, 8)
        if len(hdr) < 8:
            return None
        length = u32(hdr, 0, '>')
        ctype = hdr[4:8]
        if length > MAX_PNG_CHUNK or not is_printable_tag(ctype):
            return None
        if p + 12 + length > src.size:
            return None
        if ctype == b'IHDR' and length >= 8:
            ihdr = src.at(p + 8, 8)
            if len(ihdr) >= 8:
                meta = {'width': u32(ihdr, 0, '>'), 'height': u32(ihdr, 4, '>')}
        p += 12 + length
        if ctype == b'IEND':
            return {'type': 'png', 'ext': 'png', 'size': p - pos, 'meta': meta}


JPEG_SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# допустимые маркеры до первого SOS (RSTn и TEM в заголовке невозможны)
JPEG_HEADER_MARKERS = (set(range(0xC0, 0xD0)) | set(range(0xDA, 0xE0))
                       | set(range(0xE0, 0xF0)) | {0xFE})
# реальный JPEG почти всегда начинается с одного из этих маркеров
JPEG_LEADING_MARKERS = {0xE0, 0xE1, 0xE2, 0xED, 0xEE, 0xDB, 0xC4, 0xFE, 0xDD}
MAX_JPEG = 64 << 20


def probe_jpeg(src: Source, pos: int) -> dict | None:
    """JPEG: строгий разбор цепочки маркеров, затем поиск EOI.

    Проверки намеренно жёсткие: в сжатых данных движка байты ``FF D8 FF``
    встречаются постоянно, и мягкая проверка даёт тысячи ложных срабатываний.
    """
    if src.at(pos, 3) != b'\xff\xd8\xff':
        return None
    # первый маркер после SOI обязан быть «заголовочным» и типичным
    p = pos + 2
    while p + 4 <= src.size and src.at(p, 1) == b'\xff':
        marker = src.at(p + 1, 1)
        if marker == b'\xff':                    # байт-заполнитель
            p += 1
            continue
        if marker[0] not in JPEG_LEADING_MARKERS:
            return None
        break
    else:
        return None
    meta, have_sof, have_dqt, saw_sos = {}, False, False, False
    while p + 4 <= src.size:
        hdr = src.at(p, 4)
        if len(hdr) < 4 or hdr[0] != 0xFF:
            return None
        marker = hdr[1]
        if marker == 0xFF:
            p += 1
            continue
        if marker not in JPEG_HEADER_MARKERS:
            return None
        length = (hdr[2] << 8) | hdr[3]
        if length < 2 or p + 2 + length > src.size:
            return None
        if marker in JPEG_SOF and length >= 8:
            seg = src.at(p + 4, 6)
            if len(seg) >= 6:
                height = (seg[1] << 8) | seg[2]
                width = (seg[3] << 8) | seg[4]
                comps = seg[5]
                if not (0 < width <= 65535 and 0 < height <= 65535 and 1 <= comps <= 4):
                    return None
                meta = {'precision': seg[0], 'height': height, 'width': width,
                        'components': comps}
                have_sof = True
        if marker == 0xDB:
            have_dqt = True
        p += 2 + length
        if marker == 0xDA:
            saw_sos = True
            break
    if not (saw_sos and have_sof and have_dqt):
        return None
    # после SOS ищем EOI; незакрытый SOI внутри потока означает, что это не JPEG
    limit = min(src.size, pos + MAX_JPEG)
    while p < limit:
        block = src.at(p, min(CHUNK, limit - p))
        if not block:
            return None
        idx = block.find(b'\xff\xd9')
        soi = block.find(b'\xff\xd8')
        if idx >= 0 and (soi < 0 or soi > idx):
            return {'type': 'jpeg', 'ext': 'jpg', 'size': p + idx + 2 - pos, 'meta': meta}
        if soi >= 0:
            return None
        p += max(1, len(block) - 1)
    return None


def _gif_skip_blocks(src: Source, p: int) -> int | None:
    """Пропустить под-блоки GIF (список завершается нулевым размером)."""
    for _ in range(1 << 20):
        hdr = src.at(p, 1)
        if not hdr:
            return None
        size = hdr[0]
        p += 1 + size
        if size == 0:
            return p
        if p > src.size:
            return None
    return None


def probe_gif(src: Source, pos: int) -> dict | None:
    head = src.at(pos, 13)
    if len(head) < 13 or head[:6] not in (b'GIF87a', b'GIF89a'):
        return None
    meta = {'width': u16(head, 6), 'height': u16(head, 8)}
    p = pos + 13
    flags = head[10]
    if flags & 0x80:
        p += 3 * (2 << (flags & 7))
    for _ in range(1 << 20):
        tag = src.at(p, 1)
        if not tag:
            return None
        if tag == b'\x3b':
            return {'type': 'gif', 'ext': 'gif', 'size': p + 1 - pos, 'meta': meta}
        if tag == b'\x21':
            p = _gif_skip_blocks(src, p + 2)
        elif tag == b'\x2c':
            desc = src.at(p, 10)
            if len(desc) < 10:
                return None
            p += 10
            if desc[9] & 0x80:
                p += 3 * (2 << (desc[9] & 7))
            p += 1                                # минимальный размер кода LZW
            p = _gif_skip_blocks(src, p)
        else:
            return None
        if p is None or p >= src.size:
            return None
    return None


BMP_DIB_SIZES = {12, 16, 40, 52, 56, 64, 108, 124}


def probe_bmp(src: Source, pos: int) -> dict | None:
    head = src.at(pos, 30)
    if len(head) < 26 or head[:2] != b'BM':
        return None
    size = u32(head, 2)
    if u32(head, 6) != 0 or size < 26 or size > src.size - pos:
        return None
    dib = u32(head, 14)
    if dib not in BMP_DIB_SIZES:
        return None
    meta = {'dib': dib}
    if dib >= 40 and len(head) >= 30:
        meta.update(width=struct.unpack_from('<i', head, 18)[0],
                    height=struct.unpack_from('<i', head, 22)[0],
                    bits=u16(head, 28))
    return {'type': 'bmp', 'ext': 'bmp', 'size': size, 'meta': meta}


DDS_BLOCK_FOURCC = {b'DXT1': 8, b'DXT2': 16, b'DXT3': 16, b'DXT4': 16, b'DXT5': 16,
                    b'ATI1': 8, b'BC4U': 8, b'BC4S': 8, b'ATI2': 16,
                    b'BC5U': 16, b'BC5S': 16}


def probe_dds(src: Source, pos: int) -> dict | None:
    head = src.at(pos, 128)
    if len(head) < 128 or head[:4] != b'DDS ':
        return None
    if u32(head, 4) != 124 or u32(head, 76) != 32:
        return None
    height, width = u32(head, 12), u32(head, 16)
    if not width or not height:
        return None
    depth = max(1, u32(head, 24))
    mips = max(1, min(u32(head, 28), 32))
    fourcc = head[84:88]
    block = DDS_BLOCK_FOURCC.get(fourcc, 0)
    if block:
        total = 128
        for i in range(mips):
            w, h = max(1, width >> i), max(1, height >> i)
            total += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block * depth
    else:
        bpp = u32(head, 88)
        if bpp not in (8, 16, 24, 32, 64, 128):
            return None
        total = 128
        for i in range(mips):
            w, h = max(1, width >> i), max(1, height >> i)
            total += w * h * (bpp // 8) * depth
        if fourcc == b'DX10':
            total += 20
    if total > src.size - pos:
        return None
    return {'type': 'dds', 'ext': 'dds', 'size': total,
            'meta': {'width': width, 'height': height, 'mips': mips,
                     'fourcc': fourcc.decode('latin-1')}}


def probe_mpeg(src: Source, pos: int) -> dict | None:
    """MPEG-1/2 program stream: ``pos`` указывает на pack header ``000001BA``.

    Граница потока точная: файлы склеены так, что перед началом каждого
    следующего стоит end-of-sequence ``000001B9`` (проверено на DATA5.ALF:
    80 разделителей ``000001B9 000001BA`` -> 81 поток).
    """
    head = src.at(pos, 8)
    if len(head) < 8 or head[:4] != b'\x00\x00\x01\xba':
        return None
    b4 = head[4]
    if (b4 & 0xF0) != 0x20 and (b4 & 0xC0) != 0x40:   # не MPEG-1 и не MPEG-2 pack
        return None
    if pos != 0 and src.at(pos - 4, 4) != b'\x00\x00\x01\xb9':
        return None                                    # середина потока, не граница
    meta = {'mpeg': 'mpeg-2' if (b4 & 0xC0) == 0x40 else 'mpeg-1'}
    return {'type': 'mpeg', 'ext': 'mpg', 'size': 0, 'segmented': True, 'meta': meta}


def probe_acgf(src: Source, pos: int) -> dict | None:
    """Закрытый ресурс движка AGE (тег ``ACGF``).

    Инварианты заголовка (проверены на всех томах): версия 2, затем 0,
    затем два одинаковых поля размера листа 1080x1080. Проверка нужна,
    иначе тег ловится внутри сжатых данных.
    """
    head = src.at(pos, 36)
    if len(head) < 36 or head[:4] != b'ACGF':
        return None
    words = struct.unpack_from('<8I', head, 4)
    if words[0] != 2 or words[1] != 0 or words[2] != words[3] or words[2] == 0:
        return None
    return {'type': 'acgf', 'ext': 'acgf', 'size': 0, 'segmented': True,
            'meta': {'version': words[0], 'sheet': words[2], 'pages': words[4],
                     'words': list(words)}}


def probe_sysfs(src: Source, pos: int) -> dict | None:
    """Системный ресурс движка AGE (тег ``SYS4472 ``).

    Инварианты: слово 6 всегда 28 (0x1C), слово 4 всегда 1, слова 3 и 5 --
    малые счётчики. Файл читается как контейнер до следующего такого заголовка.
    """
    head = src.at(pos, 40)
    if len(head) < 40 or head[:8] != b'SYS4472 ':
        return None
    words = struct.unpack_from('<8I', head, 8)
    if words[6] != 0x1C or words[4] != 1 or not 1 <= words[3] <= 64 or words[5] > 64:
        return None
    return {'type': 'sys4472', 'ext': 'sys4472', 'size': 0, 'segmented': True,
            'meta': {'kind': words[0], 'sub': words[2], 'words': list(words)}}



# --------------------------------------------------------------------------
# сканер тома
# --------------------------------------------------------------------------

# порядок важен: длинные сигнатуры идут раньше коротких
# (сигнатура, тип записи, обработчик)
MAGICS = [
    (PNG_SIG, 'png', probe_png),
    (b'SYS4472 ', 'sys4472', probe_sysfs),
    (b'\x00\x00\x01\xba', 'mpeg', probe_mpeg),
    (b'\x00\x01\x00\x00', 'font', probe_font),
    (b'\xff\xd8\xff', 'jpeg', probe_jpeg),
    (b'RIFF', 'riff', probe_riff),
    (b'ACGF', 'acgf', probe_acgf),
    (b'OTTO', 'font', probe_font),
    (b'ttcf', 'font', probe_font),
    (b'true', 'font', probe_font),
    (b'GIF8', 'gif', probe_gif),
    (b'DDS ', 'dds', probe_dds),
    (b'BM', 'bmp', probe_bmp),
]

MAX_MAGIC = max(len(sig) for sig, _t, _h in MAGICS)

# тип записи -> имя «семейства» для CLI
TYPE_ALIASES = {
    'wav': 'riff', 'avi': 'riff', 'webp': 'riff', 'riff': 'riff',
    'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png', 'gif': 'gif', 'bmp': 'bmp',
    'ttf': 'font', 'otf': 'font', 'ttc': 'font', 'font': 'font',
    'dds': 'dds', 'mpg': 'mpeg', 'mpeg': 'mpeg',
    'acgf': 'acgf', 'sys4472': 'sys4472', 'blob': ('acgf', 'sys4472'),
}
ALL_TYPES = ('riff', 'font', 'png', 'jpeg', 'gif', 'bmp', 'dds', 'mpeg',
             'acgf', 'sys4472')


@dataclass
class Record:
    """Найденный в томе объект."""
    type: str
    ext: str
    offset: int
    size: int
    meta: dict = field(default_factory=dict)
    segmented: bool = False
    parent: int | None = None      # смещение контейнера, если объект вложенный
    depth: int = 0                 # 0 -- верхний уровень тома


def parse_types(spec: str | None) -> set:
    if not spec or spec.strip().lower() in ('all', '*'):
        return set(ALL_TYPES)
    result = set()
    for item in re.split(r'[,;\s]+', spec.strip().lower()):
        if not item:
            continue
        if item not in TYPE_ALIASES:
            raise SystemExit('неизвестный тип: %s (доступны: %s)'
                             % (item, ', '.join(sorted(TYPE_ALIASES))))
        value = TYPE_ALIASES[item]
        result.update(value if isinstance(value, tuple) else (value,))
    return result


class Scanner:
    """Один проход по тому: поиск сигнатур, валидация, вычисление границ."""

    def __init__(self, src: Source, types: set, min_size: int = 0) -> None:
        self.src = src
        self.types = types
        self.min_size = min_size
        # ищем только те сигнатуры, чьи типы запрошены
        self.magics = [(sig, t, handler) for sig, t, handler in MAGICS if t in types]

    def scan(self) -> list:
        """Найти все объекты тома за один последовательный проход."""
        src, size = self.src, self.src.size
        records, segmented = [], []
        typed_end = {}
        seg_starts = set()
        prev = b''
        base = 0
        while base < size:
            block = src._seq.read(min(CHUNK, size - base))
            if not block:
                break
            data = prev + block
            data_base = base - len(prev)
            src.set_buffer(data, data_base)
            # 1) собрать кандидатов в текущем окне (быстрый bytes.find)
            candidates = []
            for sig, mtype, handler in self.magics:
                start = 0
                while True:
                    idx = data.find(sig, start)
                    if idx < 0:
                        break
                    candidates.append((data_base + idx, mtype, handler))
                    start = idx + 1
            candidates.sort(key=lambda item: item[0])
            # 2) валидировать в порядке возрастания смещений
            for pos, mtype, handler in candidates:
                # объект внутри уже принятого объекта того же типа пропускаем:
                # так отсекаются шрифты внутри коллекции и «двойные» находки
                if pos < typed_end.get(mtype, 0):
                    continue
                rec = handler(src, pos)
                if rec is None or rec['type'] not in self.types:
                    continue
                rec_type = rec['type']
                if rec.get('segmented'):
                    start = rec.get('start', pos)
                    key = (rec_type, start)
                    if key in seg_starts:
                        continue
                    seg_starts.add(key)
                    segmented.append(Record(rec_type, rec['ext'], start, 0,
                                            rec['meta'], True))
                    continue
                rec_size = rec['size']
                if rec_size <= 0 or rec_size > size - pos or rec_size < self.min_size:
                    continue
                records.append(Record(rec_type, rec['ext'], pos, rec_size, rec['meta']))
                typed_end[rec_type] = pos + rec_size
            prev = data[-(MAX_MAGIC - 1):]
            base += len(block)
        # границы «сегментированных» записей: до начала следующей такой же записи
        by_type = {}
        for item in segmented:
            by_type.setdefault(item.type, []).append(item)
        for items in by_type.values():
            items.sort(key=lambda r: r.offset)
            for i, item in enumerate(items):
                item.size = (items[i + 1].offset if i + 1 < len(items) else size) - item.offset
        records.extend(r for items in by_type.values() for r in items
                       if r.size >= self.min_size)
        records.sort(key=lambda r: r.offset)
        return records




# --------------------------------------------------------------------------
# анализ раскладки тома: первичный тип, вложенность, неизвлечённые области
# --------------------------------------------------------------------------

def union_length(records: list) -> int:
    """Суммарная длина объединения диапазонов (без двойного учёта)."""
    total, cur_start, cur_end = 0, None, None
    for rec in sorted(records, key=lambda r: r.offset):
        start, end = rec.offset, rec.offset + rec.size
        if cur_end is None:
            cur_start, cur_end = start, end
        elif start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def choose_primary(records: list, size: int, threshold: float) -> dict:
    """Выбрать тип, записи которого образуют «верхний уровень» тома.

    Возвращает словарь ``{тип: доля покрытия}`` и лучший тип (может быть None).
    """
    coverage = {}
    for rec_type in {r.type for r in records}:
        same = [r for r in records if r.type == rec_type]
        coverage[rec_type] = union_length(same) / float(size) if size else 0.0
    best = None
    if coverage:
        best_type = max(coverage, key=lambda t: coverage[t])
        if coverage[best_type] >= threshold:
            best = best_type
    return {'coverage': coverage, 'best': best}


def constrain_to_containers(records: list, primary: str | None) -> None:
    """Обрезать «сегментированные» записи по границе контейнера верхнего уровня.

    Если тип верхнего уровня определён (в DATA1.ALF это ``SYS4472``), то цепочка
    ``ACGF`` не должна выходить за его пределы: иначе одна запись «проглатывает»
    начало следующего контейнера.
    """
    if primary is None:
        return
    containers = sorted((r for r in records if r.type == primary),
                        key=lambda r: r.offset)
    if not containers:
        return
    starts = [c.offset for c in containers]
    for rec in records:
        if rec.type == primary or not rec.segmented:
            continue
        index = bisect.bisect_right(starts, rec.offset) - 1
        if index < 0:
            continue
        end = containers[index].offset + containers[index].size
        if rec.offset < end < rec.offset + rec.size:
            rec.size = end - rec.offset


def assign_parents(records: list) -> None:
    """Проставить ``parent`` (смещение контейнера) и ``depth`` для вложенных.

    Контейнер -- минимальная по размеру запись, которая полностью и строго
    содержит данную. Вложенность в томах реальная: например, в DATA1.ALF
    записи ``ACGF``/``RIFF``/шрифт лежат внутри записей ``SYS4472``.
    """
    ordered = sorted(records, key=lambda r: (r.offset, -r.size))
    stack = []
    for rec in ordered:
        while stack and rec.offset >= stack[-1].offset + stack[-1].size:
            stack.pop()
        parent = None
        for candidate in reversed(stack):
            if (candidate.offset <= rec.offset
                    and rec.offset + rec.size <= candidate.offset + candidate.size
                    and candidate.size > rec.size):
                parent = candidate
                break
        rec.parent = parent.offset if parent is not None else None
        rec.depth = (parent.depth + 1) if parent is not None else 0
        stack.append(rec)
        if len(stack) > 64:                      # защита от вырожденных случаев
            stack = stack[-64:]


def find_gaps(records: list, size: int) -> list:
    """Области тома, не покрытые ни одной извлечённой записью."""
    gaps, pos = [], 0
    for rec in sorted(records, key=lambda r: r.offset):
        if rec.offset > pos:
            gaps.append((pos, rec.offset - pos))
        pos = max(pos, rec.offset + rec.size)
    if pos < size:
        gaps.append((pos, size - pos))
    return gaps


# --------------------------------------------------------------------------
# извлечение и отчётность
# --------------------------------------------------------------------------

def find_volumes(paths: list) -> list:
    """Развернуть список путей в список файлов-томов ``*.ALF``."""
    result = []
    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.lower().endswith('.alf'):
                    result.append(os.path.join(path, name))
        elif os.path.isfile(path):
            result.append(path)
        else:
            raise SystemExit('путь не найден: %s' % path)
    if not result:
        raise SystemExit('не найдено ни одного тома *.ALF')
    return result


def safe_name(text: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '_', text).strip('_') or 'x'


def write_manifest_header(writer: csv.writer) -> None:
    writer.writerow(['volume', 'type', 'ext', 'offset', 'size', 'depth', 'parent',
                     'sha1', 'saved_as', 'meta'])


def write_gap_report(path: str, volume: str, src: 'Source', gaps: list) -> None:
    """Записать неизвлечённые области тома вместе с их началом (hex + ASCII)."""
    with open(path, 'w', encoding='utf-8-sig') as out:
        out.write('# %s: области, не покрытые ни одной записью\n' % volume)
        out.write('# offset     size        head (64 байта)\n')
        for offset, size in gaps:
            head = src.at(offset, 64)
            hexpart = head.hex()
            text = ''.join(chr(c) if 32 <= c < 127 else '.' for c in head)
            out.write('%010X %10d  %s  %s\n' % (offset, size, hexpart, text))


def process_volume(path: str, args, writer: csv.writer | None, seen: dict) -> dict:
    """Просканировать том, извлечь записи; вернуть статистику."""
    volume = os.path.basename(path)
    stats = {'records': 0, 'processed': 0, 'written': 0, 'bytes': 0, 'dups': 0,
             'nested': 0, 'by_type': {}, 'skipped': 0, 'gaps': [], 'coverage': {},
             'primary': None, 'claimed': 0}
    started = time.time()
    with Source(path) as src:
        records = Scanner(src, args.types_set, args.min_size).scan()
        choice = choose_primary(records, src.size, args.primary_threshold)
        constrain_to_containers(records, choice['best'])
        records = [r for r in records if r.size > 0]
        assign_parents(records)
        stats['coverage'] = choice['coverage']
        stats['primary'] = choice['best']
        if args.top_level:
            records = [r for r in records if r.depth == 0]
        stats['records'] = len(records)
        stats['claimed'] = union_length(records)
        if args.report_gaps:
            stats['gaps'] = find_gaps(records, src.size)
            gap_path = os.path.join(args.out, '%s_gaps.txt'
                                    % safe_name(os.path.splitext(volume)[0]))
            if args.list_only:
                stats['gap_path'] = '(не записан в режиме --list)'
            else:
                write_gap_report(gap_path, volume, src, stats['gaps'])
                stats['gap_path'] = gap_path
        for index, rec in enumerate(records):
            if args.max_items and index >= args.max_items:
                stats['skipped'] += len(records) - index
                break
            stats['processed'] += 1
            stats['by_type'][rec.type] = stats['by_type'].get(rec.type, 0) + 1
            stats['bytes'] += rec.size
            if rec.depth:
                stats['nested'] += 1
            if args.list_only:
                if not args.quiet:
                    print('  %-10s %08X %10s d=%d %s'
                          % (rec.type, rec.offset, human(rec.size), rec.depth,
                             json.dumps(rec.meta, ensure_ascii=False)))
                continue
            dest_dir = os.path.join(args.out, safe_name(os.path.splitext(volume)[0]),
                                    rec.type)
            os.makedirs(dest_dir, exist_ok=True)
            tmp_path = os.path.join(dest_dir, '%010X.part' % rec.offset)
            hasher = hashlib.sha1()
            copied = src.copy_to(rec.offset, rec.size, tmp_path, hasher)
            digest = hasher.hexdigest()
            dup_of = seen.get(digest)
            if args.dedup and dup_of is not None:
                os.remove(tmp_path)
                stats['dups'] += 1
                saved_as = dup_of
            else:
                seen.setdefault(digest, tmp_path)
                final = os.path.join(dest_dir, '%010X_%s.%s'
                                     % (rec.offset, digest[:8], rec.ext))
                os.replace(tmp_path, final)
                saved_as = os.path.relpath(final, args.out).replace('\\', '/')
                stats['written'] += 1
            if writer is not None:
                writer.writerow([volume, rec.type, rec.ext, rec.offset, copied,
                                 rec.depth,
                                 '' if rec.parent is None else '%010X' % rec.parent,
                                 digest, saved_as,
                                 json.dumps(rec.meta, ensure_ascii=False)])
    stats['elapsed'] = time.time() - started
    return stats


def print_volume_summary(volume: str, stats: dict) -> None:
    processed = stats['processed']
    print('%s: %d записей%s, %s, %.1f c%s'
          % (volume, processed,
             '' if processed == stats['records'] else ' из %d' % stats['records'],
             human(stats['bytes']), stats['elapsed'],
             '' if not stats['by_type'] else ' -- по типам: ' + ', '.join(
                 '%s=%d' % (k, v) for k, v in sorted(stats['by_type'].items()))))
    if stats['coverage']:
        print('   покрытие по типам: %s; верхний уровень: %s'
              % (', '.join('%s=%.1f%%' % (k, v * 100.0)
                           for k, v in sorted(stats['coverage'].items(),
                                              key=lambda kv: -kv[1])),
                 stats['primary'] or 'не определён'))
    if stats['nested']:
        print('   вложенных объектов: %d (внутри контейнеров)' % stats['nested'])
    if stats['gaps']:
        total_gap = sum(size for _o, size in stats['gaps'])
        print('   не извлечено: %s в %d областях, отчёт: %s'
              % (human(total_gap), len(stats['gaps']), stats['gap_path']))
    if stats['written'] or stats['dups']:
        print('   сохранено %d объектов, дубликатов %d, пропущено %d'
              % (stats['written'], stats['dups'], stats['skipped']))



# --------------------------------------------------------------------------
# командная строка
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='alf_extract.py',
        description='Извлечение данных из томов *.ALF (конкатенация ресурсов).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Примеры:\n'
               '  python alf_extract.py --list etc/example\n'
               '  python alf_extract.py etc/example --out etc/out\n'
               '  python alf_extract.py etc/example --out etc/out --types wav,ttf\n'
               '  python alf_extract.py etc/example --out etc/out --top-level --no-blobs\n')
    parser.add_argument('paths', nargs='+', metavar='PATH',
                        help='файлы *.ALF или каталоги с ними')
    parser.add_argument('--out', default='extracted', metavar='DIR',
                        help='каталог для результатов (по умолчанию: extracted)')
    parser.add_argument('--types', metavar='SPEC',
                        help='какие типы извлекать: riff,wav,font,ttf,otf,ttc,jpeg,jpg,'
                             'png,gif,bmp,dds,mpeg,mpg,acgf,sys4472,blob,all')
    parser.add_argument('--no-blobs', action='store_true',
                        help='не извлекать закрытые форматы ACGF/SYS4472')
    parser.add_argument('--min-size', type=int, default=1, metavar='N',
                        help='минимальный размер объекта в байтах (по умолчанию: 1)')
    parser.add_argument('--max-items', type=int, default=0, metavar='N',
                        help='обработать не более N объектов на том (для тестов)')
    parser.add_argument('--list', dest='list_only', action='store_true',
                        help='только показать найденное, ничего не записывать')
    parser.add_argument('--top-level', action='store_true',
                        help='извлекать только объекты верхнего уровня, без вложенных')
    parser.add_argument('--report-gaps', dest='report_gaps', action='store_true',
                        default=True,
                        help='писать отчёт о неизвлечённых областях (по умолчанию включено)')
    parser.add_argument('--no-report-gaps', dest='report_gaps', action='store_false')
    parser.add_argument('--primary-threshold', type=float, default=0.5, metavar='P',
                        help='доля покрытия, при которой тип считается верхним '
                             'уровнем тома (по умолчанию: 0.5)')
    parser.add_argument('--dedup', dest='dedup', action='store_true', default=True,
                        help='не сохранять повторяющиеся объекты (по умолчанию включено)')
    parser.add_argument('--no-dedup', dest='dedup', action='store_false',
                        help='сохранять все объекты, включая дубликаты')
    parser.add_argument('--quiet', action='store_true',
                        help='не печатать каждую запись в режиме --list')
    return parser


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    args.types_set = parse_types(args.types)
    if args.no_blobs:
        args.types_set -= {'acgf', 'sys4472'}
    if not args.types_set:
        raise SystemExit('не выбран ни один тип для извлечения')

    volumes = find_volumes(args.paths)
    print('томов: %d, типы: %s' % (len(volumes), ', '.join(sorted(args.types_set))))
    total = {'records': 0, 'processed': 0, 'written': 0, 'bytes': 0, 'dups': 0,
             'elapsed': 0.0, 'by_type': {}}
    seen = {}
    writer = None
    manifest = None
    if not args.list_only:
        os.makedirs(args.out, exist_ok=True)
        manifest_path = os.path.join(args.out, 'manifest.csv')
        manifest = open(manifest_path, 'w', newline='', encoding='utf-8-sig')
        writer = csv.writer(manifest, delimiter=';')
        write_manifest_header(writer)
        print('результаты: %s (манифест: %s)' % (args.out, manifest_path))
    started = time.time()
    try:
        for path in volumes:
            stats = process_volume(path, args, writer, seen)
            print_volume_summary(os.path.basename(path), stats)
            for key in ('records', 'processed', 'written', 'bytes', 'dups'):
                total[key] += stats[key]
            total['elapsed'] += stats['elapsed']
            for key, value in stats['by_type'].items():
                total['by_type'][key] = total['by_type'].get(key, 0) + value
    except KeyboardInterrupt:
        print('\nпрервано пользователем')
    finally:
        if manifest is not None:
            manifest.close()
    print('---')
    print('итого: %d объектов (%s), сохранено %d, дубликатов %d, %.1f c'
          % (total['processed'], human(total['bytes']), total['written'],
             total['dups'], total['elapsed']))
    for key, value in sorted(total['by_type'].items()):
        print('  %-10s %d' % (key, value))
    return 0

if __name__ == '__main__':
    sys.exit(main())

