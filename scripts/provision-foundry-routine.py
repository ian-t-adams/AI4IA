#!/usr/bin/env python3
"""Validate a Foundry routine manifest and print its plan (foundry/routines/*.routine.json).

Operator-run, validation-only companion to the routines plan in docs/foundry-toolbox.md (P7).
It does NOT run during `azd up`, in CI (beyond validating the shipped example), or in the app
runtime, and it never imports or calls the Azure SDK -- see "Why there is no --create" below.

The bridge, in one line: a routine's tool calls target the shared toolbox, which is already
fronted by the official-MCP APIM -- so routines inherit that governance and add NO new APIM
surface for our runtime.

What it does
------------
1. Loads + validates a routine manifest (default: foundry/routines/example.routine.json).
2. Prints the *plan*: the steps, the model, and which toolbox tools each step calls (a reminder
   that every tool call flows through the APIM-fronted toolbox MCP endpoint), plus the project
   endpoint if one is configured (informational only -- this script never calls it).

Why there is no --create
-------------------------
This manifest models a multi-step, model-driven, tool-calling workflow (``name``/``description``/
``model``/``toolbox``/``steps[].{name,instructions,tools}``). azure-ai-projects 2.3.0 has no
``project.routines`` at all -- its actual (public preview) routines surface is
``project.beta.routines.create_or_update(routine_name, *, triggers, action)``, which models
something fundamentally different: an event **trigger** (a custom event, a GitHub issue, a cron
schedule, or a timer) that invokes ONE **already-existing** Foundry agent by name with a static
input payload -- not "run these N steps, each calling tools." There is no trigger-type field and
no target-agent-name field anywhere in this manifest schema, so there is no faithful,
non-inventive translation from one shape to the other. Rather than fake-map semantics (e.g.
silently treating a step as a trigger and guessing an agent name), this script is
validation/planning-only until AI4IA defines a manifest schema that actually captures a
trigger + target-agent shape, or the SDK's routines surface gains step-based workflow support.
See docs/foundry-toolbox.md's "Routines" section for the full residual-gap writeup.

All routine features referenced here are Azure public preview; do not use in production without
validation.
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
# CLI
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Path to a *.routine.json manifest.")
    parser.add_argument(
        "--project-endpoint",
        default=None,
        help="Foundry project endpoint (else AZURE_FOUNDRY_PROJECT_ENDPOINT), shown for context only "
        "-- this script never calls Azure (see module docstring: 'Why there is no --create').",
    )
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

    # Informational only: unlike the toolbox/skills scripts, this script has no live call, so a
    # missing endpoint is not fatal here (see module docstring: 'Why there is no --create').
    try:
        endpoint = resolve_project_endpoint(args.project_endpoint)
    except SystemExit:
        endpoint = "(not configured -- informational only; this script never calls Azure)"
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
    print("\n(validation-only -- there is no --create; azure-ai-projects 2.3.0's actual routines")
    print("surface cannot faithfully represent this manifest today. See module docstring / ")
    print("docs/foundry-toolbox.md's 'Routines' section for why.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
