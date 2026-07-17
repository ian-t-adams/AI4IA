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

describe("danger token contrast", () => {
  it.each([
    [":root", "#c0392b", "#ffffff"],
    ['[data-theme="dark"]', "#ff6b5e", "#0d1117"],
    ['[data-theme="contrast"]', "#ff8a80", "#000000"],
  ])("%s keeps danger text at WCAG AA", (selector, background, foreground) => {
    const block = css.match(
      new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([\\s\\S]*?)\\}`),
    )?.[1];
    expect(block).toContain(`--danger: ${background}`);
    expect(block).toContain(`--danger-fg: ${foreground}`);
    expect(contrast(background, foreground)).toBeGreaterThanOrEqual(4.5);
  });
});
