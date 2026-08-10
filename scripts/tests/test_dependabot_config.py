"""Guards that Dependabot and CI agree about `app/api/uv.lock`.

`app-ci` fails the `api` job when `uv lock --check` reports drift. That gate was
added because the lock silently rotted four `pypdf` releases behind a live CVE
fix -- nothing on the install path reads it (`Dockerfile` runs `pip install .`,
CI runs `pip install -e ".[dev]"`), so nothing noticed.

The gate only works if whatever opens dependency PRs keeps the two files in
step. Dependabot's `pip` ecosystem does not: it edits `pyproject.toml` and has
no knowledge of `uv.lock`. And because `uv.lock` records the *declared
specifier* rather than only resolved versions, even a pure ceiling bump
(`fastapi<0.141` -> `<0.142`, with no package actually moving) desyncs it. Every
Dependabot Python PR therefore failed CI until someone hand-ran `uv lock`.

`package-ecosystem: uv` updates `pyproject.toml` and `uv.lock` in the same
commit (dependabot-core's uv lock file updater declares
`REQUIRED_FILES = %w(pyproject.toml uv.lock)` and emits both), so the gate is
satisfied by construction.

These checks are text/YAML level on purpose: the real behaviour lives in
GitHub's hosted Dependabot, which CI cannot execute. What CI *can* do is stop
the configuration from drifting back into the broken combination.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT = ROOT / ".github/dependabot.yml"
APP_CI = ROOT / ".github/workflows/app-ci.yml"
API_DIR = "/app/api"
PROXY_PROJECT_DIRS = {
    "/proxy/AI4IA.Proxy.Tests",
    "/proxy/Shared",
    "/proxy/Shared-parser",
    "/proxy/SimpleL7Proxy",
}

# Ecosystems that resolve through uv and therefore maintain uv.lock.
UV_AWARE_ECOSYSTEMS = {"uv"}

# Dependabot's YAML values for Python dependency manifests. `poetry` and
# `pipenv` projects are both handled under `pip`, so these two are the whole
# set. Other ecosystems legitimately share the directory -- `docker` manages
# app/api/Dockerfile's base image -- and are not competing for pyproject.toml.
PYTHON_ECOSYSTEMS = {"pip", "uv"}


def _updates() -> list[dict]:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    return list(config["updates"])


def _python_entries_for(directory: str) -> list[dict]:
    matches = []
    for entry in _updates():
        if entry.get("package-ecosystem") not in PYTHON_ECOSYSTEMS:
            continue
        directories = entry.get("directories") or [entry.get("directory")]
        if directory in directories:
            matches.append(entry)
    return matches


class DependabotUvLockCoupling(unittest.TestCase):
    def test_the_uv_lock_gate_still_exists(self) -> None:
        """Premise check, so the assertions below cannot pass vacuously."""
        self.assertTrue(
            (ROOT / "app/api/uv.lock").is_file(),
            "app/api/uv.lock is gone; revisit whether this coupling is still needed.",
        )
        self.assertIn(
            "uv lock --check",
            APP_CI.read_text(encoding="utf-8"),
            "app-ci no longer gates uv.lock drift. If that was deliberate, this "
            "whole module can go; if not, the lock is free to rot again.",
        )

    def test_app_api_is_managed_by_a_uv_aware_ecosystem(self) -> None:
        entries = _python_entries_for(API_DIR)
        self.assertTrue(
            entries, f"No Python Dependabot entry covers {API_DIR}."
        )
        for entry in entries:
            ecosystem = entry.get("package-ecosystem")
            self.assertIn(
                ecosystem,
                UV_AWARE_ECOSYSTEMS,
                f"{API_DIR} is managed by '{ecosystem}', which does not update "
                "uv.lock. Every dependency PR it opens will fail app-ci's "
                "`uv lock --check`. Use `package-ecosystem: uv` instead.",
            )

    def test_only_one_python_ecosystem_manages_app_api(self) -> None:
        """Two Python ecosystems on one directory means duplicate PRs per bump.

        Adding `pip` back "for safety" alongside `uv` looks harmless and is not:
        both would open a PR for the same dependency, and the pip one would fail
        CI every time.
        """
        entries = _python_entries_for(API_DIR)
        self.assertEqual(
            len(entries),
            1,
            f"Expected exactly one Python Dependabot entry for {API_DIR}, found "
            f"{len(entries)}: {[e.get('package-ecosystem') for e in entries]}.",
        )


class DependabotNuGetLockCoupling(unittest.TestCase):
    def test_all_proxy_projects_have_lock_files(self) -> None:
        for directory in PROXY_PROJECT_DIRS:
            self.assertTrue(
                (ROOT / directory.lstrip("/") / "packages.lock.json").is_file(),
                f"{directory} is missing packages.lock.json",
            )

    def test_one_nuget_entry_covers_every_proxy_project(self) -> None:
        entries = [
            entry
            for entry in _updates()
            if entry.get("package-ecosystem") == "nuget"
            and PROXY_PROJECT_DIRS.intersection(
                set(entry.get("directories") or [entry.get("directory")])
            )
        ]
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(set(entries[0]["directories"]), PROXY_PROJECT_DIRS)

    def test_ci_restores_the_proxy_in_locked_mode(self) -> None:
        quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
        self.assertIn(
            "dotnet restore proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj "
            "--locked-mode",
            quality,
        )


if __name__ == "__main__":
    unittest.main()
