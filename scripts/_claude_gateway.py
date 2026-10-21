"""Explicit upstream APIM credential configuration, never an identity fallback."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from _capacity_evidence import EvidenceError, az_command, object_value, run_bounded, strict_json


@dataclass(frozen=True)
class GatewayConfiguration:
    url: str
    api_key: str = field(repr=False)


def configured_gateway(environment: dict[str, str] | os._Environ[str] = os.environ) -> GatewayConfiguration | None:
    enabled = environment.get("AI4IA_CLAUDE_GATEWAY_ENABLED", "").lower()
    if enabled not in ("", "false", "true"):
        raise EvidenceError("claude_gateway_flag_not_boolean")
    if enabled != "true":
        return None
    if environment.get("AI4IA_CLAUDE_EXTERNAL_ENABLED", "").lower() == "true":
        raise EvidenceError("claude_auth_modes_conflict")
    url = environment.get("AI4IA_CLAUDE_GATEWAY_URL", "")
    key = environment.get("AI4IA_CLAUDE_GATEWAY_API_KEY", "")
    if not re.fullmatch(r"https://[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\.azure-api\.net/claude", url):
        raise EvidenceError("claude_gateway_url_invalid")
    if not re.fullmatch(r"[A-Za-z0-9]{16,128}", key):
        raise EvidenceError("claude_gateway_key_missing_or_invalid")
    return GatewayConfiguration(url=url, api_key=key)


class SourceRouteReader:
    def __init__(self, apim: str, subscription: str):
        self.apim, self.subscription = apim, subscription
        self.deadline, self.calls, self.bytes = time.monotonic() + 180, 0, 0

    def get(self, target: bool, path: str, version: str, *, graph: bool = False) -> dict[str, Any]:
        remaining = self.deadline - time.monotonic()
        self.calls += 1
        if (
            target or graph or version != "2024-05-01" or remaining <= 0 or self.calls > 40
            or self.bytes >= 8 * 1024 * 1024 or not path.startswith(self.apim + "/")
            or not re.fullmatch(
                r"(?:apis/openai/policies/policy|policyFragments/[A-Za-z0-9_-]+|namedValues|subscriptions/[A-Za-z0-9_-]+)",
                path[len(self.apim) + 1:],
            )
        ):
            raise EvidenceError("claude_gateway_route_read_scope_or_budget")
        query = "?api-version=" + version
        if "/policies/" in path or "/policyFragments/" in path:
            query += "&format=rawxml"
        result = run_bounded(
            [*az_command(), "rest", "--method", "GET", "--url", "https://management.azure.com" + path + query,
             "--headers", "Accept=application/json",
             "--subscription", self.subscription, "--only-show-errors", "--output", "json"],
            min(20, remaining), min(1024 * 1024, 8 * 1024 * 1024 - self.bytes),
        )
        self.bytes += len(result.body)
        if result.warning:
            raise EvidenceError("claude_gateway_route_read_incomplete")
        data = object_value(strict_json(result.body))
        if data.get("nextLink") or data.get("@odata.nextLink"):
            raise EvidenceError("claude_gateway_route_read_continuation")
        return data


def source_apim_id(environment: dict[str, str] | os._Environ[str] = os.environ) -> str:
    configured = environment.get("AZURE_APIM_RESOURCE_ID", "")
    if configured:
        return configured
    subscription = environment.get("AZURE_SUBSCRIPTION_ID", "")
    workload = environment.get("AI4IA_WORKLOAD") or "ai4ia"
    name = environment.get("AZURE_ENV_NAME", "")
    group = environment.get("AZURE_RESOURCE_GROUP") or f"rg-{workload}-{name}"
    if (
        not re.fullmatch(r"[0-9a-f-]{36}", subscription)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,18}[a-z0-9]", name)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,89}", group)
    ):
        raise EvidenceError("claude_gateway_source_scope_invalid")
    result = run_bounded(
        [*az_command(), "resource", "list", "--resource-group", group,
         "--resource-type", "Microsoft.ApiManagement/service", "--query", "[].id",
         "--subscription", subscription, "--only-show-errors", "--output", "json"],
        20, 65536,
    )
    rows = strict_json(result.body)
    if result.warning or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], str):
        raise EvidenceError("claude_gateway_source_apim_ambiguous")
    return rows[0]


def _source_scope(apim: str, environment: dict[str, str] | os._Environ[str]) -> tuple[str, str]:
    subscription = environment.get("AZURE_SUBSCRIPTION_ID", "")
    workload = environment.get("AI4IA_WORKLOAD") or "ai4ia"
    if not re.fullmatch(
        r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9_.-]+/providers/Microsoft.ApiManagement/service/[a-z0-9-]+", apim,
    ) or apim.split("/")[2] != subscription or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,89}", workload):
        raise EvidenceError("claude_gateway_source_scope_invalid")
    return subscription, workload


def verify_gateway_transition(apim: str, environment: dict[str, str] | os._Environ[str] = os.environ) -> None:
    from _claude_binding import ROOT, object_rows, policy_tree, same_policy

    gateway = configured_gateway(environment)
    subscription, _ = _source_scope(apim, environment)
    if gateway is None:
        raise EvidenceError("claude_gateway_not_configured")
    reader = SourceRouteReader(apim, subscription)
    rows = object_rows(reader.get(False, apim + "/namedValues", "2024-05-01").get("value"),
                       256, "claude_named_value_inventory")
    selected = [row for row in rows if row.get("name") in {"claude-target-endpoint", "claude-gateway-url"}]
    observed = {row["name"]: row.get("properties", {}).get("value") for row in selected}
    if len(observed) != len(selected):
        raise EvidenceError("claude_named_value_duplicates")
    if observed == {"claude-target-endpoint": gateway.url, "claude-gateway-url": gateway.url}:
        return
    policy = reader.get(False, apim + "/apis/openai/policies/policy", "2024-05-01")
    refs = [node.attrib.get("fragment-id", "") for node in
            policy_tree(policy.get("properties", {}).get("value")).iter("include-fragment")
            if node.attrib.get("fragment-id", "").startswith("claude_auth_v1-")]
    if len(refs) != 1 or not re.fullmatch(r"claude_auth_v1-[a-z0-9]{13}", refs[0]):
        raise EvidenceError("claude_current_auth_unknown")
    fragment = reader.get(False, apim + "/policyFragments/" + refs[0], "2024-05-01")
    disabled = (ROOT / "infra" / "policies" / "claude-disabled.xml").read_text(encoding="utf-8")
    if not same_policy(fragment.get("properties", {}).get("value"), disabled):
        raise EvidenceError("claude_disable_old_binding_before_change")
    if environment.get("AI4IA_CLAUDE_ENABLED", "").lower() == "true":
        raise EvidenceError("claude_initial_disabled_stage_required")


def verify_gateway_routes(apim: str, environment: dict[str, str] | os._Environ[str] = os.environ) -> None:
    from _claude_binding import verify_apim_routes

    gateway = configured_gateway(environment)
    subscription, workload = _source_scope(apim, environment)
    if gateway is None:
        raise EvidenceError("claude_gateway_not_configured")
    if urlsplit(gateway.url).hostname == apim.rsplit("/", 1)[1] + ".azure-api.net":
        raise EvidenceError("claude_gateway_loop")
    enabled = environment.get("AI4IA_CLAUDE_ENABLED", "").lower() == "true"
    verify_apim_routes(
        apim, {
            "claude-target-endpoint": gateway.url, "claude-gateway-url": gateway.url,
            "claude-proxy-subscription": workload + "-proxy-models",
        }, workload, SourceRouteReader(apim, subscription),
        auth_filename="claude-gateway-auth.xml" if enabled else "claude-disabled.xml",
        secret_names=("claude-gateway-key",),
    )
