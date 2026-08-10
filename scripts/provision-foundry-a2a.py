#!/usr/bin/env python3
"""Validate an AI4IA A2A integration design (docs/foundry-toolbox.md).

This is a design/preview artifact only. It does not run during ``azd up``,
emit provisioning commands, create an APIM surface, register an AgentSpec, or
provide an app runtime client.

The checked-in manifest records every missing executable contract:
protocol/version negotiation, endpoint discovery, backend authentication, APIM
operation/product/subscription/policy, and runtime client wiring. Until those
are modeled and tested, printing plausible Azure commands would falsely imply
that the integration is callable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "foundry" / "a2a" / "example.a2a.json"
MANIFEST_SCHEMA = REPO_ROOT / "foundry" / "a2a" / "a2a.schema.json"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")
REQUIRED_BLOCKERS = {
    "protocol-and-version",
    "endpoint-discovery",
    "backend-authentication",
    "apim-operation",
    "apim-product-and-subscription",
    "apim-policy",
    "runtime-client-wiring",
}


def load_manifest(path: Path) -> Any:
    """Load and parse an A2A design manifest."""
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest_schema(manifest: Any) -> list[str]:
    """Validate the complete design contract before semantic checks."""
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


def validate_manifest(manifest: Any) -> list[str]:
    """Return human-readable validation errors (empty means valid design)."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return [f"manifest must be a JSON object, got {type(manifest).__name__}."]
    if not manifest.get("agentName"):
        errors.append("`agentName` is required (the proposed Foundry agent).")
    if not manifest.get("displayName"):
        errors.append("`displayName` is required.")
    if not manifest.get("description"):
        errors.append("`description` is required.")
    link_as = manifest.get("linkAs")
    if not isinstance(link_as, str) or not _SLUG_RE.match(link_as):
        errors.append(
            "`linkAs` must be a lowercase slug matching "
            "^[a-z][a-z0-9-]{0,38}[a-z0-9]$."
        )

    blockers = manifest.get("blockingRequirements")
    blocker_values = (
        {value for value in blockers if isinstance(value, str)}
        if isinstance(blockers, list)
        else set()
    )
    if (
        not isinstance(blockers, list)
        or any(not isinstance(value, str) for value in blockers)
        or blocker_values != REQUIRED_BLOCKERS
    ):
        missing = sorted(REQUIRED_BLOCKERS - blocker_values)
        extra = sorted(blocker_values - REQUIRED_BLOCKERS)
        errors.append(
            "`blockingRequirements` must name every unresolved executable "
            f"contract (missing={missing}, extra={extra})."
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to a *.a2a.json design manifest.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the design and exit.",
    )
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        raise SystemExit(f"Manifest not found: {args.manifest}")
    manifest = load_manifest(args.manifest)

    errors = validate_manifest_schema(manifest)
    if not errors:
        errors.extend(validate_manifest(manifest))
    if errors:
        print("A2A design is not valid:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if args.check:
        print("A2A design is valid and remains explicitly non-callable.")
        return 0

    print(f"A2A design        : {manifest['agentName']}")
    print(f"Proposed link name: {manifest['linkAs']}")
    print("Lifecycle         : design-preview (not callable)")
    print("\nBlocking executable contracts:")
    for blocker in manifest["blockingRequirements"]:
        print(f"  - {blocker}")
    print("\nNo Azure commands or runtime registration are emitted. Define and test")
    print("every contract above before changing this artifact to an executable lifecycle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
