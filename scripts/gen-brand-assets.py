#!/usr/bin/env python3
"""Regenerate the AI4IA brand assets (app mark, portal icon, OG lettermark, favicon).

Brand palette is orange -> blue, the complementary pair, over near-black. The
authoritative *interface* tokens live in `app/web/src/app/globals.css`; the
values here are the DECORATIVE brand colours (globals.css `--brand`/`--brand-2`),
which are deliberately more saturated than `--accent` because they are never used
as text. Keep the two in step by hand -- a gradient cannot be expressed as a CSS
custom property, so there is no generator that can check it for us.

Run after changing the palette:

    python scripts/gen-brand-assets.py

Outputs are committed. This is NOT wired into CI: it needs Pillow plus a bold
sans TTF, and re-encoding a PNG is not byte-reproducible across Pillow versions,
so a `--check` mode would fail for reasons unrelated to the artwork.

Sizes are chosen against how each asset is actually rendered, not "as big as
possible" -- the previous portal icon was 1024x1024 (750 KB) behind a 30 px CSS
box on six pages.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover - operator-facing guidance
    sys.exit("Pillow is required: python -m pip install Pillow")

REPO = pathlib.Path(__file__).resolve().parent.parent

# globals.css --brand (light) -> --brand-2 (light).
ORANGE = (234, 88, 12)
BLUE = (29, 78, 216)
# A touch of the dark theme's brighter orange keeps the top-left corner from
# reading muddy at 30 px, mirroring the highlight in the original mark.
ORANGE_HI = (251, 146, 60)
NEAR_BLACK = (11, 13, 18)

# Bold sans, in preference order. Windows first (this is where it is run), then
# common Linux packages so a contributor on CI-like tooling can still regenerate.
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if pathlib.Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    sys.exit(
        "No bold sans TTF found. Install DejaVu/Liberation fonts or add a path "
        "to FONT_CANDIDATES."
    )


def diagonal_gradient(size: tuple[int, int]) -> Image.Image:
    """Orange (top-left) -> blue (bottom-right).

    Built at 1/8 scale and upscaled: a smooth gradient has no high-frequency
    detail, so this is visually identical to per-pixel work at full size and
    fast enough to stay a plain nested loop rather than a numpy dependency.
    """
    w, h = size
    small = Image.new("RGB", (max(w // 8, 2), max(h // 8, 2)))
    sw, sh = small.size
    pixels = small.load()
    for y in range(sh):
        for x in range(sw):
            # Normalised distance along the top-left -> bottom-right diagonal.
            t = (x / max(sw - 1, 1) + y / max(sh - 1, 1)) / 2
            # The highlight occupies only the first third: a diagonal ramp spends
            # most of its area near t=0.5, so an even split renders as "orange
            # with a blue corner" rather than the intended two-colour brand.
            if t < 0.32:
                u = t / 0.32
                start, end = ORANGE_HI, ORANGE
            else:
                u = (t - 0.32) / 0.68
                start, end = ORANGE, BLUE
            pixels[x, y] = tuple(
                round(start[i] + (end[i] - start[i]) * u) for i in range(3)
            )
    return small.resize(size, Image.LANCZOS)


def squircle_mask(size: tuple[int, int], radius: int) -> Image.Image:
    """Anti-aliased rounded-rect mask (drawn 4x then downsampled)."""
    w, h = size
    mask = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * 4 - 1, h * 4 - 1), radius=radius * 4, fill=255
    )
    return mask.resize(size, Image.LANCZOS)


def draw_wordmark(
    canvas: Image.Image, text: str, font: ImageFont.FreeTypeFont, center: tuple[int, int]
) -> None:
    """White wordmark with a soft glow, matching the original mark's treatment."""
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text(center, text, font=font, fill=(255, 255, 255, 170), anchor="mm")
    canvas.alpha_composite(
        glow.filter(ImageFilter.GaussianBlur(max(canvas.size[0] // 80, 2)))
    )
    ImageDraw.Draw(canvas).text(center, text, font=font, fill=(255, 255, 255, 255), anchor="mm")


def build_mark(px: int) -> Image.Image:
    """The square app/portal mark: gradient squircle + AI4IA."""
    base = diagonal_gradient((px, px)).convert("RGBA")
    base.putalpha(squircle_mask((px, px), radius=round(px * 0.22)))
    # Fit the wordmark to ~78% of the width regardless of the chosen font's metrics.
    size = px // 4
    font = load_font(size)
    while font.getbbox("AI4IA")[2] > px * 0.78 and size > 6:
        size -= 1
        font = load_font(size)
    draw_wordmark(base, "AI4IA", font, (px // 2, px // 2))
    return base


def build_lettermark(width: int, height: int) -> Image.Image:
    """Open Graph card: mark on the left, wordmark + tagline on the right.

    1200x630 is the ratio Open Graph consumers crop to; the previous asset was
    1024x1024, so social previews were centre-cropping the artwork.
    """
    card = Image.new("RGBA", (width, height), NEAR_BLACK + (255,))
    mark_px = round(height * 0.52)
    mark = build_mark(mark_px)
    mark_x = round(width * 0.10)
    card.alpha_composite(mark, (mark_x, (height - mark_px) // 2))

    text_x = mark_x + mark_px + round(width * 0.05)
    title_font = load_font(round(height * 0.17))
    sub_font = load_font(round(height * 0.055))
    draw = ImageDraw.Draw(card)
    draw.text((text_x, height // 2 - round(height * 0.06)), "AI4IA", font=title_font,
              fill=(255, 255, 255, 255), anchor="lm")
    draw.text((text_x, height // 2 + round(height * 0.09)),
              "Governed multi-model AI on Azure", font=sub_font,
              fill=ORANGE_HI + (255,), anchor="lm")
    return card


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be written"
    )
    args = parser.parse_args()

    # (path, builder, note). Sizes track the rendered box at up to 4x DPI.
    targets = [
        (REPO / "app/web/public/ai4ia-mark.png", lambda: build_mark(128),
         "sidebar/sign-in mark, rendered 28 px"),
        (REPO / "site/assets/ai4ia-icon.png", lambda: build_mark(128),
         "portal nav brand, rendered 30 px"),
        (REPO / "site/assets/ai4ia-lettermark.png", lambda: build_lettermark(1200, 630),
         "Open Graph preview card"),
    ]

    for path, build, note in targets:
        before = path.stat().st_size if path.exists() else 0
        if args.dry_run:
            print(f"would write {path.relative_to(REPO)} ({note})")
            continue
        image = build()
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, "PNG", optimize=True)
        after = path.stat().st_size
        print(
            f"wrote {str(path.relative_to(REPO)):40} {image.size[0]}x{image.size[1]:<5} "
            f"{before / 1024:8.1f} KB -> {after / 1024:6.1f} KB   ({note})"
        )

    # Favicons carry their own size ladder; browsers pick the nearest. 256x256 in
    # a favicon is never displayed and cost 86 KB.
    ico = REPO / "site/assets/favicon.ico"
    if args.dry_run:
        print(f"would write {ico.relative_to(REPO)} (16/32/48/64 ladder)")
    else:
        before = ico.stat().st_size if ico.exists() else 0
        build_mark(64).save(ico, "ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
        print(
            f"wrote {str(ico.relative_to(REPO)):40} 16/32/48/64  "
            f"{before / 1024:8.1f} KB -> {ico.stat().st_size / 1024:6.1f} KB   (favicon ladder)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
