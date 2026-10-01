# -*- coding: utf-8 -*-
"""生成面板图标（纯标准库手写 PNG 编码器，不引入 Pillow、不提交二进制资源）。

设计：深色圆角底 + 绿色信号点 + 白色车头轮廓，风格与面板深色主题一致。
用法：python tools/make_favicon.py  ->  webpanel/ui/favicon.png
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 64
SS = 4  # 超采样倍数，用于抗锯齿

BG = (13, 17, 23)
CARD = (22, 27, 34)
BORDER = (48, 54, 61)
SIGNAL = (63, 185, 80)
TRAIN = (230, 237, 243)
WINDOW = (88, 166, 255)


def _blend(base, top, alpha):
    return tuple(int(round(base[i] * (1 - alpha) + top[i] * alpha)) for i in range(3))


def _rounded_rect_alpha(x, y, w, h, radius):
    """返回 (x, y) 在圆角矩形内的覆盖比例（0/1，硬边——由超采样负责抗锯齿）。"""
    if x < 0 or y < 0 or x >= w or y >= h:
        return 0.0
    cx = min(max(x, radius), w - radius)
    cy = min(max(y, radius), h - radius)
    dx, dy = x - cx, y - cy
    return 1.0 if (dx * dx + dy * dy) <= radius * radius else 0.0


def _pixel(x, y):
    """按超采样点坐标计算颜色。坐标范围 0..SIZE。

    构图（64x64）：深色圆角底 -> 绿色地平线 -> 白色列车车身 -> 蓝色车窗 -> 两个车灯。
    元素之间保留 4px 内边距，避免贴边看起来"糊"。
    """
    color = BG
    if _rounded_rect_alpha(x, y, SIZE, SIZE, 13):
        color = CARD
    inner = _rounded_rect_alpha(x - 2, y - 2, SIZE - 4, SIZE - 4, 11)
    if not inner:
        # 只在圆角底的边缘上，未被内层覆盖 -> 描边
        return BORDER if _rounded_rect_alpha(x, y, SIZE, SIZE, 13) else color
    color = CARD

    # 地平线（绿色轨道）
    if _rounded_rect_alpha(x - 9, y - 49, 46, 5, 2.5):
        color = SIGNAL

    # 车身
    body_alpha = _rounded_rect_alpha(x - 9, y - 13, 46, 34, 11)
    if body_alpha:
        color = TRAIN

    # 车窗（车身之上，两侧各留 9px 边框）
    if body_alpha and _rounded_rect_alpha(x - 18, y - 20, 28, 13, 5):
        color = WINDOW

    # 车灯：绿色圆点
    for lamp_x in (21.0, 43.0):
        dx, dy = x - lamp_x, y - 42.5
        if dx * dx + dy * dy <= 6.5:
            color = SIGNAL
    return color


def render() -> bytes:
    rows = []
    for py in range(SIZE):
        row = bytearray([0])  # filter type 0
        for px in range(SIZE):
            r = g = b = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    x = px + (sx + 0.5) / SS
                    y = py + (sy + 0.5) / SS
                    c = _pixel(x, y)
                    r += c[0]
                    g += c[1]
                    b += c[2]
            total = SS * SS
            row += bytes((int(round(r / total)), int(round(g / total)), int(round(b / total))))
        rows.append(bytes(row))
    return b"".join(rows)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, raw: bytes) -> None:
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)  # 8-bit truecolor
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> int:
    target = Path(__file__).resolve().parent.parent / "py12306" / "panel" / "ui" / "favicon.png"
    write_png(target, render())
    print("已生成 %s（%d 字节）" % (target, target.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
