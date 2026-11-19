#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 Artem Pronchenko
# SPDX-License-Identifier: GPL-3.0-or-later
r"""
rpgmvp2png.py -- картинки RPG Maker MV (``*.rpgmvp``) -> обычный PNG.

Структура файла
---------------
1. ``*.rpgmvp`` -- это обычный PNG, у которого **первые 16 байт зашифрованы**,
   а перед ними стоит 16-байтовый служебный заголовок::

       +0    16 байт   'RPGMV' 00 00 00 00 03 01 00 00 00 00 00   заголовок
       +16   16 байт   первые 16 байт PNG, XOR с ключом игры
       +32   ...       остаток PNG как есть (не шифруется)

   Проверено на наборе из 5768 файлов: после снятия XOR с байтов 16..31
   получается ровно ``\x89PNG\r\n\x1a\n`` + длина чанка ``IHDR`` + ``IHDR``,
   а всё тело файла разбирается как PNG с корректными CRC всех чанков.

2. Ключ -- 16 байт, лежит в ``data/System.json`` игры в поле
   ``"encryptionKey"`` (32 hex-цифры). XOR применяется **только** к первым
   16 байтам картинки, остальное тело файла не изменено.

3. Поэтому ключ не обязателен: первые 16 байт любого PNG известны заранее
   (сигнатура + длина и имя первого чанка ``IHDR``), значит ключ
   восстанавливается из самого файла: ``key = enc16 XOR plain16``.
   Именно это делает :func:`derive_key`; ``--key`` и ``--system-json`` нужны,
   только если файл повреждён или нужно сверить конкретный ключ.

Использование
-------------
    python rpgmvp2png.py                                # input\ -> output\
    python rpgmvp2png.py "C:\game\img\pictures"         # -> рядом каталог output
    python rpgmvp2png.py "C:\game\img" --out png        # свои каталоги
    python rpgmvp2png.py "C:\game\img" --out png --flat --overwrite
    python rpgmvp2png.py input --dry-run                # проверка без записи
    python rpgmvp2png.py input --key 3be360cc21cc93da2c203c59f7d4a554
    python rpgmvp2png.py input --system-json "C:\game\data\System.json"

Коды возврата: ``0`` -- все файлы сконвертированы (или проверены);
``1`` -- были ошибки (список печатается в конце); ``2`` -- неверные аргументы.

Зависимостей нет (только стандартная библиотека, Python 3.9+).
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import zlib

# --------------------------------------------------------------------------
# константы формата
# --------------------------------------------------------------------------

RPGMV_MAGIC = b'RPGMV'          # начало служебного заголовка *.rpgmvp
MV_HEADER = 16                  # длина служебного заголовка
ENC_PREFIX = 16                 # сколько первых байт картинки зашифровано

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
# Первые 16 байт любого PNG: сигнатура + длина чанка IHDR (13) + 'IHDR'.
# Это открытый текст для атаки по известному началу: key = enc16 XOR plain16.
PNG_KNOWN_PREFIX = PNG_SIGNATURE + b'\x00\x00\x00\x0d' + b'IHDR'

IMAGE_EXT = '.rpgmvp'
PNG_EXT = '.png'


# --------------------------------------------------------------------------
# ключ шифрования
# --------------------------------------------------------------------------


def parse_key(text: str) -> bytes:
    """Разобрать 16-байтовый ключ из hex-строки.

    Разделители ``:``, ``-``, ``_`` и пробелы игнорируются, поэтому ключ можно
    вставлять прямо из ``System.json`` или из лога.
    """
    digits = ''.join(c for c in text if c not in ' \t\r\n:-_')
    try:
        key = bytes.fromhex(digits)
    except ValueError:
        raise ValueError('ключ должен быть hex-строкой из 32 цифр: %r' % text) from None
    if len(key) != ENC_PREFIX:
        raise ValueError('ключ должен быть длиной %d байт (32 hex-цифры), '
                         'а не %d' % (ENC_PREFIX, len(key)))
    return key


def key_from_system_json(path: str) -> bytes:
    """Взять ключ из поля ``encryptionKey`` файла ``System.json`` игры."""
    with open(path, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('%s: ожидался JSON-объект' % path)
    value = data.get('encryptionKey') or ''
    if not value:
        raise ValueError('%s: нет поля "encryptionKey" (игра без шифрования?)' % path)
    try:
        return parse_key(str(value))
    except ValueError as exc:
        raise ValueError('%s: %s' % (path, exc)) from None


def derive_key(raw: bytes) -> bytes:
    """Восстановить ключ по известному началу PNG (known-plaintext)."""
    enc = raw[MV_HEADER:MV_HEADER + ENC_PREFIX]
    if len(enc) < ENC_PREFIX:
        raise ValueError('файл короче заголовка *.rpgmvp (%d байт)' % len(raw))
    return bytes(a ^ b for a, b in zip(enc, PNG_KNOWN_PREFIX))


# --------------------------------------------------------------------------
# расшифровка и проверка PNG
# --------------------------------------------------------------------------


def decrypt_png(raw: bytes, key: bytes | None = None) -> bytes:
    """Снять шифрование с ``*.rpgmvp`` и вернуть байты обычного PNG.

    ``key=None`` -- ключ восстанавливается из самого файла (см.
    :func:`derive_key`). Шифрование затрагивает только байты 16..31, поэтому
    результат -- ``(enc16 XOR key) + raw[32:]``.
    """
    if len(raw) < MV_HEADER + ENC_PREFIX:
        raise ValueError('файл короче заголовка *.rpgmvp (%d байт)' % len(raw))
    if key is None:
        key = derive_key(raw)
    head = bytes(a ^ b for a, b in zip(raw[MV_HEADER:MV_HEADER + ENC_PREFIX], key))
    return head + raw[MV_HEADER + ENC_PREFIX:]


def check_png(data: bytes, deep: bool = True) -> dict:
    """Проверить структуру PNG и вернуть сводку о ней.

    Проверяется: сигнатура, первым чанком идёт ``IHDR`` длины 13, чанки не
    обрезаны, последний чанк -- ``IEND``; при ``deep`` -- ещё и CRC каждого
    чанка (``zlib.crc32``), то есть целостность содержимого.

    Возвращает словарь с полями ``ok``, ``error``, ``w``, ``h``,
    ``bit_depth``, ``color_type``, ``chunks``, ``trailing`` (байты после
    ``IEND``; лишние байты не считаются ошибкой -- только пометка).
    """
    info = {'ok': False, 'error': '', 'w': 0, 'h': 0, 'bit_depth': 0,
            'color_type': 0, 'chunks': 0, 'trailing': 0}
    if not data.startswith(PNG_SIGNATURE):
        info['error'] = 'нет сигнатуры PNG'
        return info
    pos = len(PNG_SIGNATURE)
    total = len(data)
    first = True
    while True:
        if pos + 8 > total:
            info['error'] = 'обрезанный заголовок чанка'
            return info
        length, name = struct.unpack_from('>I4s', data, pos)
        body_start = pos + 8
        body_end = body_start + length
        if body_end + 4 > total:
            info['error'] = 'чанк %s обрезан' % name.decode('latin-1', 'replace')
            return info
        if first:
            if name != b'IHDR' or length != 13:
                info['error'] = 'первый чанк -- не IHDR'
                return info
            (info['w'], info['h'],
             info['bit_depth'], info['color_type']) = struct.unpack_from(
                '>IIBB', data, body_start)
            first = False
        if deep:
            (crc,) = struct.unpack_from('>I', data, body_end)
            if zlib.crc32(data[pos + 4:body_end]) & 0xFFFFFFFF != crc:
                info['error'] = ('не сходится CRC чанка %s'
                                 % name.decode('latin-1', 'replace'))
                return info
        info['chunks'] += 1
        pos = body_end + 4
        if name == b'IEND':
            break
    if not info['w'] or not info['h']:
        info['error'] = 'нулевые размеры в IHDR'
        return info
    info['trailing'] = total - pos
    info['ok'] = True
    return info


def decode_file(path: str, key: bytes | None,
                deep: bool = True) -> tuple[bytes, bytes | None, str]:
    """Прочитать файл и вернуть ``(png, ключ, как)``.

    ``как`` -- ``'rpgmvp'`` (файл был зашифрован) или ``'plain'`` (это уже
    обычный PNG, например при ``encrypted=false``). Ошибки -- ``ValueError``.
    """
    with open(path, 'rb') as f:
        raw = f.read()
    if raw.startswith(PNG_SIGNATURE):
        png, used, how = raw, key, 'plain'
    elif raw.startswith(RPGMV_MAGIC):
        if key is None:
            key = derive_key(raw)
        png, used, how = decrypt_png(raw, key), key, 'rpgmvp'
    else:
        raise ValueError('не *.rpgmvp и не PNG (начало: %s)' % raw[:8].hex(' '))
    info = check_png(png, deep=deep)
    if not info['ok']:
        raise ValueError('PNG не прошёл проверку: %s' % info['error'])
    return png, used, how


# --------------------------------------------------------------------------
# обход каталогов и имена результатов
# --------------------------------------------------------------------------


def iter_inputs(root: str) -> list[str]:
    """Все ``*.rpgmvp`` в каталоге ``root`` (рекурсивно, в стабильном порядке)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(IMAGE_EXT):
                out.append(os.path.join(dirpath, name))
    return out


