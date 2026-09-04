#!/usr/bin/env python3
"""Verify every Azure resource provider this stack deploys or queries is registered.

A subscription that has never hosted a given resource type has that provider in
`NotRegistered`, and ARM rejects the deployment referencing it. Because
`azd provision` submits the template and *then* fails on the first unregistered
provider, a new-subscription standup does not fail cleanly -- it fails partway,
leaving a half-built resource group that the next attempt has to reconcile.
Checking up front is the difference between a five-second error and a partial
deploy. An unregistered operational provider instead makes its evidence plane
unavailable; the same preflight handles both cases.

The deployed-resource set is derived from infra/**/*.bicep, not hand-maintained.
Operational APIs with no Bicep resource declaration are explicit, evidence-backed
additions. A broad hardcoded list is exactly the kind of thing that silently rots
when someone adds a resource type.

Usage:
    python scripts/check-resource-providers.py            # check, exit 1 if any unregistered
    python scripts/check-resource-providers.py --register # register missing ones, then wait
    python scripts/check-resource-providers.py --list     # print the required set and exit
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"

# `resource <symbol> '<Namespace>/<type>@<apiVersion>'` and the `existing` form.
RESOURCE_RE = re.compile(
    r"^\s*resource\s+\w+\s+'(?P<namespace>Microsoft\.[A-Za-z0-9]+)/",
    re.MULTILINE,
)

# Always present in an Azure subscription; never needs registering, and asking
# about them just adds noise to the report.
ALWAYS_REGISTERED = frozenset(
    {
        "Microsoft.Authorization",  # role assignments -- built into ARM
        "Microsoft.Resources",  # resource groups / deployments
        "Microsoft.Consumption",  # budgets
    }
)

# Namespaces the deployed app's operational workflows query even though no Bicep
# `resource` declaration names them. Keep this evidence-backed and minimal.
IMPLICIT: dict[str, str] = {
    "Microsoft.ResourceHealth": (
        "the status snapshot queries Resource Health availability statuses"
    ),
}


def required_namespaces() -> dict[str, list[str]]:
    """Map each required namespace to the sources that require it."""
    found: dict[str, list[str]] = {}
    for path in sorted(INFRA.rglob("*.bicep")):
        text = path.read_text(encoding="utf-8")
        for match in RESOURCE_RE.finditer(text):
            namespace = match.group("namespace")
            if namespace in ALWAYS_REGISTERED:
                continue
            found.setdefault(namespace, [])
            rel = path.relative_to(ROOT).as_posix()
            if rel not in found[namespace]:
                found[namespace].append(rel)
    for namespace, reason in IMPLICIT.items():
        found.setdefault(namespace, []).append(f"(implicit: {reason})")
    return dict(sorted(found.items()))


def _az(*args: str) -> subprocess.CompletedProcess[str]:
    # Resolve the executable rather than relying on the OS to find "az" on
    # PATH. On Windows the CLI ships as az.cmd, and CreateProcess will not run a
    # batch file given a bare name -- subprocess raises FileNotFoundError even
    # though shutil.which() just found it.
    executable = shutil.which("az")
    if executable is None:
        raise SystemExit(
            "ERROR: the Azure CLI (az) is not on PATH. Install it, run `az login`, "
            "and select the target subscription with `az account set --subscription <id>`."
        )
    return subprocess.run(
        [executable, *args], capture_output=True, text=True, check=False, encoding="utf-8"
    )


def registration_states() -> dict[str, str]:
    result = _az("provider", "list", "--query", "[].{ns:namespace,state:registrationState}", "-o", "json")
    if result.returncode != 0:
        raise SystemExit(
            "ERROR: could not list resource providers. Run `az login` and "
            f"`az account set --subscription <id>` first.\n{result.stderr.strip()}"
        )
    # Keyed casefolded on purpose. Resource provider namespaces are
    # case-insensitive and ARM is not self-consistent about them: the templates
    # declare `Microsoft.Insights/...`, but `az provider list` returns that one
    # as `microsoft.insights` (lowercase) while returning its siblings
    # `Microsoft.OperationalInsights` and `Microsoft.PolicyInsights` in Pascal
    # case. An exact-match lookup reports a registered provider as missing --
    # a preflight that cries wolf is a preflight operators learn to skip.
    return {
        entry["ns"].casefold(): entry["state"]
        for entry in json.loads(result.stdout or "[]")
    }


def state_of(states: dict[str, str], namespace: str) -> str:
    return states.get(namespace.casefold(), "NotFound")


def register(namespaces: list[str], timeout_seconds: int = 600) -> int:
    # This function polls for minutes. Python block-buffers stdout when it is a
    # pipe rather than a TTY, so without flushing an operator watching a CI log
    # or a captured shell sees nothing at all until the whole thing finishes.
    def progress(message: str) -> None:
        print(message, flush=True)

    for namespace in namespaces:
        progress(f"Registering {namespace} ...")
        result = _az("provider", "register", "--namespace", namespace)
        if result.returncode != 0:
            print(f"ERROR: failed to request registration for {namespace}", file=sys.stderr)
            print(result.stderr.strip(), file=sys.stderr)
            return 1

    # Registration is asynchronous. Returning before it completes would just move
    # the partial-deploy failure to the caller.
    deadline = time.monotonic() + timeout_seconds
    pending = list(namespaces)
    while pending and time.monotonic() < deadline:
        time.sleep(10)
        states = registration_states()
        still = [ns for ns in pending if state_of(states, ns) != "Registered"]
        for done in [ns for ns in pending if ns not in still]:
            progress(f"  {done}: Registered")
        pending = still
        if pending:
            progress(f"  waiting on {len(pending)}: {', '.join(sorted(pending))}")

    if pending:
        print(
            "\nWARNING: still not Registered after "
            f"{timeout_seconds}s: {', '.join(sorted(pending))}.\n"
            "Registration can take several minutes. Re-run this script to re-check.",
            file=sys.stderr,
        )
        return 1
    progress("\nAll required resource providers are registered.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--register",
        action="store_true",
        help="register any missing providers and wait for them to become Registered",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print required deployment and operational namespaces, then exit",
    )
    args = parser.parse_args()

    required = required_namespaces()

    if args.list:
        for namespace, sources in required.items():
            print(f"{namespace}\n    {', '.join(sources)}")
        print(f"\n{len(required)} namespaces required.")
        return 0

    states = registration_states()
    missing: list[str] = []
    registering: list[str] = []
    for namespace in required:
        state = state_of(states, namespace)
        if state == "Registered":
            continue
        if state == "Registering":
            registering.append(namespace)
        else:
            missing.append(namespace)

    if not missing and not registering:
        print(f"All {len(required)} required resource providers are registered.")
        return 0

    if registering:
        print(
            "ERROR: these providers are still registering; wait for them to finish "
            "before continuing:\n  " + "\n  ".join(sorted(registering)),
            file=sys.stderr,
        )
    if missing:
        print(
            "ERROR: these resource providers are not registered in the selected "
            "subscription; required deployment or operational behavior will fail:\n  "
            + "\n  ".join(f"{ns}  ({', '.join(required[ns])})" for ns in sorted(missing)),
            file=sys.stderr,
        )
        print(
            "\nFix with:\n  python scripts/check-resource-providers.py --register",
            file=sys.stderr,
        )

    if args.register:
        return register(sorted(set(missing + registering)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
