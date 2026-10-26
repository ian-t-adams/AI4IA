import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { ghostBtn, inputStyle, primaryBtn, secondaryBtn } from "./builderStyles";

const ui = readFileSync(new URL("../app/ui.css", import.meta.url), "utf8");
const globals = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
const decision = readFileSync(new URL("./DecisionPlayground.module.css", import.meta.url), "utf8");

function rule(source: string, selector: string): string {
  const start = source.indexOf(`${selector} {`);
  expect(start, `Missing ${selector}`).toBeGreaterThanOrEqual(0);
  return source.slice(start, source.indexOf("}", start) + 1);
}

describe("shared web control contract", () => {
  it("binds Studio adapters and CSS controls to the same target and radius tokens", () => {
    expect(rule(globals, ":root")).toContain("--control-height: 44px;");
    for (const style of [inputStyle, primaryBtn, secondaryBtn, ghostBtn]) {
      expect(style.minHeight).toBe("var(--control-height)");
      expect(style.borderRadius).toBe("var(--control-radius)");
    }
    for (const selector of [".btn", ".field-control", ".workspace-tab"]) {
      const css = rule(ui, selector);
      expect(css).toContain("min-height: var(--control-height);");
      expect(css).toContain("border-radius: var(--control-radius);");
    }
    expect(rule(ui, ".accent-swatch")).toContain("width: var(--control-height);");
    expect(rule(ui, ".accent-swatch")).toContain("height: var(--control-height);");
  });

  it("reserves the accent fill for actions while tabs keep a non-color selection cue", () => {
    const selected = rule(ui, '.workspace-tab[aria-selected="true"]');
    expect(selected).toContain("background: var(--bg-active);");
    expect(selected).toContain("box-shadow: inset 0 -2px var(--accent);");
    expect(selected).toContain("font-weight: 600;");
    expect(primaryBtn.background).toBe("var(--accent)");
    expect(primaryBtn.color).toBe("var(--accent-fg)");
  });

  it("keeps Decision layout separate from shared control styling and text scaling", () => {
    expect(decision).not.toContain(".playground button {");
    expect(decision).not.toContain("outline: 2px");
    expect(decision).toContain("font-size: calc(1rem * var(--font-scale))");
    expect(rule(ui, ".ui-state[role=\"alert\"]")).toContain("border-color: var(--danger);");
  });
});
