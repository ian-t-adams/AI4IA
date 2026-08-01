"""Guards the committed brand rasters against the ways they silently rot.

`scripts/gen-brand-assets.py` produces these files, but it needs Pillow and a font,
so it deliberately does not run in CI -- the rasters are committed output. This
suite reads the actual committed bytes rather than any declaration about them.

Three failure modes, all of which have actually happened here:

1. **Stale artwork.** The first orange rebrand regenerated four assets and missed
   six, including `app/web/src/app/favicon.ico` -- the web app's browser tab icon --
   and all of `assets/branding/`. Those kept the previous azure mark. Their
   dimensions and weights never changed, so no structural check could have noticed;
   only reading the pixels catches it. Hence `test_every_raster_carries_the_brand_hue`.

2. **An asset nobody remembered.** The miss above was possible because the gate
   only knew about the files the generator happened to write, so an uncovered
   asset was invisible to both. `test_every_committed_raster_is_covered` now
   discovers rasters from git and fails on anything not explicitly listed here or
   in `NON_BRAND_RASTERS`.

3. **Declared size vs real size, and weight.** `site/index.html` tells Open Graph
   consumers the lettermark is 1200x630; nothing checked that against the file.
   Separately the portal icon was once 1024x1024 / 750 KB behind a 30 px box.
   An oversized or mis-declared image is invisible except in the network tab.

Pixel access goes through `_pngread`, a small stdlib PNG reader, so this stays
offline and dependency-free like the other `quality` suites -- no Pillow needed to
verify Pillow's output. Its hues were cross-checked against Pillow and agree to
0.1 degrees.
"""

from __future__ import annotations

import pathlib
import re
import struct
import subprocess
import sys
import unittest

try:  # package import: python -m unittest scripts.tests.test_brand_assets
    from ._pngread import PNG_SIGNATURE, hue_share
except ImportError:  # direct execution: python scripts/tests/test_brand_assets.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from _pngread import PNG_SIGNATURE, hue_share

ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "site" / "index.html"

# Expected (width, height, max_kib) per committed PNG. The marks are sized against
# the box they render in at up to 4x DPI, not "as large as possible". Ceilings sit
# a little above what the generator emits: tight enough that a 10x regression trips
# them, loose enough that a font or palette tweak does not.
EXPECTED_PNGS = {
    "app/web/public/ai4ia-mark.png": (128, 128, 20),
    "app/web/src/app/icon.png": (256, 256, 40),
    "app/web/src/app/apple-icon.png": (180, 180, 24),
    "site/assets/ai4ia-icon.png": (128, 128, 20),
    "site/assets/ai4ia-lettermark.png": (1200, 630, 90),
    "assets/branding/ai4ia-icon-1024.png": (1024, 1024, 200),
    "assets/branding/ai4ia-lettermark.png": (1200, 630, 90),
}

# Expected (required sizes, max_kib) per committed ICO. The web favicons stop at 64
# because a 256 px entry is never displayed in a tab and cost 86 KB; the branding
# .ico is a Windows source (shortcuts, installers) where 256 genuinely is used.
EXPECTED_ICOS = {
    "app/web/src/app/favicon.ico": ({16, 32, 48}, 24),
    "site/assets/favicon.ico": ({16, 32, 48}, 24),
    "assets/branding/ai4ia-icon.ico": ({16, 32, 48, 256}, 64),
}

# Tracked rasters that are deliberately not brand assets (documentation
# screenshots and the like). Empty today; adding an image to the repo should be a
# conscious choice between "this is brand artwork the generator owns" and "this is
# not", which is exactly what the completeness test forces.
NON_BRAND_RASTERS: set[str] = set()

# The brand is orange over near-black; the previous mark was azure at 201 degrees.
# Measured rather than guessed: every current asset and every ICO entry scores a
# 70.2-76.5% warm share, and the azure assets pulled from git history score
# exactly 0.0%. A 40% floor therefore sits ~30 points clear of both boundaries --
# it cannot miss a stale asset, and it will not trip on a palette tweak.
BRAND_HUE_CENTER = 20.0
BRAND_HUE_TOLERANCE = 45.0
MIN_BRAND_HUE_SHARE = 0.40
# A share is meaningless if almost nothing was saturated, so require real
# evidence. The smallest sample in the family is a 16x16 favicon entry, which
# yields ~215 saturated pixels.
MIN_SATURATED_PIXELS = 100

# The four originally-generated assets alone were 1.65 MB before regeneration.
TOTAL_PAYLOAD_MAX_KIB = 512


