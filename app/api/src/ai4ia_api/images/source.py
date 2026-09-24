"""Bounded, dependency-free inspection of image-edit sources and region masks.

The Azure OpenAI edit operation accepts a PNG or JPEG ``image`` and an optional
PNG ``mask`` that "must have the same dimensions as the image"; fully transparent
(alpha 0) mask pixels mark the region to change. This module reads just enough of
each container to prove that contract before any provider call:

* the format comes from magic bytes, never a filename, content type or URL;
* dimensions come from the PNG ``IHDR`` or the JPEG start-of-frame header, and the
  container structure must be complete (a truncated file is refused);
* EXIF orientation (JPEG ``APP1`` or PNG ``eXIf``) is reported, because a browser
  displays a rotated photo in a different pixel space than the stored bytes, so a
  region drawn over it would not match a mask built in stored-pixel space.

Nothing here decodes pixels. The region mask is written directly as an RGBA PNG,
one filtered scanline at a time through ``zlib``, so memory stays proportional to
one row and the output is exactly the source size by construction.
"""
from __future__ import annotations

import hashlib
import math
import struct
import zlib
from dataclasses import dataclass
from typing import Literal

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SOI = b"\xff\xd8\xff"

# The strictest documented provider bound: Learn states 50 MB, the committed
# 2025-04-01-preview OpenAPI states 25 MB and an earlier Learn revision 20 MB.
MAX_EDIT_SOURCE_BYTES = 20_000_000
# Provider: the mask must be "a valid PNG file, less than 4MB".
MAX_EDIT_MASK_BYTES = 4_000_000
# Application bounds, not provider limits: they bound mask construction cost.
MAX_EDIT_EDGE = 8192
MAX_EDIT_PIXELS = 4096 * 4096
# Structural scan bounds so a hostile file cannot make the parser loop for long.
_MAX_PNG_CHUNKS = 65_536
_MAX_JPEG_SEGMENTS = 4096
_MAX_EXIF_ENTRIES = 512
_EXIF_ORIENTATION_TAG = 0x0112
# Mask pixels (R, G, B, A): opaque keeps the source, alpha 0 marks the edit.
_KEEP_PIXEL = b"\x00\x00\x00\xff"
_EDIT_PIXEL = b"\x00\x00\x00\x00"

ImageContentType = Literal["image/png", "image/jpeg"]


class ImageSourceError(ValueError):
    """A sanitized refusal with the HTTP status the caller should surface."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ImageInfo:
    content_type: ImageContentType
    width: int
    height: int
    # EXIF orientation when the container declares one (1 is upright).
    orientation: int | None = None

    @property
    def rotated(self) -> bool:
        return self.orientation not in (None, 1)


@dataclass(frozen=True)
class EditRegion:
    """A rectangle as fractions of the image: ``x``/``y`` from the top-left."""

    x: float
    y: float
    width: float
    height: float


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inspect_image(data: bytes) -> ImageInfo:
    """Sniff, measure and structurally verify one edit source image."""
    if not data:
        raise ImageSourceError(422, "The source image is empty.")
    if len(data) > MAX_EDIT_SOURCE_BYTES:
        raise ImageSourceError(
            413, f"The source image is larger than {MAX_EDIT_SOURCE_BYTES // 1_000_000} MB."
        )
    if data.startswith(PNG_SIGNATURE):
        width, height, _, _, orientation = _png_header(data)
        content_type: ImageContentType = "image/png"
    elif data.startswith(JPEG_SOI):
        width, height, orientation = _jpeg_header(data)
        content_type = "image/jpeg"
    else:
        raise ImageSourceError(422, "Only PNG and JPEG images can be edited.")
    if width < 1 or height < 1:
        raise ImageSourceError(422, "The source image has no pixels.")
    if width > MAX_EDIT_EDGE or height > MAX_EDIT_EDGE or width * height > MAX_EDIT_PIXELS:
        raise ImageSourceError(
            422,
            f"The source image must be at most {MAX_EDIT_EDGE} pixels per edge and "
            f"{MAX_EDIT_PIXELS} pixels in total.",
        )
    return ImageInfo(content_type, width, height, orientation)


def region_box(region: EditRegion, width: int, height: int) -> tuple[int, int, int, int]:
    """Convert a fractional region to a non-empty ``(x0, y0, x1, y1)`` pixel box."""
    values = (region.x, region.y, region.width, region.height)
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in values
    ):
        raise ImageSourceError(422, "The edit region must use finite numbers.")
    tolerance = 1e-9
    if (
        region.x < 0 or region.y < 0 or region.width <= 0 or region.height <= 0
        or region.x + region.width > 1 + tolerance
        or region.y + region.height > 1 + tolerance
    ):
        raise ImageSourceError(
            422, "The edit region must lie inside the image and have a positive size."
        )
    x0 = min(width, math.floor(region.x * width))
    y0 = min(height, math.floor(region.y * height))
    x1 = min(width, math.ceil((region.x + region.width) * width - tolerance))
    y1 = min(height, math.ceil((region.y + region.height) * height - tolerance))
    if x1 <= x0 or y1 <= y0:
        raise ImageSourceError(422, "The edit region must cover at least one pixel.")
    return x0, y0, x1, y1


def build_region_mask(width: int, height: int, box: tuple[int, int, int, int]) -> bytes:
    """An RGBA PNG exactly ``width``x``height``: transparent inside ``box`` only."""
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ImageSourceError(422, "The edit region is outside the image.")
    keep_row = b"\x00" + _KEEP_PIXEL * width
    edit_row = (
        b"\x00" + _KEEP_PIXEL * x0 + _EDIT_PIXEL * (x1 - x0) + _KEEP_PIXEL * (width - x1)
    )
    compressor = zlib.compressobj(6)
    parts: list[bytes] = []
    for row in range(height):
        parts.append(compressor.compress(edit_row if y0 <= row < y1 else keep_row))
    parts.append(compressor.flush())
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    mask = (
        PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", b"".join(parts))
        + _chunk(b"IEND", b"")
    )
    validate_mask(mask, width, height)
    return mask


def validate_mask(mask: bytes, width: int, height: int) -> None:
    """Re-check the provider's mask contract immediately before dispatch."""
    if not mask.startswith(PNG_SIGNATURE):
        raise ImageSourceError(422, "The edit mask must be a PNG image.")
    if len(mask) >= MAX_EDIT_MASK_BYTES:
        raise ImageSourceError(422, "The edit mask must be smaller than 4 MB.")
    mask_width, mask_height, bit_depth, color_type, _ = _png_header(mask)
    if (mask_width, mask_height) != (width, height):
        raise ImageSourceError(422, "The edit mask must match the image dimensions.")
    if bit_depth != 8 or color_type not in (4, 6):
        raise ImageSourceError(422, "The edit mask must carry an alpha channel.")


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data)) + kind + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png_header(data: bytes) -> tuple[int, int, int, int, int | None]:
    """``(width, height, bit_depth, color_type, orientation)`` of a complete PNG."""
    position = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int] | None = None
    orientation: int | None = None
    saw_data = False
    for index in range(_MAX_PNG_CHUNKS):
        if position + 12 > len(data):
            break
        (length,) = struct.unpack(">I", data[position:position + 4])
        kind = data[position + 4:position + 8]
        start = position + 8
        end = start + length
        if length > len(data) or end + 4 > len(data):
            break
        body = data[start:end]
        if index == 0:
            (crc,) = struct.unpack(">I", data[end:end + 4])
            if kind != b"IHDR" or length != 13 or zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
                break
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
            header = (width, height, bit_depth, color_type)
        elif kind == b"IDAT":
            saw_data = True
        elif kind == b"eXIf" and not saw_data:
            orientation = _exif_orientation(body[6:] if body.startswith(b"Exif\x00\x00") else body)
        elif kind == b"IEND":
            if header is None or not saw_data:
                break
            return (*header, orientation)
        position = end + 4
    raise ImageSourceError(422, "The PNG image is truncated or malformed.")


