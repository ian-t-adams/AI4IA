"""Guards the committed brand images against the two ways they silently rot.

`scripts/gen-brand-assets.py` produces these files, but it needs Pillow and a font,
so it deliberately does not run in CI -- the PNGs are committed output. That leaves
two gaps, both of which this suite closes by reading the actual committed bytes
rather than any declaration about them:

1. **Declared size vs real size.** `site/index.html` tells Open Graph consumers the
   lettermark is 1200x630. Nothing checked that against the file, so regenerating at
   a different size (or committing a stale asset) would leave the HTML lying, and
   `twitter:card=summary_large_image` would crop rather than fail.

2. **Weight.** The portal icon was once 1024x1024 / 750 KB behind a 30 px box, and
   the four assets totalled 1.65 MB shipped on every page. Nothing objected, because
   an oversized image is invisible except in the network tab.

PNG dimensions come straight out of the IHDR chunk and ICO dimensions out of the
directory entries, so this stays stdlib-only and offline like the other `quality`
suites -- no Pillow needed to verify Pillow's output.
"""

from __future__ import annotations

import pathlib
import re
import struct
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "site" / "index.html"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Expected (width, height, max_kib) per committed asset. The marks are 128px for a
# ~30px box, which covers up to 4x DPI without shipping a poster. The ceilings sit a
# little above what the generator currently emits: tight enough that a 10x regression
# trips them, loose enough that a font or palette tweak does not.
EXPECTED_PNGS = {
    "app/web/public/ai4ia-mark.png": (128, 128, 20),
    "site/assets/ai4ia-icon.png": (128, 128, 20),
    "site/assets/ai4ia-lettermark.png": (1200, 630, 90),
}

FAVICON = "site/assets/favicon.ico"
FAVICON_MAX_KIB = 24
# Every size a browser or pinned tile actually asks for.
FAVICON_REQUIRED_SIZES = {16, 32, 48}


def png_size(path: pathlib.Path) -> tuple[int, int]:
    """Return (width, height) from a PNG's IHDR chunk.

    Layout is fixed by the spec: 8-byte signature, then a 4-byte length and the
    4-byte chunk type `IHDR`, then width and height as big-endian uint32. IHDR is
    required to be the first chunk, so offset 16 is unconditionally the width.
    """
    raw = path.read_bytes()
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError(f"{path} is not a PNG")
    if raw[12:16] != b"IHDR":
        raise ValueError(f"{path} does not start with an IHDR chunk")
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


def ico_sizes(path: pathlib.Path) -> set[int]:
    """Return the set of square sizes declared in an ICO directory."""
    raw = path.read_bytes()
    reserved, image_type, count = struct.unpack("<HHH", raw[:6])
    if reserved != 0 or image_type != 1:
        raise ValueError(f"{path} is not an ICO")
    sizes: set[int] = set()
    for index in range(count):
        offset = 6 + index * 16
        width = raw[offset]
        height = raw[offset + 1]
        # 0 is the ICO encoding for 256.
        sizes.add((width or 256) if width == height else -1)
    return sizes


class BrandAssetTests(unittest.TestCase):
    def test_every_expected_asset_exists(self) -> None:
        for relative in (*EXPECTED_PNGS, FAVICON):
            with self.subTest(asset=relative):
                self.assertTrue(
                    (ROOT / relative).is_file(),
                    f"{relative} is missing -- run `python scripts/gen-brand-assets.py`",
                )

    def test_pngs_have_the_dimensions_they_are_generated_at(self) -> None:
        for relative, (want_w, want_h, _) in EXPECTED_PNGS.items():
            with self.subTest(asset=relative):
                got_w, got_h = png_size(ROOT / relative)
                self.assertEqual(
                    (got_w, got_h),
                    (want_w, want_h),
                    f"{relative} is {got_w}x{got_h}, expected {want_w}x{want_h}. "
                    "Regenerate with `python scripts/gen-brand-assets.py`, or update "
                    "this expectation AND site/index.html's og:image dimensions.",
                )

    def test_open_graph_declares_the_lettermark_actual_size(self) -> None:
        """The HTML's og:image dimensions must match the file on disk.

        This is the coupling with no other gate: a consumer that trusts the meta
        tags will lay out against them and crop or letterbox if they are wrong.
        """
        html = INDEX_HTML.read_text(encoding="utf-8")
        declared = {}
        for axis in ("width", "height"):
            match = re.search(
                rf'<meta\s+property="og:image:{axis}"\s+content="(\d+)"',
                html,
            )
            self.assertIsNotNone(
                match, f"site/index.html declares no og:image:{axis}"
            )
            assert match is not None  # narrowing for type checkers
            declared[axis] = int(match.group(1))

        actual_w, actual_h = png_size(ROOT / "site/assets/ai4ia-lettermark.png")
        self.assertEqual(
            (declared["width"], declared["height"]),
            (actual_w, actual_h),
            "site/index.html advertises the Open Graph image as "
            f"{declared['width']}x{declared['height']} but the committed file is "
            f"{actual_w}x{actual_h}.",
        )

    def test_open_graph_image_keeps_the_wide_card_ratio(self) -> None:
        """twitter:card=summary_large_image wants ~1.91:1; a square gets cropped."""
        width, height = png_size(ROOT / "site/assets/ai4ia-lettermark.png")
        ratio = width / height
        self.assertAlmostEqual(
            ratio,
            1.91,
            delta=0.06,
            msg=f"Open Graph card is {ratio:.2f}:1; social consumers crop to ~1.91:1.",
        )

    def test_favicon_carries_the_sizes_browsers_ask_for(self) -> None:
        sizes = ico_sizes(ROOT / FAVICON)
        missing = FAVICON_REQUIRED_SIZES - sizes
        self.assertFalse(
            missing,
            f"favicon.ico is missing {sorted(missing)}px entries (has {sorted(sizes)}).",
        )

    def test_assets_stay_small_enough_to_ship_on_every_page(self) -> None:
        """The nav icon and app mark render at ~30px; the portal loads them everywhere."""
        ceilings = {rel: kib for rel, (_, _, kib) in EXPECTED_PNGS.items()}
        ceilings[FAVICON] = FAVICON_MAX_KIB
        for relative, max_kib in ceilings.items():
            with self.subTest(asset=relative):
                kib = (ROOT / relative).stat().st_size / 1024
                self.assertLess(
                    kib,
                    max_kib,
                    f"{relative} is {kib:.0f} KiB, over its {max_kib} KiB ceiling. "
                    "These ship on every portal page for a small rendered box.",
                )

    def test_total_brand_payload_stays_small(self) -> None:
        """Per-file ceilings can all pass while the aggregate creeps back up."""
        total = sum(
            (ROOT / relative).stat().st_size for relative in (*EXPECTED_PNGS, FAVICON)
        )
        self.assertLess(
            total / 1024,
            128,
            f"brand assets total {total / 1024:.0f} KiB (was 1,650 KiB before regeneration).",
        )


if __name__ == "__main__":
    unittest.main()
