"""Every Markdown heading anchor a document links to must actually resolve.

The existing checks cover two of the three ways a Markdown link can rot, and the
gap between them shipped a broken link to `main`.

* ``test_documented_paths_exist`` validates backticked repo paths -- and it
  explicitly *strips* a trailing ``#anchor`` before checking (see its
  ``path.split("#")[0]``), so the anchor half is deliberately unexamined.
* The portal/link checks validate that a linked file exists, not that a fragment
  inside it does.

So ``[the disposition](#immediate-action--status-as-of-2026-08-05)`` rendered as
a perfectly ordinary link, pointing at nothing, because the heading it targeted
had been retitled to ``... 2026-08-08``. That is not a hypothetical failure mode
here: several headings in this repo carry dates and statuses that are updated in
place, so their anchors change every time the underlying claim is refreshed --
exactly the headings that in-document links most want to point at.

Scope is kept narrow so this stays signal rather than noise:

* Only Markdown links whose target is a same-document fragment (``#anchor``) or a
  relative Markdown file plus fragment (``other.md#anchor``). External URLs and
  bare file links are already covered elsewhere.
* Anchors are resolved with GitHub's documented slug algorithm, including its
  duplicate-heading ``-1``/``-2`` suffixing.
* Headings inside fenced code blocks are not headings, so they are skipped.
* ``CHANGELOG.md`` is skipped for the same reason the path checker skips it: it
  is a historical record whose entries correctly reference retired sections.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SKIP_DOCUMENTS = {"CHANGELOG.md"}

# ``[text](target)`` where target is a fragment, optionally preceded by a
# relative path. Excludes external schemes and mailto.
_LINK = re.compile(r"\[(?:[^\]]*)\]\(([^)\s]*#[^)\s]+)\)")
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# Inline markup that GitHub renders away before slugging.
_INLINE_CODE = re.compile(r"`([^`]*)`")
_INLINE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")


def slugify(heading: str) -> str:
    """Reproduce GitHub's heading-to-anchor algorithm.

    Rendered inline markup is unwrapped first, then everything that is not a
    word character, space, or hyphen is dropped and spaces become hyphens. The
    dropped characters are why an em dash surrounded by spaces yields a double
    hyphen (``action -- status``), which is precisely the shape this repo's
    dated headings produce.
    """
    text = _INLINE_LINK.sub(r"\1", heading)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s", "-", text)


def anchors_for(text: str) -> set[str]:
    """All anchors a rendered document exposes, with duplicate suffixing."""
    found: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if not match:
            continue
        slug = slugify(match.group(2))
        if not slug:
            continue
        seen = counts.get(slug, 0)
        found.add(slug if seen == 0 else f"{slug}-{seen}")
        counts[slug] = seen + 1
    return found


def tracked_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


class MarkdownAnchorsResolve(unittest.TestCase):
    def test_every_markdown_fragment_link_resolves(self) -> None:
        docs = tracked_markdown()
        self.assertGreater(len(docs), 20, "expected a substantial docs corpus")

        cache: dict[Path, set[str]] = {}
        checked = 0
        broken: list[str] = []

        for doc in sorted(docs):
            if Path(doc).name in SKIP_DOCUMENTS:
                continue
            source = ROOT / doc
            text = source.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                for match in _LINK.finditer(line):
                    target = match.group(1)
                    if "://" in target or target.startswith("mailto:"):
                        continue
                    path_part, _, fragment = target.partition("#")
                    if not fragment:
                        continue
                    if path_part:
                        if not path_part.endswith(".md"):
                            continue
                        resolved = (source.parent / path_part).resolve()
                    else:
                        resolved = source.resolve()
                    if not resolved.is_file():
                        # A missing target file is the path checker's job.
                        continue
                    if resolved not in cache:
                        cache[resolved] = anchors_for(
                            resolved.read_text(encoding="utf-8", errors="replace")
                        )
                    checked += 1
                    if fragment.lower() not in cache[resolved]:
                        broken.append(
                            f"{doc}:{number} -> '{target}' "
                            f"(no heading in {resolved.relative_to(ROOT).as_posix()} "
                            f"slugs to '{fragment.lower()}')"
                        )

        # Non-vacuity: a pass here must mean links were actually resolved, not
        # that the matcher quietly found nothing to check.
        self.assertGreater(
            checked, 20, f"anchor check inspected only {checked} links; matcher is broken"
        )
        self.assertEqual([], broken, "broken Markdown anchors:\n" + "\n".join(broken))

    def test_slugify_matches_github_for_this_repos_heading_shapes(self) -> None:
        # Pinned so a future "simplification" of slugify cannot silently make the
        # check above vacuous. The em-dash case is the one that broke in practice.
        self.assertEqual(
            "immediate-action--status-as-of-2026-08-08",
            slugify("Immediate action — status as of 2026-08-08"),
        )
        self.assertEqual("what-this-repo-is", slugify("What this repo is"))
        self.assertEqual(
            "add-or-move-a-documentation-page",
            slugify("Add or move a documentation page"),
        )
        self.assertEqual("run-uv-lock", slugify("Run `uv lock`"))

    def test_duplicate_headings_get_github_style_suffixes(self) -> None:
        anchors = anchors_for("# Notes\n\n# Notes\n\n# Notes\n")
        self.assertEqual({"notes", "notes-1", "notes-2"}, anchors)

    def test_headings_inside_fenced_code_are_not_anchors(self) -> None:
        anchors = anchors_for("# Real\n\n```\n# Fake\n```\n")
        self.assertEqual({"real"}, anchors)


if __name__ == "__main__":
    unittest.main()
