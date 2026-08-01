"""Minimal stdlib PNG reader used by the brand-asset gate.

`scripts/gen-brand-assets.py` needs Pillow and a font, so it cannot run in CI --
the rasters are committed output. Verifying that output therefore has to happen
without Pillow, which is why this exists rather than a two-line `Image.open`.

Deliberately narrow: 8-bit non-interlaced truecolour-with-alpha only, which is
what Pillow emits for these assets. Anything else raises instead of guessing, so
a future Pillow that starts writing palette or interlaced PNGs fails loudly
rather than silently reporting the wrong pixels.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_RGBA = 6
_BPP = 4


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_rgba(raw: bytes) -> tuple[int, int, bytearray]:
    """Return (width, height, RGBA bytes) for an 8-bit truecolour+alpha PNG."""
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")

    width, height = struct.unpack(">II", raw[16:24])
    depth, colour, _, _, interlace = raw[24:29]
    if (depth, colour, interlace) != (8, _RGBA, 0):
        raise ValueError(
            f"unsupported PNG: depth={depth} colour={colour} interlace={interlace}; "
            "this reader only handles 8-bit non-interlaced RGBA"
        )

    # Concatenate IDAT payloads. They are allowed to be split arbitrarily, and
    # zlib's stream spans the join, so they must be joined before inflating.
    chunks: list[bytes] = []
    offset = 8
    while offset < len(raw):
        (length,) = struct.unpack(">I", raw[offset : offset + 4])
        kind = raw[offset + 4 : offset + 8]
        if kind == b"IDAT":
            chunks.append(raw[offset + 8 : offset + 8 + length])
        elif kind == b"IEND":
            break
        offset += 12 + length

    data = zlib.decompress(b"".join(chunks))
    stride = width * _BPP
    out = bytearray(height * stride)
    previous = bytearray(stride)
    pos = 0

    for row in range(height):
        filter_type = data[pos]
        line = bytearray(data[pos + 1 : pos + 1 + stride])
        pos += 1 + stride

        if filter_type == 0:
            pass
        elif filter_type == 2:  # Up: no intra-row dependency, so vectorise it.
            line = bytearray(
                (x + b) & 0xFF for x, b in zip(line, previous)
            )
        elif filter_type == 1:  # Sub
            for i in range(_BPP, stride):
                line[i] = (line[i] + line[i - _BPP]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                a = line[i - _BPP] if i >= _BPP else 0
                line[i] = (line[i] + ((a + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                a = line[i - _BPP] if i >= _BPP else 0
                c = previous[i - _BPP] if i >= _BPP else 0
                line[i] = (line[i] + _paeth(a, previous[i], c)) & 0xFF
        else:
            raise ValueError(f"unknown PNG filter type {filter_type}")

        start = row * stride
        out[start : start + stride] = line
        previous = line

    return width, height, out


def hue_share(
    raw: bytes, center: float, tolerance: float
) -> tuple[float, int]:
    """Return (share of saturated pixels near `center`, saturated pixel count).

    A "most common exact RGB triple" metric looks appealing but is noise on these
    assets: the mark is a gradient, so at 16x16 almost every pixel is a unique
    blend and the modal colour is backed by one or two pixels -- a 16 px favicon
    entry reported 327 deg (magenta) purely from anti-aliasing between the orange
    and blue ends. A share across all saturated pixels is stable at every size.

    The count is returned so callers can assert the check was not vacuous.
    """
    _, _, pixels = decode_rgba(raw)
    saturated = 0
    near = 0

    for i in range(0, len(pixels), _BPP):
        r, g, b, a = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
        if a < 200:
            continue
        high, low = max(r, g, b), min(r, g, b)
        if high < 51:
            continue
        delta = high - low
        if delta / high < 0.35:  # saturation
            continue
        saturated += 1
        if r == high:
            hue = ((g - b) / delta) % 6
        elif g == high:
            hue = (b - r) / delta + 2
        else:
            hue = (r - g) / delta + 4
        # Circular distance: hue wraps, and warm tones straddle 0/360.
        if abs((hue * 60.0 - center + 180.0) % 360.0 - 180.0) <= tolerance:
            near += 1

    if saturated == 0:
        return 0.0, 0
    return near / saturated, saturated
