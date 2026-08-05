import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./globals.css", import.meta.url), "utf8");

function luminance(hex: string): number {
  const channels = hex
    .slice(1)
    .match(/../g)!
    .map((value) => {
      const channel = Number.parseInt(value, 16) / 255;
      return channel <= 0.04045
        ? channel / 12.92
        : ((channel + 0.055) / 1.055) ** 2.4;
    });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

function block(selector: string): string {
  const found = css.match(
    new RegExp(
      `${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([\\s\\S]*?)\\}`,
    ),
  )?.[1];
  expect(found, `no CSS block for ${selector}`).toBeTruthy();
  return found!;
}

function token(selector: string, name: string): string {
  const value = block(selector).match(
    new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`),
  )?.[1];
  expect(value, `${selector} does not define --${name}`).toBeTruthy();
  return value!.toLowerCase();
}

const AA = 4.5;
// Non-text UI (focus rings, borders) is held to WCAG 1.4.11 rather than 1.4.3.
const AA_NON_TEXT = 3;

/**
 * Shortest angular distance between two hues, in degrees (0-180).
 *
 * Uses circular distance deliberately: a plain `Math.abs(a - b)` reports hue 350
 * and hue 10 as 340 apart when they are 20 apart and visually near-identical,
 * so a red-violet accent could have slipped past the distinctness checks below.
 * Every current pair sits near hue 0-60, so this is behaviour-preserving today.
 */
function hueSeparation(a: string, b: string): number {
  const hue = (hex: string) => {
    const [r, g, b_] = [0, 2, 4].map(
      (i) => Number.parseInt(hex.slice(1 + i, 3 + i), 16) / 255,
    );
    const max = Math.max(r, g, b_);
    const min = Math.min(r, g, b_);
    if (max === min) return 0;
    const d = max - min;
    const h =
      max === r
        ? (g - b_) / d + (g < b_ ? 6 : 0)
        : max === g
          ? (b_ - r) / d + 2
          : (r - g) / d + 4;
    return h * 60;
  };
  const delta = Math.abs(hue(a) - hue(b));
  return Math.min(delta, 360 - delta);
}

describe("danger token contrast", () => {
  it.each([
    [":root", "#b91c1c", "#ffffff"],
    ['[data-theme="dark"]', "#ff6b5e", "#0d1117"],
    ['[data-theme="contrast"]', "#ff8a80", "#000000"],
  ])("%s keeps danger text at WCAG AA", (selector, background, foreground) => {
    const css_ = block(selector);
    expect(css_).toContain(`--danger: ${background}`);
    expect(css_).toContain(`--danger-fg: ${foreground}`);
    expect(contrast(background, foreground)).toBeGreaterThanOrEqual(AA);
  });
});

// --accent is load-bearing in two directions at once: it is link/icon TEXT on
// --bg in ~38 places AND a button FILL under --accent-fg in ~13. A brand orange
// picked for vibrance alone silently fails the first (#ea580c is 3.3:1 on
// white), so both directions are asserted for every theme.
describe("brand accent contrast", () => {
  it.each([":root", '[data-theme="dark"]', '[data-theme="contrast"]'])(
    "%s keeps --accent legible as text on --bg and --bg-elevated",
    (selector) => {
      const accent = token(selector, "accent");
      for (const surface of ["bg", "bg-elevated"]) {
        expect(
          contrast(accent, token(selector, surface)),
          `--accent on --${surface} in ${selector}`,
        ).toBeGreaterThanOrEqual(AA);
      }
    },
  );

  it.each([":root", '[data-theme="dark"]', '[data-theme="contrast"]'])(
    "%s keeps --accent-fg legible on an --accent fill",
    (selector) => {
      expect(
        contrast(token(selector, "accent"), token(selector, "accent-fg")),
      ).toBeGreaterThanOrEqual(AA);
    },
  );

  it.each([":root", '[data-theme="dark"]', '[data-theme="contrast"]'])(
    "%s keeps user-bubble text legible",
    (selector) => {
      expect(
        contrast(token(selector, "user-bubble"), token(selector, "user-bubble-fg")),
      ).toBeGreaterThanOrEqual(AA);
    },
  );

  it.each([":root", '[data-theme="dark"]'])(
    "%s keeps the focus ring visible against both surfaces",
    (selector) => {
      const ring = token(selector, "focus-ring");
      for (const surface of ["bg", "bg-elevated"]) {
        expect(
          contrast(ring, token(selector, surface)),
          `--focus-ring on --${surface} in ${selector}`,
        ).toBeGreaterThanOrEqual(AA_NON_TEXT);
      }
    },
  );

  it.each([":root", '[data-theme="dark"]', '[data-theme="contrast"]'])(
    "%s keeps --info, --success and --warn legible as text on both surfaces",
    (selector) => {
      // These three are status TEXT (a failed upload, a saved memory, a degraded
      // health pill), so they need the full AA text ratio on whichever surface
      // they land on -- not the 3:1 non-text floor. Components used to hardcode
      // light-theme hexes for all of these; #b91c1c for an error message scored
      // 2.67:1 on the dark surface, below even the large-text floor.
      for (const name of ["info", "success", "warn"]) {
        for (const surface of ["bg", "bg-elevated"]) {
          expect(
            contrast(token(selector, name), token(selector, surface)),
            `--${name} on --${surface} in ${selector}`,
          ).toBeGreaterThanOrEqual(AA);
        }
      }
    },
  );

  // The brand is orange + blue. Without this, a well-meaning "make the accent
  // pop" edit could drift --accent back to a red-orange indistinguishable from
  // --danger, which is the one pair users must never confuse.
  it.each([":root", '[data-theme="dark"]'])(
    "%s keeps --accent visually distinct from --danger",
    (selector) => {
      const accent = token(selector, "accent");
      const danger = token(selector, "danger");
      expect(accent).not.toEqual(danger);
      expect(
        hueSeparation(accent, danger),
        `--accent ${accent} and --danger ${danger} are too close in hue`,
      ).toBeGreaterThanOrEqual(10);
    },
  );

  // Same failure mode one step over: the light theme's brand accent IS an
  // orange, so an amber warn drifts into it. --warn started at #92400e, five
  // degrees off --accent, which would have made a warning read as brand chrome.
  it.each([":root", '[data-theme="dark"]', '[data-theme="contrast"]'])(
    "%s keeps --warn visually distinct from --accent and --danger",
    (selector) => {
      const warn = token(selector, "warn");
      for (const other of ["accent", "danger"] as const) {
        const value = token(selector, other);
        expect(warn).not.toEqual(value);
        expect(
          hueSeparation(warn, value),
          `--warn ${warn} and --${other} ${value} are too close in hue`,
        ).toBeGreaterThanOrEqual(15);
      }
    },
  );
});

// The brand tokens are decorative, but every theme must define them or a rule
// that references var(--brand) resolves to nothing and the element loses its fill.
describe("brand tokens are defined in every theme", () => {
  it.each([":root", '[data-theme="dark"]', '[data-theme="contrast"]'])(
    "%s defines --brand and --brand-2",
    (selector) => {
      expect(token(selector, "brand")).toMatch(/^#[0-9a-f]{6}$/);
      expect(token(selector, "brand-2")).toMatch(/^#[0-9a-f]{6}$/);
    },
  );
});