def tracked_rasters() -> set[str]:
    """Every tracked .png/.ico, as forward-slash repo-relative paths."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "*.png", "*.ico"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {p for p in listing.split("\0") if p}


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


def ico_entries(path: pathlib.Path) -> list[tuple[int, bytes]]:
    """Return [(square_size, embedded image bytes)] from an ICO directory."""
    raw = path.read_bytes()
    reserved, image_type, count = struct.unpack("<HHH", raw[:6])
    if reserved != 0 or image_type != 1:
        raise ValueError(f"{path} is not an ICO")
    entries: list[tuple[int, bytes]] = []
    for index in range(count):
        offset = 6 + index * 16
        width, height = raw[offset], raw[offset + 1]
        if width != height:
            continue
        size, data_offset = struct.unpack("<II", raw[offset + 8 : offset + 16])
        # 0 is the ICO encoding for 256.
        entries.append((width or 256, raw[data_offset : data_offset + size]))
    return entries


def ico_sizes(path: pathlib.Path) -> set[int]:
    return {size for size, _ in ico_entries(path)}


class BrandAssetCoverageTests(unittest.TestCase):
    """The gate that would have caught the missed favicon."""

    def test_every_committed_raster_is_covered(self) -> None:
        covered = set(EXPECTED_PNGS) | set(EXPECTED_ICOS) | NON_BRAND_RASTERS
        uncovered = tracked_rasters() - covered
        self.assertFalse(
            uncovered,
            "these committed images are governed by nothing: "
            f"{sorted(uncovered)}. Either add them to scripts/gen-brand-assets.py "
            "and the expectations above, or list them in NON_BRAND_RASTERS. The "
            "orange rebrand shipped a stale azure favicon precisely because an "
            "asset could exist that no generator owned and no test checked.",
        )

    def test_expectations_do_not_name_missing_files(self) -> None:
        """A typo in a path would otherwise make its checks silently vacuous."""
        for relative in (*EXPECTED_PNGS, *EXPECTED_ICOS):
            with self.subTest(asset=relative):
                self.assertTrue(
                    (ROOT / relative).is_file(),
                    f"{relative} is missing -- run `python scripts/gen-brand-assets.py`",
                )


class BrandAssetColourTests(unittest.TestCase):
    def test_every_raster_carries_the_brand_hue(self) -> None:
        """Catches artwork that was never regenerated.

        The six assets missed by the first rebrand were the right size and the
        right weight; they were simply the wrong colour. Nothing else here would
        have failed. Every ICO entry is checked individually, not just the
        largest, so a partially-refreshed ladder cannot hide in the small sizes.
        """
        targets = [(rel, (ROOT / rel).read_bytes()) for rel in EXPECTED_PNGS]
        for relative in EXPECTED_ICOS:
            for size, blob in ico_entries(ROOT / relative):
                targets.append((f"{relative}[{size}px]", blob))

        for label, raw in targets:
            with self.subTest(asset=label):
                share, saturated = hue_share(
                    raw, BRAND_HUE_CENTER, BRAND_HUE_TOLERANCE
                )
                self.assertGreaterEqual(
                    saturated,
                    MIN_SATURATED_PIXELS,
                    f"{label} has only {saturated} saturated pixels, so the share "
                    "assertion would pass vacuously.",
                )
                self.assertGreaterEqual(
                    share,
                    MIN_BRAND_HUE_SHARE,
                    f"{label}: only {share:.1%} of its saturated pixels are within "
                    f"{BRAND_HUE_TOLERANCE:.0f} deg of the brand hue "
                    f"{BRAND_HUE_CENTER:.0f} deg (floor {MIN_BRAND_HUE_SHARE:.0%}; "
                    "current assets score ~75%, the previous azure mark scores 0%). "
                    "Run `python scripts/gen-brand-assets.py`.",
                )


class BrandAssetShapeTests(unittest.TestCase):
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

    def test_the_two_lettermarks_are_the_same_card(self) -> None:
        """README hero and Open Graph card are one design, generated together."""
        self.assertEqual(
            (ROOT / "site/assets/ai4ia-lettermark.png").read_bytes(),
            (ROOT / "assets/branding/ai4ia-lettermark.png").read_bytes(),
            "the portal and branding lettermarks have diverged; both come from "
            "build_lettermark(1200, 630) in scripts/gen-brand-assets.py.",
        )

    def test_favicons_carry_the_sizes_browsers_ask_for(self) -> None:
        for relative, (required, _) in EXPECTED_ICOS.items():
            with self.subTest(asset=relative):
                sizes = ico_sizes(ROOT / relative)
                missing = required - sizes
                self.assertFalse(
                    missing,
                    f"{relative} is missing {sorted(missing)}px entries "
                    f"(has {sorted(sizes)}).",
                )


class BrandAssetWeightTests(unittest.TestCase):
    def test_assets_stay_small_enough_to_ship_on_every_page(self) -> None:
        """The nav icon and app mark render at ~30px; the portal loads them everywhere."""
        ceilings = {rel: kib for rel, (_, _, kib) in EXPECTED_PNGS.items()}
        ceilings.update({rel: kib for rel, (_, kib) in EXPECTED_ICOS.items()})
        for relative, max_kib in ceilings.items():
            with self.subTest(asset=relative):
                kib = (ROOT / relative).stat().st_size / 1024
                self.assertLess(
                    kib,
                    max_kib,
                    f"{relative} is {kib:.0f} KiB, over its {max_kib} KiB ceiling.",
                )

    def test_total_brand_payload_stays_small(self) -> None:
        """Per-file ceilings can all pass while the aggregate creeps back up."""
        total = sum(
            (ROOT / relative).stat().st_size
            for relative in (*EXPECTED_PNGS, *EXPECTED_ICOS)
        )
        self.assertLess(
            total / 1024,
            TOTAL_PAYLOAD_MAX_KIB,
            f"brand rasters total {total / 1024:.0f} KiB "
            "(3.4 MB across the repo before regeneration).",
        )


if __name__ == "__main__":
    unittest.main()
