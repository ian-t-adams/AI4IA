"""Allowlisted public projections of private Azure observations, including legacy snapshots."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from scripts.http_contract import HttpError, stamp, strict_json, timestamp

TYPES = {
    "microsoft.apimanagement/service": ("API Management", "Gateway"),
    "microsoft.app/containerapps": ("Container App", "Compute"),
    "microsoft.app/managedenvironments": ("Container Apps Environment", "Compute"),
    "microsoft.app/managedenvironments/managedcertificates": ("Managed TLS Certificate", "Compute"),
    "microsoft.appconfiguration/configurationstores": ("App Configuration", "Config"),
    "microsoft.cognitiveservices/accounts": ("Azure AI Foundry account", "AI"),
    "microsoft.cognitiveservices/accounts/projects": ("Foundry project", "AI"),
    "microsoft.containerregistry/registries": ("Container Registry", "Compute"),
    "microsoft.documentdb/databaseaccounts": ("Cosmos DB (NoSQL)", "Data"),
    "microsoft.durabletask/schedulers": ("Durable Task Scheduler", "Compute"),
    "microsoft.search/searchservices": ("Azure AI Search", "Data"),
    "microsoft.eventgrid/systemtopics": ("Storage security events", "Security"),
    "microsoft.eventhub/namespaces": ("Event Hubs", "Messaging"),
    "microsoft.servicebus/namespaces": ("Service Bus", "Messaging"),
    "microsoft.insights/components": ("Application Insights", "Observability"),
    "microsoft.insights/actiongroups": ("Monitor Action Group", "Observability"),
    "microsoft.keyvault/vaults": ("Key Vault", "Security"),
    "microsoft.managedidentity/userassignedidentities": ("Managed Identity", "Security"),
    "microsoft.monitor/accounts": ("Monitor Workspace", "Observability"),
    "microsoft.operationalinsights/workspaces": ("Log Analytics Workspace", "Observability"),
    "microsoft.storage/storageaccounts": ("Storage Account", "Data"),
    "microsoft.apicenter/services": ("API Center", "Gateway"),
}
AVAILABILITY = {"Available", "Unavailable", "Degraded", "Unknown"}
PROVISIONING = {"Succeeded", "Failed", "Canceled", "Creating", "Updating", "Deleting", "n/a"}
OUTCOMES = {
    "healthy", "reachable", "http_error", "network_unavailable", "target_unresolved",
    "persistence_unavailable", "invalid_response", "redirect_rejected", "deadline",
    "response_too_large", "invalid_target", "private_ingress", "target_ambiguous",
    "auth_required", "unexpected_redirect",
}
PUBLIC_UUID_CONSTANTS = {
    "00000000-0000-0000-0000-000000000000",
    "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # RFC UUID DNS namespace
    "6ba7b811-9dad-11d1-80b4-00c04fd430c8",  # RFC UUID URL namespace
    "28e9f9db-3c8d-5aa5-9faf-44286ae5937c",  # Mermaid's public diagram namespace
}


def object_value(value: Any) -> dict:
    if not isinstance(value, dict):
        raise HttpError("invalid_response")
    return value


def count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 30000:
        raise HttpError("invalid_response")
    return value


def date(value: Any) -> str:
    return stamp(timestamp(value))


def inventory(value: Any) -> dict | None:
    if value is None:
        return None
    data = object_value(value)
    if not isinstance(data.get("resources"), list) or len(data["resources"]) > 500:
        raise HttpError("invalid_response")
    rows = []
    for raw in data["resources"]:
        row = object_value(raw)
        resource_type = row.get("type")
        key = resource_type.lower() if isinstance(resource_type, str) else "other"
        if key not in TYPES:
            key = "other"
        label, group = TYPES.get(key, ("Azure service", "Other"))
        availability = row.get("availability")
        if availability not in AVAILABILITY:
            availability = "Unknown"
        provisioning = row.get("provisioningState")
        if provisioning not in PROVISIONING:
            provisioning = "n/a"
        state = (
            "unavailable" if availability == "Unavailable"
            else "degraded" if availability == "Degraded" or provisioning in {"Failed", "Canceled"}
            else "healthy" if availability == "Available" else "provisioned"
        )
        rows.append({
            "type": key, "label": label, "group": group,
            "provisioningState": provisioning, "availability": availability,
            "healthReported": row.get("healthReported") is True, "state": state,
        })
    return {"generatedAt": date(data.get("generatedAt")), "resources": rows}


def status(value: Any) -> dict | None:
    if value is None:
        return None
    data = object_value(value)
    source = object_value(data.get("healthSource"))
    source_status = source.get("status")
    if source_status not in {"available", "unavailable"}:
        raise HttpError("invalid_response")
    summary = object_value(data.get("summary"))
    numbers = {key: count(summary.get(key)) for key in (
        "total", "healthy", "provisioned", "degraded", "unavailable", "endpointsUp", "endpointsTot",
    )}
    raw_endpoints = data.get("endpoints")
    if not isinstance(raw_endpoints, list) or len(raw_endpoints) != 4:
        raise HttpError("invalid_response")
    endpoints = []
    for raw, (name, kind) in zip(raw_endpoints, (
        ("Web app", "ingress"), ("Model gateway", "ingress"),
        ("API liveness", "liveness"), ("API readiness", "readiness"),
    ), strict=True):
        row = object_value(raw)
        state = row.get("state")
        if state not in {"up", "down", "unknown"}:
            raise HttpError("invalid_response")
        outcome = row.get("outcome")
        if outcome not in OUTCOMES:
            outcome = "invalid_response"
            state = "unknown"
        http_status = count(row.get("httpStatus", 0))
        if http_status > 599:
            raise HttpError("invalid_response")
        observed = row.get("observedAt")
        observed = date(observed) if observed is not None else None
        observed_ok = row.get("ok")
        if kind == "ingress":
            reachable = outcome == "reachable" and (
                200 <= http_status < 400 or http_status in (401, 403)
            )
            failed = outcome == "http_error" and 100 <= http_status <= 599 and not (
                200 <= http_status < 400 or http_status in (401, 403)
            )
        else:
            reachable = outcome == "healthy" and http_status == 200
            failed = (
                kind == "readiness" and outcome == "persistence_unavailable"
                and http_status == 503
            ) or (
                outcome == "http_error" and 100 <= http_status <= 599
                and http_status != 200 and not 300 <= http_status < 400
                and http_status not in (401, 403)
                and (kind != "readiness" or http_status != 503)
            )
        conclusion = (
            state == "up" and observed_ok is True and reachable
        ) or (
            state == "down" and observed_ok is False and failed
        )
        if not (conclusion and observed is not None):
            if state != "unknown" or outcome in {
                "reachable", "healthy", "http_error", "persistence_unavailable",
            }:
                outcome = "invalid_response"
            state = "unknown"
        latency = row.get("latencyMs")
        if latency is not None:
            latency = count(latency)
        endpoints.append({
            "name": name, "kind": kind, "httpStatus": http_status,
            "ok": state == "up", "state": state, "outcome": outcome,
            "observedAt": observed,
            "latencyMs": latency,
        })
    numbers["endpointsTot"] = len(endpoints)
    numbers["endpointsUp"] = sum(row["state"] == "up" for row in endpoints)
    return {
        "generatedAt": date(data.get("generatedAt")),
        "healthSource": {
            "status": source_status,
            "note": (
                "Resource Health query succeeded; missing per-service signals remain unknown."
                if source_status == "available"
                else "Resource Health source unavailable; availability was not checked."
            ),
        },
        "summary": numbers, "endpoints": endpoints,
    }


def write_snapshots(directory: Path, inv: Any, health: Any) -> None:
    projected = (inventory(inv), status(health))
    directory.mkdir(parents=True, exist_ok=True)
    for name, variable, value in zip(
        ("inventory", "status"), ("AI4IA_INVENTORY", "AI4IA_STATUS"), projected, strict=True,
    ):
        (directory / f"{name}.js").write_text(
            f"window.{variable} = {json.dumps(value, ensure_ascii=True, allow_nan=False)};\n",
            encoding="utf-8",
        )


def assignment(raw: bytes, variable: str) -> Any:
    match = re.fullmatch(
        rf"\s*(?://[^\n]*\n)?window\.{variable}\s*=\s*(.+?)\s*;\s*",
        raw.decode("utf-8-sig"), re.DOTALL,
    )
    if match is None:
        raise HttpError("invalid_response")
    return strict_json(match[1].encode("utf-8"), limit=256 * 1024)


def check_site(directory: Path) -> None:
    from scripts.uptime import parse_report

    for name, variable, project in (
        ("inventory", "AI4IA_INVENTORY", inventory), ("status", "AI4IA_STATUS", status),
    ):
        data = assignment((directory / "data" / f"{name}.js").read_bytes(), variable)
        if project(data) != data:
            raise HttpError("invalid_response")
    raw = (directory / "data" / "uptime.json").read_bytes()
    value = strict_json(raw, limit=16 * 1024)
    if value != {
        "version": 2, "checkedAt": None, "history": "unavailable",
        "overall": "unknown", "endpoints": [],
    } and parse_report(raw) != value:
        raise HttpError("invalid_response")
    forbidden = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        r"|/subscriptions/|/resourcegroups/"
        r"|https?://[a-z0-9.-]+\.(?:azurecontainerapps\.io|azure-api\.net|"
        r"cognitiveservices\.azure\.com|services\.ai\.azure\.com|openai\.azure\.com|"
        r"documents\.azure\.com|blob\.core\.windows\.net|search\.windows\.net)"
        r"|\b(?:eastus2?|westus[23]?|swedencentral)\b|\b(?:East US(?: 2)?|West US|Sweden Central)\b",
        re.IGNORECASE,
    )
    for path in directory.rglob("*"):
        if path.suffix.lower() not in {".js", ".json", ".html", ".md", ".txt", ".xml"}:
            continue
        text = path.read_text(encoding="utf-8-sig")
        for constant in PUBLIC_UUID_CONSTANTS:
            text = text.replace(constant, "")
        if forbidden.search(text):
            # Do not echo the offending content or path into public workflow logs.
            raise HttpError("invalid_response")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--directory", type=Path)
    target.add_argument("--check-site", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.check_site:
            check_site(arguments.check_site)
        else:
            data = object_value(json.load(sys.stdin))
            write_snapshots(arguments.directory, data.get("inventory"), data.get("status"))
    except (HttpError, ValueError, TypeError, OSError):
        raise SystemExit("Public snapshot projection refused invalid observations.") from None


if __name__ == "__main__":
    main()
