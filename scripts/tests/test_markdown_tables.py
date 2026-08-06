"""Every Markdown table row must have the same number of cells as its header.

Two real corruptions shipped before this existed, both from one careless edit to
`docs/configuration-reference.md` in #280:

* An `old_str` that ended with a newline consumed the line break between two
  rows, so `| Permit dev auth ... || Cost center tag | ...` became one line. The
  "Cost center tag" row *disappeared from the rendered table entirely*.
* `` `demo|production` `` was written in an audit table cell. GitHub Flavored
  Markdown splits cells on any unescaped `|` **including inside a code span**, so
  that cell silently became two and the row grew a phantom column.

Neither is visible in a diff, neither breaks any link, and neither trips a
spell-check or a link checker. They only show up when someone reads the rendered
page and finds a row missing -- which for a configuration reference means an
operator never learns a variable exists.

The rule is purely structural, so it is cheap to enforce and has no false
positives worth tolerating: within one contiguous run of `|`-prefixed lines,
every line must contain the same number of unescaped pipes.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# GFM splits on any pipe that is not backslash-escaped. A pipe inside backticks
# is NOT exempt, which is the trap that produced the second corruption above.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _tracked_markdown() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return sorted(result.stdout.split())


def _broken_rows(path: Path) -> list[str]:
    """Rows whose cell count differs from the first row of their table."""
    problems: list[str] = []
    in_table = False
    expected = 0
    header_line = 0
    for number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        line = raw.rstrip()
        if line.startswith("|"):
            count = len(_UNESCAPED_PIPE.findall(line))
            if not in_table:
                in_table = True
                expected = count
                header_line = number
            elif count != expected:
                problems.append(
                    f"{path.relative_to(ROOT).as_posix()}:{number}: "
                    f"{count} cell delimiters, but the table starting at line "
                    f"{header_line} has {expected}. Escape any literal pipe as "
                    f"`\\|` (backticks do NOT protect it), or restore a missing "
                    f"line break: {line[:70]}"
                )
        else:
            in_table = False
    return problems


class MarkdownTableShapeTests(unittest.TestCase):
    def test_there_are_tables_to_check(self) -> None:
        """Non-vacuity: this must not pass by finding nothing."""
        rows = 0
        for name in _tracked_markdown():
            for line in (ROOT / name).read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("|"):
                    rows += 1
        self.assertGreater(rows, 100, "expected many table rows across the docs")

    def test_every_table_row_has_a_consistent_cell_count(self) -> None:
        problems: list[str] = []
        for name in _tracked_markdown():
            problems.extend(_broken_rows(ROOT / name))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_detects_an_unescaped_pipe_in_a_code_span(self) -> None:
        """Guards the guard, for the case that looks safe and is not."""
        import tempfile

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            sample = Path(tmp) / "sample.md"
            sample.write_text(
                "| a | b |\n| --- | --- |\n| x | `demo|production` |\n",
                encoding="utf-8",
            )
            self.assertEqual(len(_broken_rows(sample)), 1)

    def test_accepts_a_properly_escaped_pipe(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            sample = Path(tmp) / "sample.md"
            sample.write_text(
                "| a | b |\n| --- | --- |\n| x | `demo\\|production` |\n",
                encoding="utf-8",
            )
            self.assertEqual(_broken_rows(sample), [])


if __name__ == "__main__":
    unittest.main()
