"""Edit-source inspection and region masks, proved against real container bytes.

Every refusal is paired with the same fixture made valid, so each check is shown
to be the reason a defect fails rather than an unrelated parse error.
"""
from __future__ import annotations

import math
import struct
import zlib

import pytest

from ai4ia_api.images.source import (
    MAX_EDIT_EDGE,
    MAX_EDIT_MASK_BYTES,
    MAX_EDIT_SOURCE_BYTES,
    EditRegion,
    ImageSourceError,
    build_region_mask,
    inspect_image,
    region_box,
    validate_mask,
)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(
        ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
    )


def png(width: int = 4, height: int = 3, *, color_type: int = 2, exif: bytes | None = None,
        iend: bool = True, idat: bool = True, crc_ok: bool = True,
        pixels: bool = True) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    ihdr = _chunk(b"IHDR", header)
    if not crc_ok:
        ihdr = ihdr[:-1] + bytes([ihdr[-1] ^ 0xFF])
    channels = {2: 3, 6: 4, 4: 2, 0: 1}[color_type]
    # The inspector never inflates IDAT, so huge-dimension fixtures skip pixels.
    raw = (
        b"".join(b"\x00" + b"\x7f" * (width * channels) for _ in range(height))
        if pixels else b"\x00"
    )
    body = b"\x89PNG\r\n\x1a\n" + ihdr
    if exif is not None:
        body += _chunk(b"eXIf", exif)
    if idat:
        body += _chunk(b"IDAT", zlib.compress(raw))
    if iend:
        body += _chunk(b"IEND", b"")
    return body


def tiff_orientation(value: int, *, big_endian: bool = False) -> bytes:
    prefix = ">" if big_endian else "<"
    order = b"MM" if big_endian else b"II"
    entry = struct.pack(prefix + "HHIHH", 0x0112, 3, 1, value, 0)
    return order + struct.pack(prefix + "HI", 42, 8) + struct.pack(prefix + "H", 1) + entry + b"\x00" * 4


def jpeg(width: int = 640, height: int = 480, *, orientation: int | None = None,
         sof: int = 0xC0, eoi: bool = True, frame: bool = True) -> bytes:
    def segment(marker: int, payload: bytes) -> bytes:
        return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload

    data = b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00")
    if orientation is not None:
        data += segment(0xE1, b"Exif\x00\x00" + tiff_orientation(orientation, big_endian=True))
    if frame:
        data += segment(sof, bytes([8]) + struct.pack(">HH", height, width) + b"\x03" + b"\x01\x11\x00" * 3)
    data += segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00") + b"\x12\x34" * 32
    if eoi:
        data += b"\xff\xd9"
    return data


