"""Shared scaffolding for scripts/gen-*.py code generators.

Every generator that writes a single committed file follows the same pattern:
- Build rendered output from source data
- In check mode: compare with the committed file, exit 1 if stale
- In write mode: write the file and print a confirmation

This module exposes two helpers:

``build_parser(description)``
    Return an ``ArgumentParser`` with ``--check`` already added.

``check_or_write(target, rendered, *, regenerate_hint, check)``
    Compare *rendered* to *target* (check mode) or write it (write mode).
    Returns 1 on stale, 0 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser(description: str) -> argparse.ArgumentParser:
    """Return a parser with ``--check`` pre-registered."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the output is stale (no write).",
    )
    return parser


def check_or_write(
    target: Path,
    rendered: str,
    *,
    regenerate_hint: str,
    check: bool,
) -> int:
    """Compare *rendered* to *target*, or overwrite it.

    Parameters
    ----------
    target:
        Path to the committed output file.
    rendered:
        The freshly generated string that *target* should contain.
    regenerate_hint:
        Message printed to stderr when the file is stale.
    check:
        ``True`` → compare only (no write); ``False`` → write.

    Returns
    -------
    int
        ``1`` when check mode detects staleness, ``0`` otherwise.
    """
    if check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != rendered:
            print(regenerate_hint, file=sys.stderr)
            return 1
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return 0
