# -*- coding: utf-8 -*-
"""用标准库生成程序图标（packaging/app_icon.png 与 .ico）。

不引入 Pillow：ICO 只是给 PNG 加一层 22 字节的文件头，可以直接拼。
用法：python packaging/make_icon.py
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 256
SS = 2  # 超采样倍数

BG = (18, 24, 33)
TILE = (30, 38, 51)
RAIL = (63, 185, 80)
TRAIN = (230, 237, 243)
WINDOW = (88, 166, 255)
RISK = (248, 81, 73)


def _rounded(x, y, w, h, radius):
    if x < 0 or y < 0 or x >= w or y >= h:
        return 0.0
    cx = min(max(x, radius), w - radius)
    cy = min(max(y, radius), h - radius)
    dx, dy = x - cx, y - cy
    return 1.0 if (dx * dx + dy * dy) <= radius * radius else 0.0


def _pixel(x, y):
    """构图：深色圆角底 + 绿色轨道 + 白色车身 + 蓝窗 + 红点（风控告警）。"""
    color = BG
    if _rounded(x, y, SIZE, SIZE, 46):
        color = TILE
    if not _rounded(x - 10, y - 10, SIZE - 20, SIZE - 20, 38):
        return color
    color = TILE

    if _rounded(x - 34, y - 196, 188, 18, 9):
        color = RAIL
    body = _rounded(x - 34, y - 60, 188, 130, 40)
    if body:
        color = TRAIN
    if body and _rounded(x - 62, y - 92, 108, 52, 20):
        color = WINDOW
    dx, dy = x - 178.0, y - 170.0
    if dx * dx + dy * dy <= 330:
        color = RISK
    return color


def render() -> bytes:
    rows = []
    for py in range(SIZE):
        row = bytearray([0])
        for px in range(SIZE):
            r = g = b = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    c = _pixel(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS)
                    r += c[0]
                    g += c[1]
                    b += c[2]
            total = SS * SS
            row += bytes((int(round(r / total)), int(round(g / total)), int(round(b / total))))
        rows.append(bytes(row))
    return b"".join(rows)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_bytes() -> bytes:
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(render(), 9))
        + _chunk(b"IEND", b"")
    )


def ico_bytes(png: bytes) -> bytes:
    """ICO 包一层 PNG（Vista 以后都支持，资源管理器也认）。"""
    width = 0 if SIZE >= 256 else SIZE
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", width, width, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def main() -> int:
    out_dir = Path(__file__).resolve().parent
    png = png_bytes()
    (out_dir / "app_icon.png").write_bytes(png)
    (out_dir / "app_icon.ico").write_bytes(ico_bytes(png))
    print("已生成 %s 与 %s" % (out_dir / "app_icon.png", out_dir / "app_icon.ico"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
