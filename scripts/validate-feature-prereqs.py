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

    # main.bicep derives the realtime Origin allowlist from the web app this
    # deployment actually creates (Container Apps default FQDN + webCustomDomain
    # when bound), so it is never empty and needs no per-environment value. What
    # does need guarding is the reverse mistake, which is what this file used to
    # mandate: pinning a literal hostname here. A stale hardcoded origin is still
    # "non-empty", so it satisfies both this validator and the API's startup
    # check while naming whatever tenant it was written for -- the stack then
    # comes up green and rejects every browser on the Voice Live handshake.
    # Additional origins belong in the AI4IA_REALTIME_ALLOWED_ORIGINS variable,
    # which Bicep unions into the derived set.
    raw_realtime_origins = parameter_value(parameters, "realtimeAllowedOrigins", "")
    if (
        isinstance(raw_realtime_origins, str)
        and raw_realtime_origins.strip()
        and PLACEHOLDER_RE.match(raw_realtime_origins.strip()) is None
    ):
        errors.append(
            "realtimeAllowedOrigins must not hardcode a hostname in main.parameters.json "
            "(found a literal value): the deployed web origins are derived in main.bicep, "
            "and a literal is tenant-coupled. Use the AI4IA_REALTIME_ALLOWED_ORIGINS "
            "variable to add extra origins."
        )

    if truthy(parameter_value(parameters, "voiceLiveToolsEnabled", False)) and not truthy(
        parameter_value(parameters, "voiceLiveEnabled", False)
    ):
        errors.append("voiceLiveToolsEnabled=true is inert unless voiceLiveEnabled=true.")

    # Speech Voice Live is a second, additive realtime provider. It must never be
    # reachable unless the master Voice Live gate is also on, its allowlist entry
    # is present, and its default (if pointed at Speech) is actually allowlisted.
    # These mirror the fail-closed checks app/api/src/ai4ia_api/config.py enforces
    # at runtime, so a contradictory deployment fails here (at plan time) too.
    speech_voice_live_enabled = truthy(parameter_value(parameters, "speechVoiceLiveEnabled", False))
    voice_provider_allowlist = [
        entry.strip().lower()
        for entry in text(parameter_value(parameters, "voiceProviderAllowlist", "azure_openai")).split(",")
        if entry.strip()
    ]
    voice_default_provider = text(parameter_value(parameters, "voiceDefaultProvider", "azure_openai")).lower()

    if speech_voice_live_enabled and not truthy(parameter_value(parameters, "voiceLiveEnabled", False)):
        errors.append("speechVoiceLiveEnabled=true is inert unless voiceLiveEnabled=true.")

    if "azure_openai" not in voice_provider_allowlist:
        errors.append("voiceProviderAllowlist must always include azure_openai.")

    if voice_default_provider and voice_provider_allowlist and voice_default_provider not in voice_provider_allowlist:
        errors.append("voiceDefaultProvider must be a member of voiceProviderAllowlist.")

    if speech_voice_live_enabled and "speech_voice_live" not in voice_provider_allowlist:
        errors.append(
            "speechVoiceLiveEnabled=true requires voiceProviderAllowlist to include speech_voice_live."
        )

    if not speech_voice_live_enabled and "speech_voice_live" in voice_provider_allowlist:
        errors.append(
            "voiceProviderAllowlist includes speech_voice_live but speechVoiceLiveEnabled is not true; "
            "the provider would be allowlisted but never reachable (or the enablement flag was forgotten)."
        )

    if not text(parameter_value(parameters, "speechVoiceLiveManagedIdentityAudience", "https://ai.azure.com")):
        errors.append(
            "speechVoiceLiveManagedIdentityAudience must not be blanked out; "
            "leave it at its documented default unless a live-validated override is available."
        )

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
        if domain:
            # Managed-cert issuance uses domainControlValidation: 'CNAME', so ARM
            # fails this resource unless public DNS for the host already resolves
            # to *this* environment. On a first provision in a new tenant it does
            # not (and cannot — the app does not exist yet), which kills the run
            # after the expensive resources are already built. See
            # docs/runbooks/deployment.md §3 step 3a for the working order.
            warnings.append(
                f"{domain_name}={domain} requires public DNS (CNAME + asuid TXT) to already point "
                "at this environment's container app; a first provision in a new subscription/tenant "
                "must run with the custom-domain variables EMPTY and bind them on a second pass."
            )

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

    # An action group with no receiver is legal ARM and deploys clean, so nothing
    # else fails when alerts are enabled without a recipient -- the rules evaluate
    # and record, but no one is ever notified. That is the "looks enabled, is inert"
    # shape, so warn rather than stay silent. Not an error: recording alert history
    # is still worth having, and the remedy is one variable.
    if truthy(parameter_value(parameters, "enableAlerts", False)) and not text(
        parameter_value(parameters, "alertEmail")
    ):
        warnings.append(
            "enableAlerts=true with no alertEmail: alert rules will record but "
            "notify nobody. Set AI4IA_ALERT_EMAIL to a deliverable mailbox."
        )

    # Same shape one layer down, and it bit us: the budget is created
    # unconditionally, but budgetAlertEmails is not surfaced in
    # main.parameters.json, so it stayed [] and the deployed budget carried an
    # empty notifications map. A $1500/month guardrail that emails nobody looks
    # identical in the portal to one that works. main.bicep now falls back to
    # alertEmail, so the only remaining silent case is both being empty.
    if not text(parameter_value(parameters, "alertEmail")) and not parameter_value(
        parameters, "budgetAlertEmails", []
    ):
        warnings.append(
            "budget has no notification recipient: thresholds will be tracked but "
            "never emailed. Set AI4IA_ALERT_EMAIL (it feeds the budget too)."
        )

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
