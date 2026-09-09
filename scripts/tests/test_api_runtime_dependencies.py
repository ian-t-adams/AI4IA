"""Exercise the API Dockerfile's real lock guard with CI's pinned uv.

All subprocesses are offline and use isolated temporary environments. The
dependency-free fixture proves missing/stale locks are rejected for the guard,
not for an unrelated network failure. The real API lock supplies the runtime
selection control; docker-build separately exercises installation and imports.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "app" / "api"
DOCKERFILE = API / "Dockerfile"


def _instructions() -> list[list[str]]:
    source = re.sub(r"\\\r?\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    return [
        shlex.split(line, comments=True)
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _uv_commands() -> list[list[str]]:
    commands: list[list[str]] = []
    for instruction in _instructions():
        if instruction[0] != "RUN":
            continue
        command: list[str] = []
        for token in [*instruction[1:], "&&"]:
            if token == "&&":
                if command[:1] == ["uv"]:
                    commands.append(command)
                command = []
            else:
                command.append(token)
    return commands


class ApiRuntimeDockerContractTests(unittest.TestCase):
    def test_freshness_precedes_both_frozen_runtime_only_syncs(self) -> None:
        commands = _uv_commands()
        self.assertEqual([command[:2] for command in commands], [
            ["uv", "lock"], ["uv", "sync"], ["uv", "sync"],
        ])
        self.assertEqual(commands[0], ["uv", "lock", "--check", "--offline"])
        for command in commands[1:]:
            self.assertTrue(
                {"--frozen", "--no-dev", "--no-default-groups"} <= set(command), command,
            )
            self.assertFalse(
                {"--extra", "--all-extras", "--group", "--all-groups", "--inexact"} & set(command),
                command,
            )
        self.assertIn("--no-install-project", commands[1])
        self.assertIn("--no-editable", commands[2])
        instructions = _instructions()
        self.assertIn(["COPY", "pyproject.toml", "uv.lock", "README.md", "./"], instructions)
        installers = [
            instruction for instruction in instructions
            if instruction[0] == "RUN" and "pip" in instruction
        ]
        self.assertEqual(installers, [
            ["RUN", "python", "-m", "pip", "install", "uv==${UV_VERSION}"],
        ], "No pip/range-resolving fallback may replace the frozen runtime.")

    def test_runtime_copies_only_the_built_environment_and_keeps_non_root_user(self) -> None:
        instructions = _instructions()
        runtime_start = instructions.index(["FROM", "base", "AS", "runtime"])
        runtime = instructions[runtime_start:]
        self.assertEqual(
            [instruction for instruction in runtime if instruction[0] == "COPY"],
            [["COPY", "--from=build", "/opt/venv", "/opt/venv"]],
        )
        self.assertIn(["ENV", "PATH=/opt/venv/bin:$PATH"], runtime)
        self.assertIn(["RUN", "useradd", "--create-home", "--uid", "10001", "appuser"], runtime)
        self.assertIn(["USER", "appuser"], runtime)
        environment = {
            token.split("=", 1)[0]: token.split("=", 1)[1]
            for instruction in instructions if instruction[0] == "ENV"
            for token in instruction[1:]
        }
        self.assertEqual(environment["UV_PYTHON_DOWNLOADS"], "never")
        self.assertEqual(environment["UV_PYTHON"], "/usr/local/bin/python")
        self.assertEqual(environment["UV_PROJECT_ENVIRONMENT"], "/opt/venv")
        self.assertEqual(environment["UV_DEFAULT_INDEX"], "https://pypi.org/simple")
        self.assertEqual(environment["UV_NO_CONFIG"], "1")
        self.assertEqual(environment["UV_NO_CACHE"], "1")

    def test_ci_runs_the_guard_and_extra_free_import_checks(self) -> None:
        app_ci = (ROOT / ".github/workflows/app-ci.yml").read_text(encoding="utf-8")
        self.assertIn("python -m unittest scripts.tests.test_api_runtime_dependencies", app_ci)
        docker_ci = (ROOT / ".github/workflows/docker-build.yml").read_text(encoding="utf-8")
        self.assertIn('docker run --rm ai4ia-api:ci python -c "import ai4ia_api.main"', docker_ci)
        self.assertIn("docker run --rm --interactive ai4ia-api:ci python -", docker_ci)
        self.assertIn("< app/api/tests/test_lazy_imports_are_declared.py", docker_ci)


class ApiRuntimeLockBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.uv = shutil.which("uv")
        if cls.uv is None:
            raise RuntimeError("Install app-ci.yml's pinned UV_VERSION to run this gate.")
        expected = re.search(
            r"^ARG UV_VERSION=(\S+)$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE,
        )
        assert expected is not None, "API Dockerfile must declare UV_VERSION"
        version = subprocess.run(
            [cls.uv, "--version"], capture_output=True, text=True, check=True, timeout=30,
        ).stdout.split()[1]
        if version != expected[1]:
            raise RuntimeError(f"Expected uv {expected[1]} from app-ci, found {version}.")

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ai4ia-runtime-lock-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("UV_", "PIP_", "PYTHON"))
            and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
        }
        self.environment.update({
            "UV_PYTHON": sys.executable,
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_PROJECT_ENVIRONMENT": str(self.project / ".venv"),
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_NO_CONFIG": "1",
            "UV_NO_CACHE": "1",
            "UV_OFFLINE": "1",
            "NO_COLOR": "1",
        })
        self.guard = _uv_commands()[0]

    def run_uv(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.uv, *command[1:]], cwd=self.project, env=self.environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def copy_api_fixture(self) -> Path:
        for name in ("pyproject.toml", "uv.lock", "README.md"):
            shutil.copyfile(API / name, self.project / name)
        return self.project / "uv.lock"

    def planned_dependencies(self, *flags: str) -> dict[str, str]:
        result = self.run_uv([*_uv_commands()[1], "--dry-run", *flags])
        self.assert_success(result)
        selected = dict(re.findall(
            r"^\s*\+ ([\w.-]+)==(\S+)", result.stdout + result.stderr, re.MULTILINE,
        ))
        self.assertGreater(len(selected), 70, "runtime selection must not pass vacuously")
        return selected

    def fixture(self) -> Path:
        (self.project / "pyproject.toml").write_text(
            '[project]\nname = "lock-control"\nversion = "0.1.0"\n'
            'requires-python = ">=3.12"\ndependencies = []\n',
            encoding="utf-8",
        )
        self.assert_success(self.run_uv(["uv", "lock", "--offline"]))
        lock = self.project / "uv.lock"
        before = lock.read_bytes()
        self.assert_success(self.run_uv(self.guard))
        self.assertEqual(lock.read_bytes(), before)
        return lock

    def test_missing_lock_is_refused_not_created(self) -> None:
        lock = self.fixture()
        lock.unlink()
        result = self.run_uv(self.guard)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("lockfile", result.stderr.lower())
        self.assertFalse(lock.exists(), "the image must not synthesize a missing lock")
        # Same fixture with the guard off really can create a lock, offline.
        self.assert_success(self.run_uv(["uv", "lock", "--offline"]))
        self.assertTrue(lock.is_file())

    def test_stale_manifest_is_refused_even_though_frozen_sync_would_accept_it(self) -> None:
        lock = self.fixture()
        before = lock.read_bytes()
        manifest = self.project / "pyproject.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace('version = "0.1.0"', 'version = "0.1.1"'),
            encoding="utf-8",
        )
        result = self.run_uv(self.guard)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("lockfile", result.stderr.lower())
        self.assertEqual(lock.read_bytes(), before)
        self.assert_success(self.run_uv([*_uv_commands()[1], "--dry-run"]))
        self.assertEqual(lock.read_bytes(), before, "--frozen must not rewrite stale metadata")

    def test_changed_api_dependency_range_is_refused_without_rewriting(self) -> None:
        lock = self.copy_api_fixture()
        before = lock.read_bytes()
        self.assert_success(self.run_uv(self.guard))
        manifest = self.project / "pyproject.toml"
        original = manifest.read_text(encoding="utf-8")
        requirement = next(
            dependency for dependency in tomllib.loads(original)["project"]["dependencies"]
            if dependency.startswith("fastapi")
        )
        widened = requirement.replace("<", "<=", 1)
        self.assertNotEqual(requirement, widened, "FastAPI must retain its reviewed upper bound")
        stale = original.replace(f'"{requirement}"', f'"{widened}"', 1)
        self.assertNotEqual(original, stale, "the constraint mutation must actually change input")
        manifest.write_text(stale, encoding="utf-8")
        result = self.run_uv(self.guard)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(lock.read_bytes(), before)
        self.planned_dependencies()
        self.assertEqual(lock.read_bytes(), before)

    def test_real_api_lock_selects_only_locked_runtime_dependencies(self) -> None:
        lock = self.copy_api_fixture()
        before = lock.read_bytes()
        self.assert_success(self.run_uv(self.guard))
        selected = self.planned_dependencies()
        locked = tomllib.loads(before.decode("utf-8"))["package"]
        versions = {(package["name"], package["version"]) for package in locked}
        self.assertTrue(set(selected.items()) <= versions, "all versions must come from uv.lock")
        project = next(package for package in locked if package["name"] == "ai4ia-api")
        self.assertTrue({dependency["name"] for dependency in project["dependencies"]} <= selected.keys())
        self.assertFalse({
            "ai4ia-api", "azure-ai-projects", "pyright", "pytest", "pytest-asyncio", "ruff",
        } & selected.keys())
        self.assertIn("anyio", selected, "shared runtime/dev dependencies must not be dropped")
        self.assertIn("azure-identity", selected, "shared runtime/foundry dependencies must survive")
        with_extras = self.planned_dependencies("--extra", "dev", "--extra", "foundry")
        self.assertTrue({"pytest", "ruff", "pyright", "azure-ai-projects"} <= with_extras.keys())
        self.assertEqual(lock.read_bytes(), before)

    def test_runtime_extras_follow_the_target_platform_not_the_build_host(self) -> None:
        lock = self.copy_api_fixture()
        before = lock.read_bytes()
        linux = self.planned_dependencies("--python-platform", "linux")
        windows = self.planned_dependencies("--python-platform", "windows")
        self.assertIn("uvloop", linux, "uvicorn[standard] needs its Linux event loop")
        self.assertNotIn("uvloop", windows)
        self.assertIn("colorama", windows)
        self.assertNotIn("colorama", linux)
        self.assertEqual(lock.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