def _jpeg_header(data: bytes) -> tuple[int, int, int | None]:
    """``(width, height, orientation)`` of a JPEG with a frame and scan data."""
    position = 2
    size: tuple[int, int] | None = None
    orientation: int | None = None
    for _ in range(_MAX_JPEG_SEGMENTS):
        while position < len(data) and data[position] == 0xFF and (
            position + 1 < len(data) and data[position + 1] == 0xFF
        ):
            position += 1
        if position + 4 > len(data) or data[position] != 0xFF:
            break
        marker = data[position + 1]
        if marker in (0x01, *range(0xD0, 0xD8)):
            position += 2
            continue
        if marker in (0xD8, 0xD9):
            break
        (length,) = struct.unpack(">H", data[position + 2:position + 4])
        start = position + 4
        end = position + 2 + length
        if length < 2 or end > len(data):
            break
        segment = data[start:end]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00") and orientation is None:
            orientation = _exif_orientation(segment[6:])
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if len(segment) < 5:
                break
            height, width = struct.unpack(">HH", segment[1:5])
            size = (width, height)
        elif marker == 0xDA:
            # Entropy-coded data follows; a complete file still ends with EOI.
            if size is None or data.rfind(b"\xff\xd9") < end:
                break
            return (*size, orientation)
        position = end
    raise ImageSourceError(422, "The JPEG image is truncated or malformed.")


def _exif_orientation(tiff: bytes) -> int | None:
    """Read IFD0 ``Orientation`` from a TIFF-structured EXIF block, if present."""
    if len(tiff) < 8:
        return None
    order = tiff[:2]
    if order == b"II":
        prefix = "<"
    elif order == b"MM":
        prefix = ">"
    else:
        return None
    magic, offset = struct.unpack(prefix + "HI", tiff[2:8])
    if magic != 42 or offset + 2 > len(tiff):
        return None
    (count,) = struct.unpack(prefix + "H", tiff[offset:offset + 2])
    for entry in range(min(count, _MAX_EXIF_ENTRIES)):
        at = offset + 2 + entry * 12
        if at + 12 > len(tiff):
            return None
        tag, kind, number = struct.unpack(prefix + "HHI", tiff[at:at + 8])
        if tag == _EXIF_ORIENTATION_TAG:
            if kind != 3 or number != 1:
                return None
            (value,) = struct.unpack(prefix + "H", tiff[at + 8:at + 10])
            return value if 1 <= value <= 8 else None
    return None
