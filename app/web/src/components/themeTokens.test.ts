import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Source-level guard: inline foreground colors must resolve through a theme
 * token, never a literal hex.
 *
 * This is not a style preference. A literal is fixed at author time, so it can
 * only ever be correct for the one theme the author happened to be looking at,
 * and it fails silently everywhere else. Both of these shipped:
 *
 *  - `color: "#fff"` on a `var(--accent)` fill measures **1.07:1** in the
 *    high-contrast theme (white on its yellow accent) and 2.26:1 in the dark
 *    theme. It sat on the primary action button of three panels. The
 *    high-contrast theme is the accessibility floor, so an invisible label
 *    there is the worst case, not an edge case.
 *  - `#b91c1c` for an error message measures 2.67:1 on the dark surface,
 *    below even the 3:1 large-text floor.
 *
 * `--accent-fg` is derived per accent by `ThemeProvider.readableForeground`,
 * so it stays correct for a *user-chosen* accent too, which no fixed value can.
 *
 * AGENTS.md records this antipattern under "Change the brand palette or a
 * logo". It recurred anyway, which is why it is now executable rather than
 * prose. Verified by mutation: against the tree before these were fixed, the
 * two assertions below report 14 and 3 offenders respectively.
 */

const SRC = fileURLToPath(new URL("..", import.meta.url));

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    if (!/\.tsx?$/.test(entry) || /\.test\./.test(entry)) return [];
    return [full];
  });
}

/**
 * Bodies of each `style={{ ... }}`, found by brace matching.
 *
 * A fixed-width window around a match is not good enough here: it happily spans
 * two sibling JSX elements and reports the `background` of one against the
 * `color` of the next. Matching braces keeps every comparison inside a single
 * element's style object.
 */
function styleObjects(source: string): string[] {
  const bodies: string[] = [];
  for (const match of source.matchAll(/style=\{\{/g)) {
    let index = match.index + match[0].length - 1;
    const start = index;
    let depth = 0;
    while (index < source.length) {
      if (source[index] === "{") depth += 1;
      else if (source[index] === "}") {
        depth -= 1;
        if (depth === 0) {
          bodies.push(source.slice(start + 1, index));
          break;
        }
      }
      index += 1;
    }
  }
  return bodies;
}

// A property value, including multi-line ternaries. The continuation stops at
// the next `prop:` at any indentation -- without allowing leading whitespace in
// that guard, a `background:` value swallows the `color:` line beneath it.
const VALUE = String.raw`([^\n]*(?:\n(?!\s*[a-zA-Z]+:)[^\n]*)*)`;
// `(?<![A-Za-z-])` keeps this off `backgroundColor:` and `--accent-color:`.
const FOREGROUND = new RegExp(String.raw`(?<![A-Za-z-])color:\s*` + VALUE);
const BACKGROUND = new RegExp(String.raw`background(?:Color)?:\s*` + VALUE);
const FOREGROUND_LINE = /(?<![A-Za-z-])color:/;
const HEX_LITERAL = /"#[0-9a-fA-F]{3,8}"/;

describe("inline foreground colors are theme tokens", () => {
  const files = sourceFiles(SRC);

  it("finds the sources to scan", () => {
    // Guards against a refactor silently emptying the scan and making the
    // assertions below vacuously true.
    expect(files.length).toBeGreaterThan(20);
  });

  it("never assigns a literal hex to a color property", () => {
    const offenders: string[] = [];
    for (const file of files) {
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((line, index) => {
          if (FOREGROUND_LINE.test(line) && HEX_LITERAL.test(line)) {
            offenders.push(
              `${path.relative(SRC, file)}:${index + 1}: ${line.trim()}`,
            );
          }
        });
    }
    expect(
      offenders,
      `Use a theme token (var(--fg), var(--danger), var(--warn), var(--accent-fg), ...) so the color follows the active theme:\n${offenders.join("\n")}`,
    ).toEqual([]);
  });

  it("pairs every var(--accent) fill with the derived var(--accent-fg)", () => {
    // An accent background is the one fill whose readable foreground flips
    // between black and white depending on the theme and the user's chosen
    // accent, so any fixed foreground beside it is wrong for some of them.
    const offenders: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      if (!source.includes("var(--accent)")) continue;
      for (const body of styleObjects(source)) {
        const background = BACKGROUND.exec(body)?.[1];
        const foreground = FOREGROUND.exec(body)?.[1];
        if (!background || !foreground) continue;
        if (
          /var\(--accent\)/.test(background) &&
          !foreground.includes("var(--accent-fg)")
        ) {
          offenders.push(
            `${path.relative(SRC, file)}: background uses var(--accent) but color is ${foreground.trim().split("\n")[0]}`,
          );
        }
      }
    }
    expect(
      offenders,
      `A var(--accent) background must use var(--accent-fg), which ThemeProvider derives per accent:\n${offenders.join("\n")}`,
    ).toEqual([]);
  });
});
