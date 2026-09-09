"""Lazy third-party imports are invisible to every other gate in CI.

The API deliberately imports heavy/optional SDKs *inside* the function that needs
them, so the app boots and the unit tests run without every Azure service wired.
That is the right design, but it costs us the only automatic proof that a
declared dependency is still needed — and, worse, the only proof that a *needed*
dependency is still declared.

This was not hypothetical. ``pyproject.toml`` carried a comment asserting that
``azure-monitor-query`` had been dropped in favour of ``azure-monitor-querymetrics``
"since only its metrics client was ever used (no logs client)". Both halves were
false: ``metrics/log_analytics.py`` imports ``LogsQueryClient`` from it, and the
dependency was still declared two lines below the comment saying it was gone.
Acting on that comment would have removed a live dependency.

Nothing would have caught it. Verified by uninstalling the package outright:

* ``pyright`` reported ``0 errors`` — an unresolved submodule of the ``azure``
  namespace package does not fail the type gate the way a missing top-level
  module does.
* ``pytest`` stayed green — the import is lazy, and the metrics tests inject a
  fake querier rather than constructing the real client.

So the break would first appear in production, as an ``ImportError`` the moment
an admin opened the operations panel.

This test closes that hole for the whole class rather than for the one package:
it re-derives the lazy imports from the source on every run, so a new one is
covered the day it is written.

Pytest covers the dev/foundry environment. docker-build also runs this file with
plain Python inside the shipping image, without installing pytest or mounting
source over the installed package. That path exercises the extra-free runtime
and rejects build/provisioning/test tools in it.
"""
from __future__ import annotations

import ast
import importlib
import importlib.metadata
import importlib.util
import os
import re
import sys
from pathlib import Path

import ai4ia_api

SRC = Path(ai4ia_api.__file__).resolve().parent
FIRST_PARTY = "ai4ia_api"

# The survey that motivated this test found 24 distinct modules. Assert a floor a
# little below that so ordinary refactoring does not trip it, but a discovery step
# that silently matches nothing cannot let the test pass vacuously.
MINIMUM_EXPECTED = 20


def _lazy_third_party_imports() -> dict[str, set[str]]:
    """Map each third-party module imported inside a function body to its files."""
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                for module in _imported_modules(inner):
                    top = module.split(".")[0]
                    if top in sys.stdlib_module_names or top == FIRST_PARTY:
                        continue
                    found.setdefault(module, set()).add(path.name)
    return found


def _imported_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        # A non-zero level is a relative import, which is first-party by definition.
        return [node.module] if node.module and not node.level else []
    return []


def test_every_lazily_imported_module_is_actually_installed() -> None:
    discovered = _lazy_third_party_imports()

    assert len(discovered) >= MINIMUM_EXPECTED, (
        f"only {len(discovered)} lazy third-party imports discovered, expected at "
        f"least {MINIMUM_EXPECTED} — the AST walk has probably stopped matching, "
        f"which would make this test pass without checking anything"
    )

    unresolved: list[str] = []
    for module in sorted(discovered):
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            unresolved.append(f"{module} (used in {', '.join(sorted(discovered[module]))})")

    assert not unresolved, (
        "these modules are imported at runtime but are not installed, so the code "
        "paths using them raise ImportError in production while CI stays green — "
        "add the distribution to [project.dependencies] in pyproject.toml:\n  "
        + "\n  ".join(unresolved)
    )


def _assert_runtime_image() -> None:
    assert sys.version_info[:2] == (3, 12), "runtime must use CI's Python 3.12"
    assert SRC.is_relative_to(Path(sys.prefix)), "API must be installed, not imported from build source"
    test_every_lazily_imported_module_is_actually_installed()
    for module in sorted(_lazy_third_party_imports()):
        importlib.import_module(module)

    installed = {
        re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        for distribution in importlib.metadata.distributions()
    }
    excluded = {
        "azure-ai-projects", "hatchling", "pyright", "pytest", "pytest-asyncio", "ruff", "uv",
    }
    assert not installed & excluded, f"non-runtime packages installed: {installed & excluded}"
    if sys.platform == "linux":
        assert os.getuid() == 10001, "API must run as the unchanged non-root user"
        assert not os.access(sys.prefix, os.W_OK), "runtime user must not own the installed environment"
        importlib.import_module("uvloop")
        assert "colorama" not in installed, "Windows-only dependency leaked into Linux"
    importlib.import_module("ai4ia_api.main")


if __name__ == "__main__":
    _assert_runtime_image()
