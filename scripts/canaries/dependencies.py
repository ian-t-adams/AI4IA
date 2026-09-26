"""Install the canary's only third-party runtime, hash-checked from app/api/uv.lock.

Both canary jobs run this before any canary command. The API lock is the single
version and artifact-hash source: this derives the closure of ``DIRECT`` from it
and never resolves, upgrades or builds anything. ``--require-hashes`` makes pip
refuse an artifact, or a transitive dependency, that the lock does not record;
``--only-binary`` refuses source builds. Everything else the canary runs is the
standard library or repository source that must stay free of API dependencies.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "app" / "api" / "uv.lock"
DIRECT = ("aiohttp",)
REGISTRY = "https://pypi.org/simple"
PIP_ARGUMENTS = (
    "install", "--quiet", "--no-input", "--disable-pip-version-check",
    "--require-hashes", "--only-binary=:all:",
)
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")

# Disjunction of conjunctions of PEP 508 markers; {frozenset()} is unconditional.
Condition = frozenset[frozenset[str]]
ALWAYS: Condition = frozenset({frozenset()})


def _simplify(condition: Condition) -> Condition:
    # Absorption: a clause implied by a shorter one adds nothing.
    return frozenset(
        clause for clause in condition
        if not any(other < clause for other in condition)
    )


def _both(left: Condition, right: Condition) -> Condition:
    return _simplify(frozenset(a | b for a in left for b in right))


def closure(lock: dict[str, Any], direct: tuple[str, ...] = DIRECT) -> dict[str, tuple[dict[str, Any], Condition]]:
    """Each package reachable from ``direct`` with the markers under which it is needed."""
    packages: dict[str, dict[str, Any]] = {}
    for package in lock.get("package", []):
        if package["name"] in packages:
            raise ValueError(f"{package['name']} has several lock entries; select one explicitly.")
        packages[package["name"]] = package
    needed: dict[str, Condition] = {}
    pending = [(name, ALWAYS) for name in direct]
    while pending:
        name, condition = pending.pop()
        known = needed.get(name, frozenset())
        merged = _simplify(known | condition)
        if merged == known:
            continue
        needed[name] = merged
        package = packages.get(name)
        if package is None:
            raise ValueError(f"{name} is required but absent from the lock.")
        if package.get("source") != {"registry": REGISTRY}:
            raise ValueError(f"{name} is not locked to public PyPI.")
        for dependency in package.get("dependencies", []):
            if set(dependency) - {"name", "marker"}:
                raise ValueError(f"{name} -> {dependency['name']}: unsupported lock dependency form.")
            marker = dependency.get("marker")
            edge = frozenset({frozenset({marker})}) if marker else ALWAYS
            pending.append((dependency["name"], _both(merged, edge)))
    return {name: (packages[name], needed[name]) for name in sorted(needed)}


def _marker(condition: Condition) -> str:
    if condition == ALWAYS:
        return ""
    clauses = sorted(
        " and ".join(f"({marker})" if len(clause) > 1 else marker for marker in sorted(clause))
        for clause in condition
    )
    return " ; " + (clauses[0] if len(clauses) == 1 else " or ".join(f"({c})" for c in clauses))


def requirements(lock: dict[str, Any], direct: tuple[str, ...] = DIRECT) -> str:
    lines = ["# Derived from app/api/uv.lock by scripts/canaries/dependencies.py."]
    for name, (package, condition) in closure(lock, direct).items():
        artifacts = [package.get("sdist"), *package.get("wheels", [])]
        hashes = sorted({artifact["hash"] for artifact in artifacts if artifact})
        if not hashes or not all(_HASH.fullmatch(value) for value in hashes):
            raise ValueError(f"{name} has no complete sha256 artifact hashes in the lock.")
        lines.append(f"{name}=={package['version']}{_marker(condition)} \\")
        lines.extend(f"    --hash={value} \\" for value in hashes[:-1])
        lines.append(f"    --hash={hashes[-1]}")
    return "\n".join(lines) + "\n"


def main() -> int:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    pins = ", ".join(f"{name}=={package['version']}" for name, (package, _) in closure(lock).items())
    print(f"Hash-checked canary runtime from app/api/uv.lock: {pins}.", flush=True)
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "canary-requirements.txt"
        path.write_text(requirements(lock), encoding="utf-8")
        command = [sys.executable, "-m", "pip", *PIP_ARGUMENTS, "--requirement", str(path)]
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
