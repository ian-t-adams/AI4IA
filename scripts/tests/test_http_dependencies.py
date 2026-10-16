"""The two public-health publishers install only the locked HTTP dependency closure."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import http_dependencies as dependencies

ROOT = Path(__file__).resolve().parents[2]


def synthetic_lock(*packages: dict) -> dict:
    wheel = {"hash": "sha256:" + "0" * 64}
    return {"package": [
        {"source": {"registry": dependencies.REGISTRY}, "version": "1.0", "wheels": [wheel], **package}
        for package in packages
    ]}


class HttpDependencyTests(unittest.TestCase):
    def test_publishers_install_the_same_runtime_before_network_commands(self):
        for filename, job in (("uptime.yml", "observe"), ("pages.yml", "build")):
            document = yaml.safe_load((ROOT / ".github" / "workflows" / filename).read_text())
            steps = document["jobs"][job]["steps"]
            install = [index for index, step in enumerate(steps)
                       if step.get("run") == "python -m scripts.http_dependencies"]
            commands = [index for index, step in enumerate(steps)
                        if "python -m scripts.uptime " in step.get("run", "")]
            self.assertEqual(len(install), 1)
            self.assertTrue(commands)
            self.assertLess(install[0], min(commands))

    def test_markers_compose_along_paths_and_an_unconditional_path_absorbs_them(self):
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
        lock["package"][0]["dependencies"].append({"name": "shared"})
        self.assertEqual(dependencies.closure(lock, ("root",))["shared"][1], dependencies.ALWAYS)

    def test_ambiguous_incomplete_or_non_pypi_locks_refuse(self):
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

    def test_real_lock_closure_pins_every_recorded_artifact_hash(self):
        lock = tomllib.loads(dependencies.LOCK.read_text(encoding="utf-8"))
        packages = {package["name"]: package for package in lock["package"]}
        needed = dependencies.closure(lock)
        self.assertIn("aiohttp", needed)
        self.assertFalse({"pydantic", "pydantic-settings", "fastapi"} & set(needed))
        blocks = re.findall(r"^(\S+)==(\S+)[^\n]*((?:\n    --hash=\S+(?: \\)?)+)", dependencies.requirements(lock), re.M)
        self.assertEqual([name for name, _, _ in blocks], sorted(needed))
        for name, version, hashes in blocks:
            package = packages[name]
            recorded = {artifact["hash"] for artifact in [package.get("sdist"), *package.get("wheels", [])] if artifact}
            self.assertEqual(version, package["version"])
            self.assertEqual(set(re.findall(r"--hash=(\S+)", hashes)), recorded, name)
            for dependency in package.get("dependencies", []):
                self.assertIn(dependency["name"], needed, f"{name} -> {dependency['name']}")

    def test_install_is_hash_checked_binary_only_and_uses_the_running_interpreter(self):
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


if __name__ == "__main__":
    unittest.main()
