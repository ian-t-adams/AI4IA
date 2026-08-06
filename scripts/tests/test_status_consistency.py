"""Keeps the audit's disposition table and the roadmap telling the same story.

Two documents carry live status for the same findings, for different audiences:

* `docs/repository-audit-2026-08-03.md` -- the disposition table. Findings
  themselves are never edited when fixed (that would rewrite history); status
  lives only in that table.
* `docs/roadmap.md` -- the owner-decision table, which says what is left and what
  it would cost.

Nothing coupled them, and they drifted the day it mattered. PR #294 set
`disableLocalAuth` to `true` and updated the roadmap's P1-4 row, but left the
audit's disposition row reading "**Open** -- `disableLocalAuth` still defaults
false". For several hours the repository's own audit asserted, in the present
tense, the opposite of what its Bicep did. Nothing failed, because prose is not
executable.

This module makes the narrow, checkable part of that coupling executable: a
finding may not be flagged **Open** in the audit while the roadmap describes it
as fixed, done or half-closed. It deliberately does NOT try to parse or compare
free prose -- that would be brittle and would train people to phrase around it.
It checks two things a machine can be certain about:

1. the two documents cover the same set of finding IDs, and
2. no finding is "Open" in one and closed/half-closed in the other.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "docs" / "repository-audit-2026-08-03.md"
ROADMAP = REPO / "docs" / "roadmap.md"

# A disposition row: `| P1-4 short title | **Status** ... | #294 |`
_ROW = re.compile(r"^\|\s*(P[01]-\d+)\b[^|]*\|\s*(.+?)\s*\|[^|]*\|\s*$", re.MULTILINE)

# Words that mean "this shipped", in either document. Matched as a *substring* of
# the bolded status, not as the whole of it: the roadmap writes
# "**Half closed 2026-08-06.**" and the audit writes "**Partially fixed** — ...",
# and an exact-match pattern silently matches neither. That mistake made the first
# version of this module pass with the very defect it was written for still in
# place, which is why the mutation below is part of the suite.
_CLOSED = re.compile(
    r"\*\*[^*]*\b(?:fixed|closed|done|accepted|contained|shipped)\b[^*]*\*\*",
    re.IGNORECASE,
)
_OPEN = re.compile(r"\*\*open\*\*", re.IGNORECASE)


def _audit_status() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _ROW.finditer(AUDIT.read_text(encoding="utf-8"))}


def _roadmap_text() -> str:
    return ROADMAP.read_text(encoding="utf-8")


class AuditAndRoadmapAgree(unittest.TestCase):
    def test_no_finding_is_open_in_the_audit_and_closed_in_the_roadmap(self) -> None:
        roadmap = _roadmap_text()
        contradictions: list[str] = []
        for finding, status in _audit_status().items():
            if not _OPEN.search(status):
                continue
            # Find the roadmap's own paragraph/row for this finding, if any.
            for line in roadmap.splitlines():
                if finding in line and _CLOSED.search(line):
                    contradictions.append(
                        f"{finding}: audit says Open, roadmap says "
                        f"{_CLOSED.search(line).group(0)}"  # type: ignore[union-attr]
                    )
                    break
        self.assertEqual(
            contradictions,
            [],
            "The audit's disposition table and the roadmap disagree about what "
            "has shipped:\n  " + "\n  ".join(contradictions) + "\n"
            "Both are live status documents for the same findings. Update them "
            "in the same commit -- PR #294 updated only the roadmap and left the "
            "audit asserting the opposite of the deployed Bicep.",
        )

    def test_every_audit_finding_has_a_status(self) -> None:
        """A row whose status cell is empty reads as 'no one has looked'."""
        blank = [f for f, s in _audit_status().items() if not s.strip()]
        self.assertEqual(blank, [], f"disposition rows with no status: {blank}")

    def test_the_parser_still_matches_the_table(self) -> None:
        """Guards against the table being restyled into invisibility.

        If the row format changes, `_ROW` silently matches nothing and every
        assertion above passes vacuously. The audit covers P0-1..P1-17, so a
        healthy parse finds well over a dozen rows.
        """
        found = _audit_status()
        self.assertGreater(
            len(found),
            15,
            "the disposition-table parser matched too few rows, so the "
            f"consistency checks are no longer exercising anything real: {sorted(found)}",
        )
        for expected in ("P0-1", "P1-4", "P1-13", "P1-17"):
            self.assertIn(expected, found, f"{expected} missing from the disposition table")


if __name__ == "__main__":
    unittest.main()
