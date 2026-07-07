#!/usr/bin/env python3
"""Provision a Foundry Agent Service routine from a foundry/routines/*.routine.json manifest.

Operator-run, provisioning-time companion to the routines plan in docs/foundry-toolbox.md (P7).
It does NOT run during `azd up`, in CI, or in the app runtime.

The bridge, in one line: a routine's tool calls target the shared toolbox, which is already
fronted by the official-MCP APIM -- so routines inherit that governance and add NO new APIM
surface for our runtime.

What it does
------------
1. Loads + validates a routine manifest (default: foundry/routines/example.routine.json).
2. Resolves the primary project endpoint (``--project-endpoint`` or the
   ``AZURE_FOUNDRY_PROJECT_ENDPOINT`` azd output).
3. Prints the *plan*: the steps, the model, and which toolbox tools each step calls (a reminder
   that every tool call flows through the APIM-fronted toolbox MCP endpoint).
4. With ``--create``, creates the routine via the ``azure-ai-projects`` SDK (install the
   optional ``foundry`` dependency group). Without it, the script is a safe offline dry run.

All routine features are Azure public preview; do not use in production without validation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "foundry" / "routines" / "example.routine.json"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")  # matches the toolbox name pattern


# --------------------------------------------------------------------------------------
# Pure helpers (no Azure SDK import; unit-tested offline)
# --------------------------------------------------------------------------------------
def load_manifest(path: Path) -> dict[str, Any]:
    """Load + parse the routine manifest JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors (empty => valid to provision)."""
    errors: list[str] = []
    name = manifest.get("name")
    if not isinstance(name, str) or not _SLUG_RE.match(name):
        errors.append("`name` must be a lowercase slug matching ^[a-z][a-z0-9-]{0,38}[a-z0-9]$ (2-40 chars, starts with a letter).")
    if not manifest.get("description"):
        errors.append("`description` is required and must be non-empty.")
    if not manifest.get("model"):
        errors.append("`model` is required (the model deployment the routine runs on).")

    toolbox = manifest.get("toolbox")
    if toolbox is not None and (not isinstance(toolbox, str) or not _SLUG_RE.match(toolbox)):
        errors.append("`toolbox` must be a lowercase slug matching the toolbox name pattern.")

    steps = manifest.get("steps") or []
    if not steps:
        errors.append("routine has no steps: add at least one step before provisioning.")
    seen: set[str] = set()
    for i, step in enumerate(steps):
        sname = step.get("name")
        if not sname:
            errors.append(f"steps[{i}].name is required.")
        elif sname in seen:
            errors.append(f"steps[{i}].name '{sname}' is duplicated (step names must be unique).")
        else:
            seen.add(sname)
        if not step.get("instructions"):
            errors.append(f"steps[{i}].instructions is required.")
    return errors


def referenced_tools(manifest: dict[str, Any]) -> list[str]:
    """Every distinct toolbox tool name the routine's steps call (order preserved)."""
    tools: list[str] = []
    for step in manifest.get("steps") or []:
        for tool in step.get("tools") or []:
            if tool not in tools:
                tools.append(tool)
    return tools


def plan_steps(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Project manifest steps to the create payload shape (order preserved)."""
    planned: list[dict[str, Any]] = []
    for step in manifest.get("steps") or []:
        planned.append(
            {
                "name": step["name"],
                "instructions": step["instructions"],
                "tools": list(step.get("tools") or []),
            }
        )
    return planned


def resolve_project_endpoint(arg: str | None) -> str:
    import os

    endpoint = arg or os.environ.get("AZURE_FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise SystemExit(
            "No project endpoint. Pass --project-endpoint or set AZURE_FOUNDRY_PROJECT_ENDPOINT "
            "(emitted by `azd env get-values` when enableFoundryToolbox=true)."
        )
    return endpoint


# --------------------------------------------------------------------------------------
# Live path (isolated Azure SDK import; requires the optional `foundry` dependency group)
# --------------------------------------------------------------------------------------
def create_routine(manifest: dict[str, Any], project_endpoint: str) -> Any:  # pragma: no cover - live only
    """Create the routine via azure-ai-projects. Imported lazily so dry runs need no SDK."""
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise SystemExit(
            "azure-ai-projects is not installed. Install the optional provisioning group:\n"
            '  uv pip install -e "app/api[foundry]"   # or: pip install azure-ai-projects azure-identity'
        ) from exc

    project = AIProjectClient(endpoint=project_endpoint, credential=DefaultAzureCredential())
    kwargs: dict[str, Any] = {
        "name": manifest["name"],
        "description": manifest.get("description", ""),
        "model": manifest["model"],
        "steps": plan_steps(manifest),
    }
    if manifest.get("toolbox"):
        kwargs["toolbox"] = manifest["toolbox"]
    # `routines` is a public-preview surface not yet in the azure-ai-projects typed stubs; access it
    # defensively so a pinned SDK that lacks it fails with a clear message, not an AttributeError, and
    # we never ship a hard call to an unverifiable attribute.
    routines = getattr(project, "routines", None)
    if routines is None:
        raise SystemExit(
            "This azure-ai-projects build does not expose `project.routines` (routines are public "
            "preview). Pin a preview SDK that includes the routines surface, or create the routine via "
            "the az/azd CLI (see docs/foundry-toolbox.md)."
        )
    return routines.create_routine(**kwargs)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Path to a *.routine.json manifest.")
    parser.add_argument("--project-endpoint", default=None, help="Foundry project endpoint (else AZURE_FOUNDRY_PROJECT_ENDPOINT).")
    parser.add_argument("--create", action="store_true", help="Actually create the routine (needs azure-ai-projects). Default is a dry run.")
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        raise SystemExit(f"Manifest not found: {args.manifest}")
    manifest = load_manifest(args.manifest)

    errors = validate_manifest(manifest)
    if errors:
        print("Routine manifest is not ready to provision:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    endpoint = resolve_project_endpoint(args.project_endpoint)
    steps = plan_steps(manifest)
    tools = referenced_tools(manifest)
    toolbox = manifest.get("toolbox") or "(default toolbox)"

    print(f"Routine           : {manifest['name']}")
    print(f"Project endpoint  : {endpoint}")
    print(f"Model             : {manifest['model']}")
    print(f"Toolbox (bridge)  : {toolbox}")
    print(f"Steps ({len(steps)})         : {', '.join(s['name'] for s in steps)}")
    print(f"Tool calls        : {', '.join(tools) or '(none)'}")
    print("\nEvery tool call above targets the toolbox MCP endpoint, which is fronted by the")
    print("official-MCP APIM -- so this routine inherits the bridge's governance for free.")

    if not args.create:
        print("\n(dry run) Re-run with --create to create the routine in Foundry.")
        return 0

    result = create_routine(manifest, endpoint)
    rid = getattr(result, "id", None) or getattr(result, "name", manifest["name"])
    print(f"\nCreated routine '{manifest['name']}' ({rid}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
