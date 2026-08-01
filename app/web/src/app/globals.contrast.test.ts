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

  it.each([":root", '[data-theme="dark"]'])(
    "%s keeps --info and --success legible as text",
    (selector) => {
      for (const name of ["info", "success"]) {
        expect(
          contrast(token(selector, name), token(selector, "bg")),
          `--${name} on --bg in ${selector}`,
        ).toBeGreaterThanOrEqual(AA);
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
      const hue = (hex: string) => {
        const [r, g, b] = [0, 2, 4].map(
          (i) => Number.parseInt(hex.slice(1 + i, 3 + i), 16) / 255,
        );
        const max = Math.max(r, g, b);
        const min = Math.min(r, g, b);
        if (max === min) return 0;
        const d = max - min;
        const h =
          max === r
            ? ((g - b) / d + (g < b ? 6 : 0))
            : max === g
              ? (b - r) / d + 2
              : (r - g) / d + 4;
        return h * 60;
      };
      expect(
        Math.abs(hue(accent) - hue(danger)),
        `--accent ${accent} and --danger ${danger} are too close in hue`,
      ).toBeGreaterThanOrEqual(10);
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
