#!/usr/bin/env python3
"""Fail fast on contradictory deployment parameters.

This validates relationships that Bicep cannot express cleanly in
main.parameters.json. It intentionally does not require feature endpoints that
main.bicep derives from provisioned resources.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PARAMETERS_FILE = ROOT / "infra" / "main.parameters.json"
PLACEHOLDER_RE = re.compile(r"^\$\{(?P<name>[A-Z0-9_]+)(?:=(?P<default>.*))?\}$")


def parameter_value(parameters: dict[str, Any], name: str, default: Any = None) -> Any:
    return parameters.get(name, {}).get("value", default)


def resolve_placeholder(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    match = PLACEHOLDER_RE.match(value)
    if match is None:
        return value
    env_value = os.environ.get(match.group("name"))
    if env_value is not None and env_value.strip():
        return env_value
    return match.group("default") or ""


def truthy(value: Any) -> bool:
    value = resolve_placeholder(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def text(value: Any) -> str:
    value = resolve_placeholder(value)
    if value is None:
        return ""
    return str(value).strip()


def main() -> int:
    raw = json.loads(PARAMETERS_FILE.read_text(encoding="utf-8"))
    parameters = raw.get("parameters", {})
    errors: list[str] = []
    warnings: list[str] = []

    app_environment = text(parameter_value(parameters, "appEnvironment", "dev")).lower()
    auth_provider = text(parameter_value(parameters, "apiAuthProvider", "dev")).lower()

    if app_environment == "prod" and auth_provider != "entra":
        errors.append("appEnvironment=prod requires apiAuthProvider=entra.")

    if auth_provider == "entra":
        for name in ("entraTenantId", "entraAudience", "entraWebClientId"):
            if not text(parameter_value(parameters, name)):
                errors.append(f"apiAuthProvider=entra requires {name}.")

    if truthy(parameter_value(parameters, "voiceLiveToolsEnabled", False)) and not truthy(
        parameter_value(parameters, "voiceLiveEnabled", False)
    ):
        errors.append("voiceLiveToolsEnabled=true is inert unless voiceLiveEnabled=true.")

    if truthy(parameter_value(parameters, "voiceLiveEnabled", False)) and app_environment != "dev":
        if not text(parameter_value(parameters, "realtimeAllowedOrigins")):
            errors.append("voiceLiveEnabled=true outside dev requires realtimeAllowedOrigins.")

    if truthy(parameter_value(parameters, "documentComputeEnabled", False)) and not truthy(
        parameter_value(parameters, "documentUnderstandingEnabled", False)
    ):
        errors.append("documentComputeEnabled=true requires documentUnderstandingEnabled=true.")

    for domain_name, cert_name in (
        ("webCustomDomain", "webManagedCertName"),
        ("proxyCustomDomain", "proxyManagedCertName"),
    ):
        domain = text(parameter_value(parameters, domain_name))
        cert = text(parameter_value(parameters, cert_name))
        if cert and not domain:
            errors.append(f"{cert_name} is set but {domain_name} is empty.")
        if domain and not cert:
            warnings.append(f"{domain_name} is set without {cert_name}; Bicep will derive a cert name.")

    profiles_enabled = truthy(parameter_value(parameters, "proxyProfilesEnabled", False))
    profile_projection = text(parameter_value(parameters, "proxyProfileProjectionJson"))
    if profiles_enabled:
        if not profile_projection:
            errors.append(
                "proxyProfilesEnabled=true requires proxyProfileProjectionJson."
            )
        else:
            try:
                profiles = json.loads(profile_projection)
            except json.JSONDecodeError as exc:
                errors.append(f"proxyProfileProjectionJson is invalid JSON: {exc.msg}.")
            else:
                if not isinstance(profiles, list) or not profiles:
                    errors.append(
                        "proxyProfileProjectionJson must be a non-empty JSON array."
                    )
                elif any(
                    not isinstance(profile, dict)
                    or not isinstance(profile.get("appId"), str)
                    or not profile["appId"].strip()
                    for profile in profiles
                ):
                    errors.append(
                        "Every proxy profile projection entry requires a non-empty appId."
                    )
        errors.append(
            "proxyProfilesEnabled=true requires a verified identity-aware application "
            "header at the proxy edge. The temporary shared-key ingress does not meet "
            "that prerequisite; keep profiles disabled until Entra workload auth is wired."
        )

    priorities_enabled = truthy(
        parameter_value(parameters, "proxyPrioritiesEnabled", False)
    )
    priority_workers = text(parameter_value(parameters, "proxyPriorityWorkers"))
    if priorities_enabled and not re.fullmatch(r"\d+:\d+(?:\s*,\s*\d+:\d+)*", priority_workers):
        errors.append(
            "proxyPrioritiesEnabled=true requires proxyPriorityWorkers in "
            "priority:count format (for example 1:2,3:1)."
        )

    min_replicas = int(text(parameter_value(parameters, "proxyMinReplicas", 1)) or "1")
    max_replicas = int(text(parameter_value(parameters, "proxyMaxReplicas", 3)) or "3")
    if min_replicas < 1:
        errors.append("proxyMinReplicas must be at least 1 for the active gateway path.")
    if min_replicas > max_replicas:
        errors.append("proxyMinReplicas must not exceed proxyMaxReplicas.")

    if truthy(parameter_value(parameters, "dataTierPrivate", False)) and not truthy(
        parameter_value(parameters, "vnetIsolationEnabled", False)
    ):
        errors.append("dataTierPrivate=true requires vnetIsolationEnabled=true.")

    owner = text(parameter_value(parameters, "owner"))
    publisher = text(parameter_value(parameters, "apimPublisherEmail"))
    if owner in {"", "ian-t-adams"}:
        errors.append("owner must be set to the current accountable operator, not a personal default.")
    if publisher in {"", "ianadams@microsoft.com"}:
        errors.append("apimPublisherEmail must be deployment-owned, not a personal mailbox.")
    elif publisher.endswith("@example.com"):
        warnings.append("apimPublisherEmail is still an example address; set it for live deploys.")

    for warning in warnings:
        print(f"WARNING: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Deployment parameter prerequisites look sane.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
