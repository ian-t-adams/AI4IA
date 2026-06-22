#!/usr/bin/env python3
"""Fail fast on contradictory deployment parameters.

This validates relationships that Bicep cannot express cleanly in
main.parameters.json. It intentionally does not require feature endpoints that
main.bicep derives from provisioned resources.
"""

from __future__ import annotations

import json
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
