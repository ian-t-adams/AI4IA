#!/usr/bin/env python3
"""Generate the portal documentation index (site/data/docs.js) from a manifest.

The self-documenting portal in ``site/`` lists the repository's Markdown docs on
its docs hub. That index used to be a hand-edited ``site/data/docs.js`` that drifted
from reality (dead links, missing new docs). This script makes it a *generated*
artifact so it updates automatically as the docs change:

    site/data/docs.manifest.json  ->  site/data/docs.js

What it guarantees (so the portal surfaces the *right*, current docs):
  * Every path in the manifest exists (no dead links on the portal).
  * Completeness: every tracked ``*.md`` in the repo is either listed in the
    manifest or matched by an ``exclude`` glob. A newly added doc therefore cannot
    silently miss the portal -- CI (``--check``) fails until it is triaged.
  * A manifest entry may omit ``title``/``desc``; they are derived from the file's
    first ``# H1`` and first paragraph, so lightly-curated docs still render well.

It also validates the portal's *feature posture*: any entry in ``site/data/meta.js``
that carries a ``param: "<bicepParam>"`` must show ``on:`` equal to that parameter's
value in ``infra/main.parameters.json``. This catches the class of drift where the
portal advertises a capability as off (or on) that the live parameters contradict.

Run from the repo root:  python scripts/gen-docs-catalog.py
Verify-only (CI drift):  python scripts/gen-docs-catalog.py --check
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "site" / "data" / "docs.manifest.json"
TARGET = REPO_ROOT / "site" / "data" / "docs.js"
META = REPO_ROOT / "site" / "data" / "meta.js"
PARAMETERS = REPO_ROOT / "infra" / "main.parameters.json"

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EMPHASIS_RE = re.compile(r"[*_`]+")
_WS_RE = re.compile(r"\s+")
_DESC_MAX = 200


def _tracked_markdown() -> list[str]:
    """Every tracked *.md path (forward-slash, repo-relative), via git."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(p.strip().replace("\\", "/") for p in out.stdout.splitlines() if p.strip())


def _excluded(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat.endswith("/**"):
            if path == pat[:-3] or path.startswith(pat[:-2]):
                return True
        elif "*" in pat:
            from fnmatch import fnmatch

            if fnmatch(path, pat):
                return True
        elif path == pat:
            return True
    return False


def _clean_inline(text: str) -> str:
    text = _LINK_RE.sub(r"\1", text)
    text = _EMPHASIS_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def _derive_title(md: str, path: str) -> str:
    m = _H1_RE.search(md)
    return _clean_inline(m.group(1)) if m else Path(path).stem


def _derive_desc(md: str) -> str:
    """First real paragraph after the H1, cleaned and length-bounded."""
    lines = md.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            start = i + 1
            break
    para: list[str] = []
    in_code = False
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith(("#", "|", ">", "![", "<!--", "---", "- ", "* ", "1.")):
            if para:
                break
            continue
        para.append(stripped)
    desc = _clean_inline(" ".join(para))
    if len(desc) > _DESC_MAX:
        desc = desc[:_DESC_MAX].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return desc


def _js_str(value: str) -> str:
    # json.dumps yields a valid double-quoted JS string literal with correct escaping.
    return json.dumps(value, ensure_ascii=True)


def build_docs_js(manifest: dict, errors: list[str]) -> str:
    repo_base = manifest["repoBase"]
    excludes = manifest.get("exclude", [])
    listed: set[str] = set()
    sections_out: list[str] = []

    for section in manifest["sections"]:
        docs_out: list[str] = []
        for doc in section["docs"]:
            path = doc["path"].replace("\\", "/")
            listed.add(path)
            fs_path = REPO_ROOT / path
            if not fs_path.exists():
                errors.append(f"manifest lists '{path}' but the file does not exist")
                continue
            md = fs_path.read_text(encoding="utf-8")
            title = doc.get("title") or _derive_title(md, path)
            desc = doc.get("desc") or _derive_desc(md)
            docs_out.append(
                f"        {{ path: {_js_str(path)}, title: {_js_str(title)}, "
                f"desc: {_js_str(desc)} }},"
            )
        body = "\n".join(docs_out)
        sections_out.append(
            "    {\n"
            f"      group: {_js_str(section['group'])},\n"
            "      docs: [\n"
            f"{body}\n"
            "      ],\n"
            "    },"
        )

    # Completeness gate: no tracked doc may be silently absent from the portal.
    for path in _tracked_markdown():
        if path in listed or _excluded(path, excludes):
            continue
        errors.append(
            f"untriaged doc '{path}': add it to site/data/docs.manifest.json or its 'exclude' list"
        )

    sections = "\n".join(sections_out)
    return (
        "// GENERATED by scripts/gen-docs-catalog.py from site/data/docs.manifest.json.\n"
        "// Do not edit by hand -- edit the manifest and regenerate.\n"
        "window.AI4IA_DOCS = {\n"
        f"  repoBase: {_js_str(repo_base)},\n"
        "  sections: [\n"
        f"{sections}\n"
        "  ],\n"
        "};\n"
    )


def check_meta_posture(errors: list[str]) -> None:
    """Cross-check meta.js feature `on:` flags carrying a `param:` against live params."""
    if not META.exists() or not PARAMETERS.exists():
        return
    meta_text = META.read_text(encoding="utf-8")
    params = json.loads(PARAMETERS.read_text(encoding="utf-8")).get("parameters", {})
    # Each feature is a flat object literal (no nested braces); pick the ones with a param.
    for obj in re.findall(r"\{[^{}]*\}", meta_text):
        pm = re.search(r'param:\s*"([A-Za-z0-9_]+)"', obj)
        if not pm:
            continue
        param = pm.group(1)
        on_m = re.search(r"\bon:\s*(true|false)", obj)
        if not on_m:
            errors.append(f"meta.js: feature mapped to param '{param}' has no `on:` flag")
            continue
        shown = on_m.group(1) == "true"
        if param not in params:
            errors.append(
                f"meta.js: feature param '{param}' is not a parameter in "
                f"{PARAMETERS.relative_to(REPO_ROOT)}"
            )
            continue
        actual = bool(params[param].get("value"))
        if shown != actual:
            errors.append(
                f"meta.js: feature for '{param}' shows on={shown} but "
                f"main.parameters.json has {param}={actual}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if docs.js is stale or the portal docs are untriaged/inconsistent (no write).",
    )
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    errors: list[str] = []
    rendered = build_docs_js(manifest, errors)
    check_meta_posture(errors)

    if errors:
        print(f"FAIL: {len(errors)} documentation-index issue(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(
                "docs.js is stale. Run: python scripts/gen-docs-catalog.py",
                file=sys.stderr,
            )
            return 1
        print("docs.js is up to date; portal docs are complete and consistent.")
        return 0

    TARGET.write_text(rendered, encoding="utf-8")
    doc_count = sum(len(s["docs"]) for s in manifest["sections"])
    print(f"Wrote {TARGET.relative_to(REPO_ROOT)} ({doc_count} docs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
