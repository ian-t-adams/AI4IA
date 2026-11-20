"""Strict loader for exact, externally managed model deployment declarations."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("external-model-deployments.json")
FIELDS = frozenset({
    "accountId", "deploymentName", "modelFormat", "modelName", "modelVersion", "sku",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]\Z", re.ASCII)
_VERSION = re.compile(r"(?:1|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])\Z", re.ASCII)
_RESOURCE_GROUP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._()-]*[A-Za-z0-9]\Z", re.ASCII)
_SUBSCRIPTION = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE | re.ASCII,
)


def _validate_account_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("External deployment accountId must be an exact ARM resource ID.")
    parts = value.split("/")
    if (
        len(parts) != 9 or parts[0] != "" or parts[1] != "subscriptions"
        or not _SUBSCRIPTION.fullmatch(parts[2])
        or parts[3] != "resourceGroups"
        or not _RESOURCE_GROUP.fullmatch(parts[4])
        or parts[5] != "providers" or parts[6] != "Microsoft.CognitiveServices"
        or parts[7] != "accounts" or not _IDENTIFIER.fullmatch(parts[8])
    ):
        raise ValueError("External deployment accountId must be an exact ARM resource ID.")
    return value


def _validate_identifier(value: Any, field: str) -> str:
    pattern = _VERSION if field == "modelVersion" else _IDENTIFIER
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"External deployment {field} must be a literal exact identifier.")
    return value


def load_external_model_deployments(path: Path = MANIFEST_PATH) -> list[dict[str, str]]:
    """Load exact deployment identities; wildcard, prefix, and extra fields refuse."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("External model deployment manifest is unavailable or invalid JSON.") from exc

    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schemaVersion", "deployments"}
        or type(manifest["schemaVersion"]) is not int
        or manifest["schemaVersion"] != 1
        or not isinstance(manifest["deployments"], list)
    ):
        raise ValueError("External model deployment manifest has an unsupported shape.")

    declarations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in manifest["deployments"]:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise ValueError("External model deployment entry has an unsupported shape.")
        declaration = {
            "accountId": _validate_account_id(row["accountId"]),
            "deploymentName": _validate_identifier(row["deploymentName"], "deploymentName"),
            "modelFormat": _validate_identifier(row["modelFormat"], "modelFormat"),
            "modelName": _validate_identifier(row["modelName"], "modelName"),
            "modelVersion": _validate_identifier(row["modelVersion"], "modelVersion"),
            "sku": _validate_identifier(row["sku"], "sku"),
        }
        identity = (declaration["accountId"].casefold(), declaration["deploymentName"].casefold())
        if identity in seen:
            raise ValueError("External model deployment entries contain a duplicate identity.")
        seen.add(identity)
        declarations.append(declaration)
    return declarations
