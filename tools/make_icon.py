#!/usr/bin/env python3
"""Draw the home-screen icon as a PNG (iPhones ignore SVG icons).

Standard library only. Writes 1-t_at/assets/icon-192.png: the same water drop
as icon.svg, full-bleed so iOS and Android can round the corners themselves.

    python3 tools/make_icon.py
"""

import math
import os
import struct
import zlib

SIZE = 192          # pixels
SS = 4              # 4 x 4 samples per pixel for smooth edges
BOX = 512.0         # the drawing uses icon.svg's 512 x 512 coordinates

TOP = (0x1a, 0x8c, 0xff)
BOTTOM = (0x0a, 0x57, 0xb8)
CX, CY, R, TIP = 256.0, 310.0, 124.0, 88.0


def half_width(y):
    """Half the drop's width at height y (None outside the drop)."""
    if y < TIP or y > CY + R:
        return None
    if y >= CY:
        return math.sqrt(max(0.0, R * R - (y - CY) ** 2))
    low, high = 0.0, 1.0                       # the cubic curve from the tip to the widest point
    for _ in range(24):
        t = (low + high) / 2.0
        yt = TIP + 3 * (1 - t) ** 2 * t * 82 + 3 * (1 - t) * t * t * 152 + t ** 3 * 222
        if yt < y:
            low = t
        else:
            high = t
    t = (low + high) / 2.0
    return 3 * (1 - t) ** 2 * t * 68 + 3 * (1 - t) * t * t * 124 + t ** 3 * 124


def sample(x, y):
    """Colour at a point of the 512 x 512 drawing."""
    mix = (x + y) / (2 * BOX)
    colour = [TOP[i] + (BOTTOM[i] - TOP[i]) * mix for i in range(3)]
    width = half_width(y)
    if width is not None and abs(x - CX) <= width:
        water_line = 318.0 - 16.0 * math.sin(2 * math.pi * (x - 112.0) / 216.0)
        alpha = 1.0 if y >= water_line else 0.30          # water below the line, glass above
        colour = [c + (255 - c) * alpha for c in colour]
    return colour


def render(size):
    rows = []
    step = BOX / (size * SS)
    for py in range(size):
        row = bytearray([0])                               # PNG filter type 0
        for px in range(size):
            total = [0.0, 0.0, 0.0]
            for sy in range(SS):
                for sx in range(SS):
                    colour = sample((px * SS + sx + 0.5) * step, (py * SS + sy + 0.5) * step)
                    for i in range(3):
                        total[i] += colour[i]
            row.extend(int(round(c / (SS * SS))) for c in total)
        rows.append(bytes(row))
    return b"".join(rows)


def chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def write_png(path, size):
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))     # 8-bit RGB
           + chunk(b"IDAT", zlib.compress(render(size), 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
    return len(png)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = os.path.join(root, "1-t_at", "assets", "icon-192.png")
    print("wrote {} ({} bytes)".format(os.path.relpath(target, root), write_png(target, SIZE)))
