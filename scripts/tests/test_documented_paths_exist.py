"""Every repo path a document names in backticks must exist.

Documentation drifts silently in a way prose review does not catch: a file gets
renamed, and the doc that pointed at it keeps rendering perfectly while pointing at
nothing. Nothing else in CI notices, because the existing link checker only validates
Markdown *links* (``[text](path)``), and a bare `` `app/api/tests/foo.py` `` is not a
link.

This was written after exactly that happened. The repository audit gained a paragraph
citing ``app/api/tests/test_deploy_defaults_fail_closed.py`` as the test pinning a
security posture; the file was renamed to ``test_deploy_posture.py`` in the same
change, and the audit shipped naming a test that did not exist. For a document whose
entire value is that its claims are checkable, a citation that resolves to nothing is
worse than no citation.

Scope is deliberately narrow so this stays signal:

* Only backticked tokens that start with a real top-level directory **and** end in a
  known source extension. Prose like `` `--Force` `` or `` `GlobalStandard` `` is
  ignored.
* Anything containing a glob or placeholder character is skipped: docs legitimately
  write `` `infra/**/*.bicep` `` and `` `foundry/skills/<name>/SKILL.md` ``.
* ``CHANGELOG.md`` is skipped entirely. It is a historical record, so naming a file
  that was later deleted (``infra/policies/realtime-routing-legacy.xml``) is correct
  and must not be "fixed".

Measured before being enforced: 206 references qualify across the tracked Markdown,
and all of them resolve.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TOP_LEVEL = (
    "app/",
    "infra/",
    "scripts/",
    "docs/",
    "site/",
    "proxy/",
    "foundry/",
    ".github/",
)
SOURCE_SUFFIXES = (
    ".py", ".ts", ".tsx", ".ps1", ".bicep", ".json", ".yml",
    ".yaml", ".css", ".cs", ".xml", ".md", ".sh",
)
# A doc writing a pattern rather than naming a file.
PATTERN_CHARS = set("*?<>[]")
# Historical record: correctly names files that were deleted after the entry was written.
SKIP_DOCUMENTS = {"CHANGELOG.md"}

# Paths a document names precisely because they do NOT exist. Each needs a reason.
KNOWN_ABSENT = {
    # AGENTS.md's opening states no such file exists today and says to make it a
    # pointer if one is ever added. Naming it is the point.
    ".github/copilot-instructions.md",
    "CLAUDE.md",
}

_BACKTICKED = re.compile(r"`([^`\s]+)`")


def _tracked_files() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return set(out.stdout.split())


class DocumentedPathsExistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracked = _tracked_files()

    def _candidates(self) -> list[tuple[str, int, str]]:
        found: list[tuple[str, int, str]] = []
        for doc in sorted(f for f in self.tracked if f.endswith(".md")):
            if Path(doc).name in SKIP_DOCUMENTS:
                continue
            text = (ROOT / doc).read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                for match in _BACKTICKED.finditer(line):
                    # Strip a trailing anchor: `docs/x.md#section`.
                    path = match.group(1).split("#")[0].strip()
                    if not path.startswith(TOP_LEVEL):
                        continue
                    if not path.endswith(SOURCE_SUFFIXES):
                        continue
                    if PATTERN_CHARS & set(path):
                        continue
                    if path in KNOWN_ABSENT:
                        continue
                    found.append((doc, number, path))
        return found

    def test_the_scan_finds_references(self) -> None:
        """Guards against a filter change silently emptying the scan."""
        self.assertGreater(
            len(self._candidates()),
            100,
            "expected the docs to cite many repo paths; the filters may be too narrow",
        )

    def test_every_documented_path_exists(self) -> None:
        broken = [
            f"{doc}:{number}: {path}"
            for doc, number, path in self._candidates()
            if path not in self.tracked
        ]
        self.assertEqual(
            broken,
            [],
            "Documentation names repo paths that do not exist. Rename the reference, "
            "or if the file is deliberately absent add it to KNOWN_ABSENT with a "
            "reason:\n" + "\n".join(broken),
        )

    def test_known_absent_entries_are_still_absent(self) -> None:
        """Keeps the allowlist from outliving its reason.

        If one of these is created later, the entry becomes a lie that suppresses a
        real check, so it must be removed rather than left as harmless-looking noise.
        """
        stale = sorted(p for p in KNOWN_ABSENT if p in self.tracked)
        self.assertEqual(
            stale,
            [],
            f"these now exist and must be removed from KNOWN_ABSENT: {stale}",
        )


if __name__ == "__main__":
    unittest.main()