def _decode_rgba(mask: bytes) -> tuple[int, int, list[bytes]]:
    position, idat = 8, b""
    width = height = 0
    while position < len(mask):
        (length,) = struct.unpack(">I", mask[position:position + 4])
        kind = mask[position + 4:position + 8]
        body = mask[position + 8:position + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            assert (depth, color) == (8, 6)
        elif kind == b"IDAT":
            idat += body
        position += 12 + length
    raw = zlib.decompress(idat)
    stride = 1 + width * 4
    rows = [raw[i * stride:(i + 1) * stride] for i in range(height)]
    assert all(row[0] == 0 for row in rows)
    return width, height, [row[1:] for row in rows]


def test_png_and_jpeg_dimensions_come_from_their_headers():
    info = inspect_image(png(37, 21))
    assert (info.content_type, info.width, info.height, info.orientation) == ("image/png", 37, 21, None)
    for marker in (0xC0, 0xC2):
        info = inspect_image(jpeg(1536, 1024, sof=marker))
        assert (info.content_type, info.width, info.height) == ("image/jpeg", 1536, 1024)
        assert not info.rotated


@pytest.mark.parametrize(
    "payload",
    [
        b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24,
        b"GIF89a" + b"\x00" * 32,
        b"II*\x00" + b"\x00" * 32,
        b"not an image at all",
    ],
    ids=["webp", "gif", "tiff", "text"],
)
def test_only_png_and_jpeg_are_accepted(payload):
    with pytest.raises(ImageSourceError, match="Only PNG and JPEG") as caught:
        inspect_image(payload)
    assert caught.value.status_code == 422
    assert inspect_image(png()).content_type == "image/png"


@pytest.mark.parametrize(
    ("broken", "match"),
    [
        (png(iend=False), "PNG image is truncated"),
        (png(idat=False), "PNG image is truncated"),
        (png(crc_ok=False), "PNG image is truncated"),
        (png()[:-6], "PNG image is truncated"),
        (jpeg(eoi=False), "JPEG image is truncated"),
        (jpeg(frame=False), "JPEG image is truncated"),
        (jpeg()[:40], "JPEG image is truncated"),
    ],
    ids=["png-no-iend", "png-no-idat", "png-bad-ihdr-crc", "png-cut", "jpeg-no-eoi",
         "jpeg-no-frame", "jpeg-cut"],
)
def test_truncated_or_malformed_containers_are_refused(broken, match):
    with pytest.raises(ImageSourceError, match=match):
        inspect_image(broken)
    assert inspect_image(png()).width == 4 and inspect_image(jpeg()).width == 640


def test_size_and_dimension_caps():
    padded = png() + b"\x00" * (MAX_EDIT_SOURCE_BYTES - len(png()) + 1)
    with pytest.raises(ImageSourceError) as caught:
        inspect_image(padded)
    assert caught.value.status_code == 413
    assert inspect_image(png(MAX_EDIT_EDGE, 2048, pixels=False)).width == MAX_EDIT_EDGE
    for width, height in ((MAX_EDIT_EDGE + 1, 1), (4097, 4096)):
        with pytest.raises(ImageSourceError, match="at most"):
            inspect_image(png(width, height, pixels=False))
    with pytest.raises(ImageSourceError, match="no pixels"):
        inspect_image(png(0, 5, pixels=False))


@pytest.mark.parametrize("big_endian", [False, True])
def test_exif_orientation_is_reported_for_png_and_jpeg(big_endian):
    rotated = inspect_image(png(exif=tiff_orientation(6, big_endian=big_endian)))
    assert rotated.orientation == 6 and rotated.rotated
    upright = inspect_image(png(exif=tiff_orientation(1, big_endian=big_endian)))
    assert upright.orientation == 1 and not upright.rotated
    assert inspect_image(jpeg(orientation=8)).rotated
    assert not inspect_image(jpeg(orientation=1)).rotated
    assert not inspect_image(jpeg()).rotated


def test_region_mask_is_exactly_transparent_inside_the_region():
    width, height = 40, 30
    box = region_box(EditRegion(x=0.25, y=0.5, width=0.5, height=0.25), width, height)
    assert box == (10, 15, 30, 23)
    mask = build_region_mask(width, height, box)
    assert len(mask) < MAX_EDIT_MASK_BYTES
    decoded_width, decoded_height, rows = _decode_rgba(mask)
    assert (decoded_width, decoded_height) == (width, height)
    for y, row in enumerate(rows):
        for x in range(width):
            alpha = row[x * 4 + 3]
            inside = 10 <= x < 30 and 15 <= y < 23
            assert alpha == (0 if inside else 255), (x, y)
    validate_mask(mask, width, height)


def test_a_tiny_region_still_covers_one_pixel_and_a_full_region_covers_all():
    assert region_box(EditRegion(0.999, 0.999, 0.001, 0.001), 100, 100) == (99, 99, 100, 100)
    assert region_box(EditRegion(0.0, 0.0, 1.0, 1.0), 1024, 1536) == (0, 0, 1024, 1536)
    # Floating-point sums that land a hair above 1 are the whole image, not a refusal.
    assert region_box(EditRegion(0.7, 0.1, 0.3, 0.9), 10, 10) == (7, 1, 10, 10)


@pytest.mark.parametrize(
    "region",
    [
        EditRegion(-0.1, 0, 0.5, 0.5),
        EditRegion(0, 0, 0, 0.5),
        EditRegion(0, 0, 0.5, -0.5),
        EditRegion(0.6, 0, 0.5, 0.5),
        EditRegion(0, 0.9, 0.5, 0.2),
        EditRegion(math.nan, 0, 0.5, 0.5),
        EditRegion(0, 0, math.inf, 0.5),
        EditRegion(True, 0, 0.5, 0.5),  # type: ignore[arg-type]
    ],
    ids=["negative", "zero-width", "negative-height", "past-right", "past-bottom", "nan",
         "inf", "bool"],
)
def test_invalid_regions_are_refused(region):
    with pytest.raises(ImageSourceError) as caught:
        region_box(region, 64, 64)
    assert caught.value.status_code == 422
    assert region_box(EditRegion(0.1, 0.1, 0.5, 0.5), 64, 64) == (6, 6, 39, 39)


def test_mask_contract_is_rechecked_before_dispatch():
    good = build_region_mask(8, 6, (1, 1, 4, 4))
    validate_mask(good, 8, 6)
    with pytest.raises(ImageSourceError, match="match the image dimensions"):
        validate_mask(good, 6, 8)
    with pytest.raises(ImageSourceError, match="alpha channel"):
        validate_mask(png(8, 6, color_type=2), 8, 6)
    validate_mask(png(8, 6, color_type=4), 8, 6)
    with pytest.raises(ImageSourceError, match="must be a PNG"):
        validate_mask(jpeg(8, 6), 8, 6)
    with pytest.raises(ImageSourceError, match="smaller than 4 MB"):
        validate_mask(good + b"\x00" * MAX_EDIT_MASK_BYTES, 8, 6)
    with pytest.raises(ImageSourceError, match="outside the image"):
        build_region_mask(8, 6, (0, 0, 9, 6))
