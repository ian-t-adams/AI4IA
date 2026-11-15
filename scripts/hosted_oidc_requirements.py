"""Derive the hosted reader's hash-checked SDK closure from the API lock."""

import copy
import sys
import tomllib
from pathlib import Path

from scripts.http_dependencies import requirements


def render(lock):
    lock = copy.deepcopy(lock)
    packages = {p["name"]: p for p in lock["package"]}
    if len(packages) != len(lock["package"]):
        raise ValueError("ambiguous lock")
    msal = packages["msal"]
    pyjwt = packages["pyjwt"]
    edges = [d for d in msal["dependencies"] if d.get("name") == "pyjwt"]
    crypto = pyjwt.get("optional-dependencies", {}).get("crypto")
    if edges != [{"name": "pyjwt", "extra": ["crypto"]}] or crypto != [{"name": "cryptography"}]:
        raise ValueError("unreviewed SDK extra")
    if {"name": "cryptography"} not in msal["dependencies"]:
        raise ValueError("missing crypto dependency")
    edges[0].pop("extra")
    return requirements(lock, ("azure-identity",))


def main():
    lock = tomllib.loads(Path("app/api/uv.lock").read_text(encoding="utf-8"))
    sys.stdout.write(render(lock))


if __name__ == "__main__":
    main()
