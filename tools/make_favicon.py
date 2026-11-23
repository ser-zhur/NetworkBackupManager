# =========================================================
# tools/make_favicon.py — web/static/favicon.ico
# =========================================================
#
# Значок программы рисуется здесь, кодом: синий щит с белой
# галочкой (цвет brand-600 интерфейса). Без сторонних
# картинок — у значка нет чужих авторских прав.
#
#   python tools/make_favicon.py
#
# =========================================================

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "web" / "static" / "favicon.ico"
SIZES = (16, 32, 48)
BLUE = (37, 99, 235)            # #2563eb
WHITE = (255, 255, 255)
SUPERSAMPLE = 6


def _in_shield(x: float, y: float) -> bool:
    if y < 0.06 or y > 0.96:
        return False
    if y <= 0.52:
        # верх щита со скруглёнными углами
        r = 0.10
        left, right, top = 0.12, 0.88, 0.06
        if y < top + r:
            for cx in (left + r, right - r):
                if abs(x - cx) <= r and (x < left + r or x > right - r):
                    return math.hypot(x - cx, y - (top + r)) <= r
        return left <= x <= right
    t = (y - 0.52) / 0.44            # 0 → 1 к острию
    half = 0.38 * (1 - t ** 1.7)
    return abs(x - 0.5) <= half


def _dist_to_segment(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


CHECK = ((0.31, 0.50), (0.45, 0.64), (0.70, 0.36))


def _in_check(x: float, y: float) -> bool:
    (ax, ay), (bx, by), (cx, cy) = CHECK
    return min(
        _dist_to_segment(x, y, ax, ay, bx, by),
        _dist_to_segment(x, y, bx, by, cx, cy),
    ) <= 0.055


def render(size: int) -> bytes:
    """RGBA, строки сверху вниз."""
    rows = []
    n = SUPERSAMPLE
    for j in range(size):
        row = bytearray()
        for i in range(size):
            cover = white = 0
            for sj in range(n):
                for si in range(n):
                    x = (i + (si + 0.5) / n) / size
                    y = (j + (sj + 0.5) / n) / size
                    if _in_shield(x, y):
                        cover += 1
                        if _in_check(x, y):
                            white += 1
            if not cover:
                row += b"\0\0\0\0"
                continue
            w = white / cover
            rgb = [round(BLUE[k] * (1 - w) + WHITE[k] * w) for k in range(3)]
            row += bytes(rgb) + bytes([round(255 * cover / (n * n))])
        rows.append(bytes(row))
    return b"".join(rows)


def png(size: int) -> bytes:
    raw = render(size)
    stride = size * 4
    data = b"".join(b"\0" + raw[k * stride:(k + 1) * stride] for k in range(size))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(data, 9))
            + chunk(b"IEND", b""))


def ico() -> bytes:
    images = [png(s) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, img in zip(SIZES, images):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                               len(img), offset)
        offset += len(img)
    return header + entries + b"".join(images)


if __name__ == "__main__":
    OUT.write_bytes(ico())
    print(f"OK: {OUT} ({OUT.stat().st_size} байт)")
