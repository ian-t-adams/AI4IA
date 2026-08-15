"""Shared test harness for postprovision.ps1 executable tests.

Each ``test_postprovision_*.py`` file uses PowerShell's AST parser to extract
a named function from the script, inject stub Azure/HTTP helpers, and execute it
in-process. This module provides the two pieces that are otherwise copy-pasted
verbatim across all three files:

``_ps_literal(value)``
    Escape *value* as a single-quoted PowerShell string literal.

``_run_pwsh(command, *, timeout=45)``
    Run *command* via ``pwsh``, assert exit code 0, and return the parsed JSON
    from the last line of stdout.

Following the ``_loader.py`` / ``_platform.py`` / ``_pngread.py`` convention, the
leading underscore keeps unittest discovery from treating this module as a test
suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest


def _ps_literal(value: str) -> str:
    """Return *value* as a single-quoted PowerShell string literal."""
    return "'" + value.replace("'", "''") + "'"


def _run_pwsh(
    command: str,
    *,
    cwd: "Path | None" = None,
    env: "dict | None" = None,
    timeout: int = 45,
) -> dict[str, object]:
    """Execute *command* with ``pwsh`` and return the last-line JSON payload.

    Raises ``unittest.SkipTest`` when ``pwsh`` is absent, and
    ``AssertionError`` when the process exits non-zero.
    """
    if shutil.which("pwsh") is None:
        raise unittest.SkipTest("pwsh is required for executable postprovision tests")
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"PowerShell failed ({proc.returncode}):\n{proc.stderr}\n{proc.stdout}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])
