"""WCAG contrast gate for the GitHub Pages portal palette (site/assets/styles.css).

The app's own palette is gated by `app/web/src/app/globals.contrast.test.ts` under
vitest. The portal is a static site with no build and no test runner, so its
stylesheet had no gate at all -- and it had drifted: in light mode `--brand-2`
(used for eyebrows, inline code and group titles, i.e. TEXT) sat at 2.80:1, the
`.btn.primary` / `.nav-cta` gradients put dark text on `--brand` at 3.94:1, and
`--ok`/`--warn` status text sat at ~3.15:1. All four are below AA and none of
them fails loudly -- the page just renders, slightly illegibly.

Both brand tokens do double duty (TEXT on --bg, and gradient FILL under
--on-brand), so each is asserted in both directions for both colour schemes.

stdlib-only and offline, matching the other suites run by the `quality` workflow.
"""

from __future__ import annotations

import pathlib
import re
import unittest

STYLES = pathlib.Path(__file__).resolve().parents[2] / "site" / "assets" / "styles.css"

AA_TEXT = 4.5
# WCAG 1.4.11: non-text UI (focus rings, borders) needs 3:1, not 4.5:1.
AA_NON_TEXT = 3.0

# Rendered as body text somewhere in the portal, so held to 1.4.3.
TEXT_TOKENS = ("brand", "brand-2", "accent", "ok", "warn", "bad", "muted", "text")
# Used as a gradient fill under --on-brand (.btn.primary, .nav-cta, .skip-link).
FILL_TOKENS = ("brand", "brand-2")


def _channel(value: float) -> float:
    value /= 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    r, g, b = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _scheme_blocks(css: str) -> dict[str, str]:
    """Split the stylesheet into its dark (:root) and light (@media) token blocks."""
    dark = re.search(r":root\s*\{([\s\S]*?)\}", css)
    light = re.search(
        r"@media \(prefers-color-scheme: light\)\s*\{\s*:root\s*\{([\s\S]*?)\}", css
    )
    assert dark and light, "styles.css no longer has both :root and a light @media block"
    return {"dark": dark.group(1), "light": light.group(1)}


class PortalContrastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = STYLES.read_text(encoding="utf8")
        cls.blocks = _scheme_blocks(cls.css)

    def token(self, scheme: str, name: str) -> str:
        match = re.search(
            rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})\s*;", self.blocks[scheme]
        )
        self.assertIsNotNone(match, f"{scheme} scheme does not define --{name}")
        return match.group(1).lower()

    def test_text_tokens_meet_aa_against_the_page_background(self) -> None:
        for scheme in ("dark", "light"):
            background = self.token(scheme, "bg")
            for name in TEXT_TOKENS:
                with self.subTest(scheme=scheme, token=name):
                    ratio = contrast(self.token(scheme, name), background)
                    self.assertGreaterEqual(
                        ratio,
                        AA_TEXT,
                        f"--{name} ({self.token(scheme, name)}) on --bg ({background}) "
                        f"in {scheme} is {ratio:.2f}:1",
                    )

    def test_on_brand_is_legible_on_both_gradient_stops(self) -> None:
        """`.btn.primary` and `.nav-cta` fill with brand -> brand-2, so BOTH ends
        of the gradient carry --on-brand. Checking only one end passes while half
        the button is unreadable."""
        for scheme in ("dark", "light"):
            on_brand = self.token(scheme, "on-brand")
            for name in FILL_TOKENS:
                with self.subTest(scheme=scheme, stop=name):
                    ratio = contrast(self.token(scheme, name), on_brand)
                    self.assertGreaterEqual(
                        ratio,
                        AA_TEXT,
                        f"--on-brand ({on_brand}) on the --{name} gradient stop "
                        f"in {scheme} is {ratio:.2f}:1",
                    )

    def test_panel_surfaces_stay_distinguishable_from_the_page(self) -> None:
        """Cards are separated from the page by fill, not just by border, so the
        surfaces must not collapse into each other."""
        for scheme in ("dark", "light"):
            with self.subTest(scheme=scheme):
                self.assertNotEqual(
                    self.token(scheme, "panel"), self.token(scheme, "bg")
                )
                ratio = contrast(self.token(scheme, "border"), self.token(scheme, "bg"))
                self.assertGreaterEqual(
                    ratio,
                    1.2,
                    f"--border is invisible against --bg in {scheme} ({ratio:.2f}:1)",
                )

    def test_focus_ring_is_visible_on_the_page_background(self) -> None:
        """`:focus-visible` outlines with --brand; keyboard users lose their
        position entirely if it does not stand off the background."""
        self.assertIn("outline: 3px solid var(--brand)", self.css)
        for scheme in ("dark", "light"):
            with self.subTest(scheme=scheme):
                ratio = contrast(self.token(scheme, "brand"), self.token(scheme, "bg"))
                self.assertGreaterEqual(ratio, AA_NON_TEXT)

    def test_on_brand_is_a_token_rather_than_a_literal(self) -> None:
        """The value has to invert between schemes (near-black on the dark
        scheme's bright orange, white on the light scheme's deep orange).
        Hardcoding it is what left the light-mode button gradient at 3.94:1."""
        # Matching then reporting only the offending fragment: assertNotRegex
        # would dump the entire stylesheet into the failure message.
        literal = re.search(r"var\(--brand-2\)\); color: (#[0-9a-fA-F]{3,6})", self.css)
        self.assertIsNone(
            literal,
            "gradient fills must take their foreground from var(--on-brand), "
            f"found the literal {literal.group(1) if literal else ''}",
        )
        self.assertGreaterEqual(self.css.count("var(--on-brand)"), 3)

    def test_the_palette_is_orange_and_blue(self) -> None:
        """Guards the brand itself, not just its legibility: --brand must sit in
        the orange band and --brand-2 in the blue band, so a future 'make it pop'
        edit cannot quietly return the portal to the old blue/teal scheme."""

        def hue(hex_colour: str) -> float:
            r, g, b = (
                int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)
            )
            high, low = max(r, g, b), min(r, g, b)
            if high == low:
                return 0.0
            delta = high - low
            if high == r:
                raw = ((g - b) / delta) % 6
            elif high == g:
                raw = (b - r) / delta + 2
            else:
                raw = (r - g) / delta + 4
            return raw * 60

        for scheme in ("dark", "light"):
            with self.subTest(scheme=scheme):
                self.assertTrue(
                    5 <= hue(self.token(scheme, "brand")) <= 45,
                    f"--brand {self.token(scheme, 'brand')} is not an orange",
                )
                self.assertTrue(
                    195 <= hue(self.token(scheme, "brand-2")) <= 250,
                    f"--brand-2 {self.token(scheme, 'brand-2')} is not a blue",
                )


if __name__ == "__main__":
    unittest.main()
