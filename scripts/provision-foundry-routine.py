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
``model``/``toolbox``/``steps[].{name,instructions,tools}``). azure-ai-projects 2.4.0 has no
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
DEFAULT_TOOLBOX_MANIFEST = REPO_ROOT / "foundry" / "toolbox.manifest.json"
MANIFEST_SCHEMA = REPO_ROOT / "foundry" / "routines" / "routine.schema.json"
TOOLBOX_SCHEMA = REPO_ROOT / "foundry" / "toolbox.manifest.schema.json"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")  # matches the toolbox name pattern


# --------------------------------------------------------------------------------------
# Pure helpers (no Azure SDK import; unit-tested offline)
# --------------------------------------------------------------------------------------
def load_manifest(path: Path) -> Any:
    """Load + parse the routine manifest JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest_schema(manifest: Any) -> list[str]:
    """Validate the complete design contract before semantic cross-checks."""
    try:
        import jsonschema
    except ImportError:
        return [
            "JSON Schema validation requires the provisioning extra: "
            'python -m pip install -e "app/api[foundry]".'
        ]
    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
    return [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    ]


def validate_toolbox_schema(manifest: Any) -> list[str]:
    """Validate the toolbox used as the routine's semantic namespace."""
    try:
        import jsonschema
    except ImportError:
        return [
            "JSON Schema validation requires the provisioning extra: "
            'python -m pip install -e "app/api[foundry]".'
        ]
    schema = json.loads(TOOLBOX_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
    return [
        "toolbox "
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    ]


def toolbox_tool_names(toolbox_manifest: dict[str, Any]) -> set[str]:
    """Return the canonical instance names that routine steps may reference."""
    return {
        name
        for tool in toolbox_manifest.get("tools") or []
        if isinstance(tool, dict)
        for name in (tool.get("name") or tool.get("serverLabel"),)
        if isinstance(name, str) and name
    }


def validate_manifest(
    manifest: Any,
    toolbox_manifest: Any | None = None,
) -> list[str]:
    """Return human-readable validation errors (empty => valid as a design)."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return [f"manifest must be a JSON object, got {type(manifest).__name__}."]
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

    canonical = (
        load_manifest(DEFAULT_TOOLBOX_MANIFEST)
        if toolbox_manifest is None
        else toolbox_manifest
    )
    if not isinstance(canonical, dict):
        errors.append(
            f"toolbox manifest must be a JSON object, got {type(canonical).__name__}."
        )
        canonical = {}
    canonical_name = canonical.get("name")
    if toolbox and canonical_name and toolbox != canonical_name:
        errors.append(
            f"`toolbox` names '{toolbox}', but semantic validation uses canonical "
            f"toolbox '{canonical_name}'."
        )
    available_tools = toolbox_tool_names(canonical)

    raw_steps = manifest.get("steps")
    if not isinstance(raw_steps, list):
        errors.append("`steps` must be a JSON array.")
        steps: list[Any] = []
    else:
        steps = raw_steps
    if not steps:
        errors.append("routine has no steps: add at least one step before design validation.")
    seen: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{i}] must be a JSON object.")
            continue
        sname = step.get("name")
        if not sname:
            errors.append(f"steps[{i}].name is required.")
        elif sname in seen:
            errors.append(f"steps[{i}].name '{sname}' is duplicated (step names must be unique).")
        else:
            seen.add(sname)
        if not step.get("instructions"):
            errors.append(f"steps[{i}].instructions is required.")
        raw_tools = step.get("tools")
        if raw_tools is not None and not isinstance(raw_tools, list):
            errors.append(f"steps[{i}].tools must be a JSON array.")
            raw_tools = []
        for tool_name in raw_tools or []:
            if tool_name not in available_tools:
                errors.append(
                    f"steps[{i}].tools references unknown toolbox tool '{tool_name}'. "
                    f"Available canonical names: {', '.join(sorted(available_tools)) or '(none)'}."
                )
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
    """Project manifest steps to a normalized design shape (order preserved)."""
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
        "--toolbox-manifest",
        type=Path,
        default=DEFAULT_TOOLBOX_MANIFEST,
        help="Canonical toolbox manifest used for semantic tool-name validation.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the design and canonical toolbox references, then exit.",
    )
    parser.add_argument(
        "--project-endpoint",
        default=None,
        help="Foundry project endpoint (else AZURE_FOUNDRY_PROJECT_ENDPOINT), shown for context only "
        "-- this script never calls Azure (see module docstring: 'Why there is no --create').",
    )
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        raise SystemExit(f"Manifest not found: {args.manifest}")
    if not args.toolbox_manifest.exists():
        raise SystemExit(f"Toolbox manifest not found: {args.toolbox_manifest}")
    manifest = load_manifest(args.manifest)
    toolbox_manifest = load_manifest(args.toolbox_manifest)

    errors = validate_manifest_schema(manifest)
    errors.extend(validate_toolbox_schema(toolbox_manifest))
    if not errors:
        errors.extend(validate_manifest(manifest, toolbox_manifest))
    if errors:
        print("Routine design is not valid:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    if args.check:
        print("Routine design is valid; every referenced tool exists in the canonical toolbox.")
        return 0

    # Informational only: unlike the toolbox script, this script has no live call, so a
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
    print("\nIf this design later gains an executable contract, its named tools resolve to the")
    print("canonical APIM-fronted toolbox. Nothing in this file is served today.")
    print("\n(validation-only -- there is no --create; azure-ai-projects 2.4.0's actual routines")
    print("surface cannot faithfully represent this manifest today. See module docstring / ")
    print("docs/foundry-toolbox.md's 'Routines' section for why.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
