"""Validate catalog-owned Model Router subsets and provider readbacks."""
from __future__ import annotations

import json
from typing import Any


def routing_configuration(model: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any] | None:
    if "routing" not in model:
        return None
    routing = model["routing"]
    if (
        model.get("name") != "model-router" or model.get("format") != "OpenAI"
        or model.get("category") != "router" or model.get("deploymentTarget", "source") != "source"
        or not isinstance(routing, dict) or set(routing) != {"mode", "models"}
        or routing.get("mode") not in ("balanced", "cost", "quality")
    ):
        raise ValueError("routing requires an explicit source model-router mode and subset")
    rows = routing["models"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("routing.models must contain 1 to 16 exact model identities")
    seen: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict) or set(row) != {"format", "name", "version"}
            or any(not isinstance(value, str) or not value or value != value.strip() for value in row.values())
            or row["format"] != "OpenAI" or row["name"] in seen
        ):
            raise ValueError("routing.models contains an invalid or duplicate model identity")
        seen.add(row["name"])
        matches = [entry for entry in catalog["catalog"] if entry["name"] == row["name"]]
        if len(matches) != 1:
            raise ValueError("routing.models must reference an existing catalog model")
        target = matches[0]
        if (
            target.get("runtimeEnabled", True) is not True or target.get("format") != row["format"]
            or target.get("deploymentTarget", "source") != "source"
            or target.get("category") not in {"chat", "chat-fast", "reasoning"}
        ):
            raise ValueError("routing.models must reference enabled source text models")
        if any(deployment["version"] != "2025-11-18" for deployment in model["deployments"]) or not any(
            option["version"] == row["version"] for option in target["deployments"]
        ):
            raise ValueError("routing.models version does not match the catalog")
    return {"mode": routing["mode"], "models": [dict(row) for row in rows]}


def routing_matches(actual: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(actual, dict) or set(actual) != {"mode", "models"}:
        return False
    if actual["mode"] != expected["mode"] or not isinstance(actual["models"], list):
        return False
    rows = actual["models"]
    if any(
        not isinstance(row, dict) or set(row) != {"format", "name", "version"}
        or any(not isinstance(value, str) for value in row.values())
        for row in rows
    ):
        return False
    return sorted((row["format"], row["name"], row["version"]) for row in rows) == sorted(
        (row["format"], row["name"], row["version"]) for row in expected["models"]
    )


def offered_routing_models(offering: dict[str, Any], expected: dict[str, Any]) -> bool:
    capabilities = offering.get("capabilities")
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("models"), str):
        return False
    try:
        rows = json.loads(capabilities["models"])
    except (ValueError, TypeError):
        return False
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        return False
    identities: set[tuple[str, str, str]] = set()
    for row in rows:
        if (
            not isinstance(row, dict) or set(row) != {"format", "name", "version"}
            or any(not isinstance(value, str) or not value for value in row.values())
        ):
            return False
        identity = (row["format"], row["name"], row["version"])
        if identity in identities:
            return False
        identities.add(identity)
    return all((row["format"], row["name"], row["version"]) in identities for row in expected["models"])