def default_outdir(root: str) -> str:
    """Каталог ``output`` рядом с каталогом, в котором лежат исходники."""
    return os.path.join(os.path.dirname(os.path.normpath(root)), 'output')


def out_path(src: str, root: str, outdir: str, flat: bool,
             used: set[str] | None = None) -> str:
    """Куда писать PNG для исходника ``src``.

    Без ``flat`` структура подкаталогов повторяется; с ``flat`` все PNG
    складываются в один каталог, а совпадающие имена (без учёта регистра)
    получают суффикс ``_2``, ``_3``, ...
    """
    rel = os.path.basename(src) if flat else os.path.relpath(src, root)
    stem = rel[:-len(IMAGE_EXT)] if rel.lower().endswith(IMAGE_EXT) else rel
    target = os.path.join(outdir, stem + PNG_EXT)
    if used is not None:
        base, k = target, 1
        while target.lower() in used:
            k += 1
            target = os.path.join(outdir, '%s_%d%s' % (stem, k, PNG_EXT))
        used.add(target.lower())
    return target


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='rpgmvp2png.py',
        description='Картинки RPG Maker MV (*.rpgmvp) -> обычный PNG')
    ap.add_argument('paths', nargs='*', metavar='ВХОД',
                    help='файлы *.rpgmvp или каталоги с ними '
                         '(по умолчанию каталог input рядом со скриптом)')
    ap.add_argument('--out', metavar='DIR',
                    help='каталог результатов (по умолчанию output рядом с '
                         'входным каталогом)')
    ap.add_argument('--flat', action='store_true',
                    help='не повторять структуру подкаталогов, все PNG в один каталог')
    ap.add_argument('--overwrite', action='store_true',
                    help='перезаписывать существующие PNG (по умолчанию пропускаются)')
    ap.add_argument('--dry-run', action='store_true',
                    help='ничего не писать: расшифровать, проверить, напечатать сводку')
    ap.add_argument('--no-check', action='store_true',
                    help='не сверять CRC чанков PNG (быстрее, но без проверки целостности)')
    ap.add_argument('--key', metavar='HEX',
                    help='ключ шифрования (32 hex-цифры) вместо автоопределения')
    ap.add_argument('--system-json', metavar='PATH',
                    help='взять ключ из поля encryptionKey файла System.json игры')
    ap.add_argument('-v', '--verbose', action='store_true', help='печатать каждый файл')
    ap.add_argument('-q', '--quiet', action='store_true', help='только ошибки')
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = not args.quiet

    key = None
    try:
        if args.key:
            key = parse_key(args.key)
        elif args.system_json:
            key = key_from_system_json(args.system_json)
    except (OSError, ValueError) as exc:
        print('[!] %s' % exc)
        return 2

    script_dir = os.path.dirname(os.path.abspath(__file__))
    inputs = args.paths or [os.path.join(script_dir, 'input')]

    missing = [p for p in inputs if not os.path.exists(p)]
    if missing:
        for p in missing:
            print('[!] вход не найден: %s' % p)
        return 2

    jobs: list[tuple[str, str]] = []
    used: set[str] = set()
    for p in inputs:
        abs_p = os.path.abspath(p)
        if os.path.isdir(abs_p):
            root, files = abs_p, iter_inputs(abs_p)
        else:
            root, files = os.path.dirname(abs_p), [abs_p]
        if not files:
            if report:
                print('[i] нет *.rpgmvp: %s' % abs_p)
            continue
        outdir = os.path.abspath(args.out) if args.out else default_outdir(root)
        for src in files:
            jobs.append((src, out_path(src, root, outdir, args.flat, used)))

    if not jobs:
        print('[!] нечего конвертировать: файлы *.rpgmvp не найдены')
        return 1

    if report:
        print('файлов *.rpgmvp: %d' % len(jobs))
        print('режим: %s' % ('проверка без записи' if args.dry_run
                             else 'конвертация'))
        if args.flat:
            print('раскладка: все PNG в один каталог')

    stats = {'ok': 0, 'skipped': 0, 'plain': 0, 'failed': 0}
    errors: list[str] = []
    key_source = '--key' if args.key else ('--system-json' if args.system_json else '')

    for src, dst in jobs:
        try:
            png, key, how = decode_file(src, key, deep=not args.no_check)
        except (OSError, ValueError) as exc:
            stats['failed'] += 1
            errors.append('%s: %s' % (src, exc))
            continue
        if key is not None and not key_source:
            key_source = os.path.basename(src)
            if report:
                print('ключ шифрования по первому файлу (%s): %s'
                      % (key_source, key.hex()))
        if how == 'plain':
            stats['plain'] += 1
        if args.dry_run:
            stats['ok'] += 1
            if args.verbose:
                print('  ok  %s' % src)
            continue
        if os.path.exists(dst) and not args.overwrite:
            stats['skipped'] += 1
            if args.verbose:
                print('  есть, пропуск: %s' % dst)
            continue
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dst, 'wb') as f:
            f.write(png)
        stats['ok'] += 1
        if args.verbose:
            print('  %s -> %s' % (src, dst))

    if report:
        print()
        verb = 'проверено' if args.dry_run else 'записано PNG'
        tail = ''
        if stats['plain']:
            tail += '; уже были PNG: %d' % stats['plain']
        if stats['skipped']:
            tail += '; пропущено (уже есть): %d' % stats['skipped']
        print('%s: %d%s; ошибок: %d' % (verb, stats['ok'], tail, stats['failed']))
    for line in errors[:20]:
        print('[!] %s' % line)
    if len(errors) > 20:
        print('[!] ... ещё ошибок: %d' % (len(errors) - 20))

    return 1 if stats['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())

