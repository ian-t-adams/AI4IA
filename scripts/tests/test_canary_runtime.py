"""The canary jobs' declared runtime must replay the incident and its recovery.

Run 36267220637's observation job installed only aiohttp. Every offline unit test
injected a synthetic price book, so none executed the real loader, whose lazy
catalog import needs pydantic: the first live observation failed after its OIDC
exchange and one read-only catalog request. Importing the package still passed.
This builds a fresh virtual environment containing only what
application-canaries.yml installs and drives the real CLI through it, from
bootstrap through that lost observation, the durable block and the owner-attested
resolution to a scored observation, with only the network faked. No model call,
credential, OIDC token or Azure access.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

# Deliberately a hard import: a skipped gate would report success while
# checking nothing. The quality job installs PyYAML.
import yaml

from scripts.canaries import dependencies

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "application-canaries.yml"
PROBE = "scripts.tests._canary_runtime_probe"
MARK = "CANARY-RUNTIME-PROBE "
# Every application module the id-token job may load: stdlib-only constants and
# the price calculator. Configuration, settings, the pydantic catalog or any
# other API module joining this list is a reviewed change, never an incidental one.
API_MODULES = [
    "ai4ia_api", "ai4ia_api.realtime_canary", "ai4ia_api.request_constraints",
    "app.api.src.ai4ia_api", "app.api.src.ai4ia_api.usage", "app.api.src.ai4ia_api.usage.pricing",
]
INSTALL = re.compile(r"\bpip\b|\bscripts\.canaries\.dependencies\b")
COMMAND = re.compile(r"\bpython3?\s+-m\s+scripts\.canaries\s")
# The child inherits index/proxy settings, never runner identity or Python paths.
RUNNER = re.compile(r"(?:GITHUB|ACTIONS|RUNNER|CONDA)_|PYTHON|VIRTUAL_ENV\Z", re.IGNORECASE)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_runtime() -> tuple[str, list[str]]:
    """The Python version and install command both canary jobs share."""
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    declared = set()
    for name in ("prepare", "observe"):
        steps = document["jobs"][name]["steps"]
        setups = [i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@")]
        installs = [i for i, step in enumerate(steps) if INSTALL.search(step.get("run", ""))]
        commands = [i for i, step in enumerate(steps) if COMMAND.search(step.get("run", ""))]
        if len(setups) != 1 or len(installs) != 1 or not commands:
            raise AssertionError(f"{name}: expected one Python setup and one dependency install step.")
        if not setups[0] < installs[0] < commands[0]:
            raise AssertionError(f"{name}: dependencies must be installed before any canary command.")
        declared.add((str(steps[setups[0]]["with"]["python-version"]), steps[installs[0]]["run"].strip()))
    if len(declared) != 1:
        raise AssertionError("The prepare and observe jobs must declare one identical runtime.")
    version, command = declared.pop()
    return version, shlex.split(command)


def child_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not RUNNER.match(key)}
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    return env


def synthetic_lock(*packages: dict) -> dict:
    wheel = {"hash": "sha256:" + "0" * 64}
    return {"package": [
        {"source": {"registry": dependencies.REGISTRY}, "version": "1.0", "wheels": [wheel], **package}
        for package in packages
    ]}


class LockDerivedInstallTests(unittest.TestCase):
    def test_both_jobs_install_the_lock_derived_runtime_before_any_command(self) -> None:
        version, command = declared_runtime()
        self.assertEqual(command, ["python", "-m", "scripts.canaries.dependencies"])
        self.assertRegex(version, r"^3\.\d+$")

    def test_markers_compose_along_paths_and_an_unconditional_path_absorbs_them(self) -> None:
        newer, windows, pypy = "python_full_version < '3.13'", "sys_platform == 'win32'", "implementation_name == 'pypy'"
        lock = synthetic_lock(
            {"name": "root", "dependencies": [{"name": "a", "marker": newer}, {"name": "b"}]},
            {"name": "a", "dependencies": [{"name": "shared", "marker": windows}]},
            {"name": "b", "dependencies": [{"name": "shared", "marker": pypy}, {"name": "c", "marker": pypy}]},
            {"name": "c", "dependencies": [{"name": "b"}]},
            {"name": "shared"},
            {"name": "unrelated"},
        )
        needed = {name: condition for name, (_, condition) in dependencies.closure(lock, ("root",)).items()}
        self.assertEqual(set(needed), {"root", "a", "b", "c", "shared"})
        self.assertEqual(needed["b"], dependencies.ALWAYS)
        self.assertEqual(needed["a"], frozenset({frozenset({newer})}))
        self.assertEqual(needed["shared"], frozenset({frozenset({newer, windows}), frozenset({pypy})}))
        text = dependencies.requirements(lock, ("root",))
        self.assertIn(f"a==1.0 ; {newer} \\", text)
        self.assertIn(f"shared==1.0 ; (({newer}) and ({windows})) or ({pypy}) \\", text)
        self.assertIn("root==1.0 \\\n", text)
        # Control: one unconditional path makes the dependency unconditional.
        lock["package"][0]["dependencies"].append({"name": "shared"})
        absorbed = dependencies.closure(lock, ("root",))
        self.assertEqual(absorbed["shared"][1], dependencies.ALWAYS)

    def test_ambiguous_incomplete_or_non_pypi_locks_refuse(self) -> None:
        valid = synthetic_lock({"name": "root", "dependencies": [{"name": "leaf"}]}, {"name": "leaf"})
        dependencies.requirements(valid, ("root",))
        cases = {
            "duplicate": lambda lock: lock["package"].append(dict(lock["package"][1])),
            "missing": lambda lock: lock["package"].pop(),
            "source": lambda lock: lock["package"][1].update(source={"git": "https://example.test/leaf"}),
            "extra": lambda lock: lock["package"][0]["dependencies"][0].update(extra=["speedups"]),
            "version": lambda lock: lock["package"][0]["dependencies"][0].update(version="1.0"),
            "hash": lambda lock: lock["package"][1].update(wheels=[{"hash": "md5:00"}]),
            "no hash": lambda lock: lock["package"][1].update(wheels=[]),
        }
        for label, mutate in cases.items():
            lock = json.loads(json.dumps(valid))
            mutate(lock)
            with self.subTest(label), self.assertRaises(ValueError):
                dependencies.requirements(lock, ("root",))

    def test_real_lock_closure_pins_every_recorded_artifact_hash(self) -> None:
        lock = tomllib.loads(dependencies.LOCK.read_text(encoding="utf-8"))
        packages = {package["name"]: package for package in lock["package"]}
        needed = dependencies.closure(lock)
        self.assertIn("aiohttp", needed)
        for framework in ("pydantic", "pydantic-settings", "fastapi"):
            self.assertNotIn(framework, needed, "the canary runtime must stay free of the API framework")
        blocks = re.findall(r"^(\S+)==(\S+)[^\n]*((?:\n    --hash=\S+(?: \\)?)+)", dependencies.requirements(lock), re.M)
        self.assertEqual([name for name, _, _ in blocks], sorted(needed))
        for name, version, hashes in blocks:
            package = packages[name]
            recorded = {artifact["hash"] for artifact in [package.get("sdist"), *package.get("wheels", [])] if artifact}
            self.assertEqual(version, package["version"])
            self.assertEqual(set(re.findall(r"--hash=(\S+)", hashes)), recorded, name)
            for dependency in package.get("dependencies", []):
                self.assertIn(dependency["name"], needed, f"{name} -> {dependency['name']}")

    def test_install_is_hash_checked_binary_only_and_uses_the_running_interpreter(self) -> None:
        seen: dict[str, object] = {}

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            seen["command"] = command
            seen["requirements"] = Path(command[command.index("--requirement") + 1]).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(dependencies.subprocess, "run", side_effect=run), patch("builtins.print"):
            self.assertEqual(dependencies.main(), 0)
        command = seen["command"]
        assert isinstance(command, list)
        self.assertEqual(command[:3], [sys.executable, "-m", "pip"])
        for flag in ("--require-hashes", "--only-binary=:all:", "--no-input"):
            self.assertIn(flag, command)
        lock = tomllib.loads(dependencies.LOCK.read_text(encoding="utf-8"))
        self.assertEqual(seen["requirements"], dependencies.requirements(lock))


class DeclaredRuntimeLifecycleTests(unittest.TestCase):
    def test_clean_declared_runtime_runs_a_complete_synthetic_lifecycle(self) -> None:
        version, command = declared_runtime()
        self.assertEqual(
            f"{sys.version_info.major}.{sys.version_info.minor}", version,
            "Run this guard on the canary jobs' declared Python; another interpreter does not model them.",
        )
        env = child_environment()
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run([sys.executable, "-m", "venv", temporary], check=True, env=env, timeout=300)
            python = str(Path(temporary) / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            # Execute the workflow's own install command with this environment's interpreter.
            install = subprocess.run(
                [python, *command[1:]], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            listed = subprocess.run(
                [python, "-m", "pip", "list", "--format=json"], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=120, check=True,
            )
            probe = subprocess.run(
                [python, "-m", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
            )
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        installed = {canonical(row["name"]): row["version"] for row in json.loads(listed.stdout)}
        installed.pop("pip")
        lock = tomllib.loads(dependencies.LOCK.read_text(encoding="utf-8"))
        closure = dependencies.closure(lock)
        locked = {canonical(name): package["version"] for name, (package, _) in closure.items()}
        self.assertLessEqual(installed.items(), locked.items(), "only the lock-derived closure may be present")
        unconditional = {canonical(name) for name, (_, condition) in closure.items() if condition == dependencies.ALWAYS}
        self.assertLessEqual(unconditional, set(installed))

        lines = [line for line in probe.stdout.splitlines() if line.startswith(MARK)]
        self.assertEqual(len(lines), 1, probe.stdout)
        summary = json.loads(lines[0][len(MARK):])
        self.assertEqual(summary["python"], version)
        incident = summary["incident"]
        # The replayed loss read the catalog once, wrote nothing and left no state...
        self.assertEqual(incident["lost_requests"], ["GET /api/models"])
        self.assertFalse(incident["lost_state_written"])
        # ...so the next schedule blocked, and only the approved record resolved it.
        self.assertEqual(incident["blocked"], [True, ["state_missing"]])
        self.assertEqual(incident["resolved"], ["resolved", False, 0])
        self.assertTrue(incident["resolution_matches"])
        self.assertEqual(summary["api_modules"], API_MODULES)
        self.assertEqual(summary["outside"], [], "a module was loaded from outside the declared runtime")
        self.assertEqual(summary["coverage"], "complete", summary["stages"])
        for stage, (outcome, _) in summary["stages"].items():
            self.assertEqual(outcome, "pass", stage)
        self.assertEqual(summary["stages"]["cleanup"], ["pass", "cleanup_verified"])
        self.assertEqual((summary["observations"], summary["blocked"], summary["control"]), (1, False, "observe"))
        for counter in (summary["chat"], summary["realtime"]):
            self.assertEqual(counter, {"failures": 0, "alerting": False, "transition": "none"})
        self.assertEqual(summary["attempts"], [1, 1])
        self.assertTrue(summary["usage_known"] and summary["price_version_matches"])
        session = json.loads(
            (ROOT / "scripts" / "fixtures" / "conversation-cleanup.json").read_text(encoding="utf-8")
        )["session"]["id"]
        # One create, one chat dispatch and the exact-id v1 cleanup; nothing else writes.
        self.assertEqual(summary["writes"], sorted([
            "POST /api/sessions", "POST /api/chat", f"DELETE /api/sessions/{session}",
            f"POST /api/sessions/{session}/deletion/reconcile",
        ]))
        self.assertEqual((summary["sockets"], summary["setup_frames"]), (1, True))
        self.assertIn("aiohttp", summary["third_party"])


if __name__ == "__main__":
    unittest.main()
