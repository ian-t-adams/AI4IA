"""Exact, separately authenticated Claude binding readbacks. Never writes Azure."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from xml.etree import ElementTree as ET

from _capacity_evidence import EvidenceError, az_command, number, object_value, run_bounded, strict_json
from _model_targets import model_target
from _model_retirement import parse_date

ROOT = Path(__file__).resolve().parents[1]
INFERENCE_ACTIONS = ["Microsoft.CognitiveServices/accounts/MaaS/*"]
FIELDS = frozenset({
    "sourceTenantId", "sourceSubscriptionId", "sourceApimResourceId", "sourceApimPrincipalId",
    "sourceIdentityResourceId", "sourceIdentityClientId", "sourceIdentityPrincipalId",
    "applicationObjectId", "applicationClientId", "federatedCredentialName",
    "targetTenantId", "targetSubscriptionId", "targetResourceGroup", "targetAccountName",
    "targetPrincipalId", "targetReaderClientId", "targetInferenceRoleDefinitionId", "environment", "workload", "networkMode",
})
GUID_FIELDS = frozenset({
    "sourceTenantId", "sourceSubscriptionId", "sourceApimPrincipalId", "sourceIdentityClientId",
    "sourceIdentityPrincipalId", "applicationObjectId", "applicationClientId",
    "targetTenantId", "targetSubscriptionId", "targetPrincipalId", "targetReaderClientId",
})


def require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceError(code)


def object_rows(value: object, limit: int, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > limit:
        raise EvidenceError(code)
    return [object_value(row) for row in value]


@dataclass(frozen=True)
class Binding:
    values: dict[str, str]

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    @property
    def account_id(self) -> str:
        return (
            f"/subscriptions/{self['targetSubscriptionId']}/resourceGroups/{self['targetResourceGroup']}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self['targetAccountName']}"
        )

    @property
    def endpoint(self) -> str:
        return f"https://{self['targetAccountName']}.services.ai.azure.com"

    @property
    def named_values(self) -> dict[str, str]:
        return {
            "claude-target-endpoint": self.endpoint,
            "claude-target-tenant": self["targetTenantId"],
            "claude-app-client": self["applicationClientId"],
            "claude-uami-client": self["sourceIdentityClientId"],
            "claude-proxy-subscription": self["workload"] + "-proxy-models",
        }


def parse_binding(raw: str) -> Binding:
    require(len(raw.encode("utf-8")) <= 8192, "claude_binding_too_large")
    try:
        document = object_value(strict_json(raw.encode("utf-8")))
    except (ValueError, EvidenceError):
        raise EvidenceError("claude_binding_invalid_json") from None
    require(set(document) == FIELDS, "claude_binding_fields")
    data: dict[str, str] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value or value != value.strip():
            raise EvidenceError("claude_binding_values")
        data[key] = value
    for key in GUID_FIELDS:
        try:
            parsed = UUID(data[key])
            require(str(parsed) == data[key] and parsed.int != 0, "claude_binding_guid")
        except ValueError:
            raise EvidenceError("claude_binding_guid") from None
    require(data["sourceTenantId"] != data["targetTenantId"], "claude_binding_same_tenant")
    require(data["networkMode"] == "public-keyless", "claude_network_mode_not_supported")
    require(data["sourceSubscriptionId"] != data["targetSubscriptionId"], "claude_binding_same_subscription")
    require(
        data["targetReaderClientId"] not in (data["applicationClientId"], data["sourceIdentityClientId"]),
        "claude_reader_is_runtime_identity",
    )
    for key in ("targetResourceGroup", "federatedCredentialName", "workload"):
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,89}", data[key]) is not None, "claude_binding_name")
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,18}[a-z0-9]", data["environment"]) is not None, "claude_binding_environment")
    require(
        re.fullmatch(r"mf-claude-[a-z0-9-]{3,48}", data["targetAccountName"]) is not None,
        "claude_binding_dedicated_account",
    )
    for key, provider, kind in (
        ("sourceIdentityResourceId", "Microsoft.ManagedIdentity", "userAssignedIdentities"),
        ("sourceApimResourceId", "Microsoft.ApiManagement", "service"),
    ):
        pattern = (
            rf"/subscriptions/{data['sourceSubscriptionId']}/resourceGroups/[A-Za-z0-9][A-Za-z0-9_.-]{{0,89}}"
            rf"/providers/{re.escape(provider)}/{kind}/[A-Za-z0-9-]{{1,80}}"
        )
        require(re.fullmatch(pattern, data[key]) is not None, "claude_binding_resource_id")
    role_pattern = (
        rf"/subscriptions/{data['targetSubscriptionId']}(?:/resourceGroups/{re.escape(data['targetResourceGroup'])})?"
        r"/providers/Microsoft.Authorization/roleDefinitions/[0-9a-f-]{36}"
    )
    require(re.fullmatch(role_pattern, data["targetInferenceRoleDefinitionId"]) is not None, "claude_binding_role_id")
    return Binding(data)


def configured_binding(environment: dict[str, str] | os._Environ[str] = os.environ) -> Binding | None:
    def enabled(name: str) -> bool:
        value = environment.get(name, "").lower()
        require(value in ("", "true", "false"), "claude_flag_not_boolean")
        return value == "true"

    staged = enabled("AI4IA_CLAUDE_EXTERNAL_ENABLED")
    active = enabled("AI4IA_CLAUDE_ENABLED")
    require(not active or staged, "claude_external_binding_required")
    if not staged:
        return None
    binding = parse_binding(environment.get("AI4IA_CLAUDE_BINDING_JSON", ""))
    require(
        binding["sourceTenantId"] == environment.get("AZURE_TENANT_ID")
        and binding["sourceSubscriptionId"] == environment.get("AZURE_SUBSCRIPTION_ID")
        and binding["environment"] == environment.get("AZURE_ENV_NAME"),
        "claude_source_scope_mismatch",
    )
    require(binding["workload"] == (environment.get("AI4IA_WORKLOAD") or "ai4ia"), "claude_workload_mismatch")
    require(
        environment.get("AZURE_CLIENT_ID") not in {
            binding["targetReaderClientId"], binding["applicationClientId"], binding["sourceIdentityClientId"],
        },
        "claude_deployment_identity_reuse",
    )
    return binding


def requirements(models: dict[str, Any]) -> list[dict[str, Any]]:
    naming = models["naming"]
    result = []
    for model in models["catalog"]:
        if model_target(model) != "external-claude":
            continue
        for deployment in model["deployments"]:
            result.append({
                "name": (
                    f"{model['name']}-{naming['subscriptionToken']}-{deployment['region']}"
                    f"-{naming['skuShort'][deployment['sku']]}"
                ),
                "model": {"format": model["format"], "name": model["name"], "version": deployment["version"]},
                "sku": deployment["sku"], "capacity": deployment["capacity"], "region": deployment["region"],
            })
    require(0 < len(result) <= 16, "claude_catalog_inventory")
    return result


class Reader:
    """No login, account selection, writes, continuation, or raw diagnostics."""

    def __init__(self, binding: Binding, target_config_dir: str, *, runner: Callable = run_bounded) -> None:
        target = Path(target_config_dir).resolve() if target_config_dir else None
        source = Path(os.environ.get("AZURE_CONFIG_DIR", Path.home() / ".azure")).resolve()
        require(target is not None and target.is_dir() and target != source, "claude_isolated_target_reader_required")
        self.binding = binding
        self.target_config_dir = str(target)
        self.source_config_dir = str(source)
        self.runner = runner
        self.deadline = time.monotonic() + 180
        self.calls = 0
        self.bytes = 0

    def command(self, target: bool, args: list[str]) -> dict[str, Any]:
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        require(self.calls <= 40 and remaining > 0 and self.bytes < 8 * 1024 * 1024, "claude_readback_budget")
        subscription = self.binding["targetSubscriptionId" if target else "sourceSubscriptionId"]
        result = self.runner(
            [*az_command(), *args, "--subscription", subscription, "--only-show-errors", "--output", "json"],
            min(20, remaining), min(1024 * 1024, 8 * 1024 * 1024 - self.bytes),
            extra_env={"AZURE_CONFIG_DIR": self.target_config_dir if target else self.source_config_dir},
        )
        self.bytes += len(result.body)
        require(not result.warning and self.bytes <= 8 * 1024 * 1024, "claude_readback_incomplete")
        data = object_value(strict_json(result.body))
        require(not data.get("nextLink") and not data.get("@odata.nextLink"), "claude_readback_continuation")
        return data

    def get(self, target: bool, path: str, version: str, *, graph: bool = False) -> dict[str, Any]:
        host = "https://graph.microsoft.com/v1.0" if graph else "https://management.azure.com"
        query = "" if graph else f"?api-version={version}"
        if not graph and (path.endswith("/policies/policy") or "/policyFragments/" in path):
            query += "&format=rawxml"
        return self.command(target, ["rest", "--method", "GET", "--url", host + path + query])


def verify_identity(binding: Binding, reader: Reader, *, attached: bool) -> None:
    for target in (False, True):
        context = reader.command(target, ["account", "show"])
        prefix = "target" if target else "source"
        require(
            context.get("id") == binding[prefix + "SubscriptionId"]
            and context.get("tenantId") == binding[prefix + "TenantId"],
            "claude_reader_scope_mismatch",
        )
        if target:
            require(
                context.get("user", {}).get("type") == "servicePrincipal"
                and context["user"].get("name") == binding["targetReaderClientId"],
                "claude_target_reader_identity_mismatch",
            )
    identity = reader.get(False, binding["sourceIdentityResourceId"], "2023-01-31")
    require(
        identity.get("id", "").lower() == binding["sourceIdentityResourceId"].lower()
        and identity.get("properties", {}).get("tenantId") == binding["sourceTenantId"]
        and identity["properties"].get("clientId") == binding["sourceIdentityClientId"]
        and identity["properties"].get("principalId") == binding["sourceIdentityPrincipalId"],
        "claude_uami_mismatch",
    )
    apim = reader.get(False, binding["sourceApimResourceId"], "2024-05-01")
    principal = object_value(apim.get("identity"))
    require(
        apim.get("id", "").lower() == binding["sourceApimResourceId"].lower()
        and principal.get("tenantId") == binding["sourceTenantId"]
        and principal.get("principalId") == binding["sourceApimPrincipalId"]
        and principal.get("type") in ("SystemAssigned", "SystemAssigned, UserAssigned"),
        "claude_apim_system_identity_changed",
    )
    if attached:
        users = object_value(principal.get("userAssignedIdentities"))
        matching = [value for key, value in users.items() if key.lower() == binding["sourceIdentityResourceId"].lower()]
        require(len(matching) == 1, "claude_uami_not_attached")
        observed = object_value(matching[0])
        require(
            observed.get("clientId") == binding["sourceIdentityClientId"]
            and observed.get("principalId") == binding["sourceIdentityPrincipalId"],
            "claude_uami_not_attached",
        )
    app = reader.get(False, f"/applications/{binding['applicationObjectId']}", "", graph=True)
    require(
        app.get("id") == binding["applicationObjectId"] and app.get("appId") == binding["applicationClientId"]
        and app.get("signInAudience") == "AzureADMultipleOrgs"
        and app.get("passwordCredentials") == [] and app.get("keyCredentials") == [],
        "claude_application_mismatch",
    )
    fics = reader.get(False, f"/applications/{binding['applicationObjectId']}/federatedIdentityCredentials", "", graph=True)
    rows = object_rows(fics.get("value"), 20, "claude_federation_inventory")
    require(len(rows) == 1, "claude_federation_inventory")
    fic = rows[0]
    require(
        fic.get("name") == binding["federatedCredentialName"]
        and fic.get("issuer") == f"https://login.microsoftonline.com/{binding['sourceTenantId']}/v2.0"
        and fic.get("subject") == binding["sourceIdentityPrincipalId"]
        and fic.get("audiences") == ["api://AzureADTokenExchange"],
        "claude_federation_mismatch",
    )
    principal = reader.get(True, f"/servicePrincipals/{binding['targetPrincipalId']}", "", graph=True)
    require(
        principal.get("id") == binding["targetPrincipalId"]
        and principal.get("appId") == binding["applicationClientId"]
        and principal.get("appOwnerOrganizationId") == binding["sourceTenantId"]
        and principal.get("accountEnabled") is True and principal.get("servicePrincipalType") == "Application",
        "claude_target_principal_mismatch",
    )


def verify_target(binding: Binding, models: dict[str, Any], reader: Reader) -> int:
    expected = requirements(models)
    account = reader.get(True, binding.account_id, "2025-04-01-preview")
    properties = object_value(account.get("properties"))
    require(
        account.get("id", "").lower() == binding.account_id.lower()
        and account.get("name") == binding["targetAccountName"] and account.get("kind") == "AIServices"
        and account.get("location") == "eastus2" and properties.get("provisioningState") == "Succeeded"
        and properties.get("disableLocalAuth") is True
        and properties.get("publicNetworkAccess") == "Enabled"
        and properties.get("customSubDomainName") == binding["targetAccountName"]
        and account.get("tags", {}).get("ai4ia-target") == "external-claude"
        and account.get("tags", {}).get("azd-env-name") == binding["environment"],
        "claude_target_account_mismatch",
    )
    deployment_rows = object_rows(
        reader.get(True, binding.account_id + "/deployments", "2025-10-01-preview").get("value"),
        16, "claude_deployment_inventory",
    )
    require(len(deployment_rows) == len(expected), "claude_deployment_inventory")
    by_name = {row.get("name"): row for row in deployment_rows}
    require(len(by_name) == len(expected), "claude_deployment_duplicates")
    for desired in expected:
        row = by_name.get(desired["name"], {})
        props = object_value(row.get("properties", {}))
        sku = object_value(row.get("sku", {}))
        require(
            row.get("id", "").lower() == (binding.account_id + "/deployments/" + desired["name"]).lower()
            and props.get("provisioningState") == "Succeeded"
            and isinstance(props.get("model"), dict)
            and {key: props["model"].get(key) for key in desired["model"]} == desired["model"]
            and props.get("versionUpgradeOption") == "NoAutoUpgrade"
            and sku.get("name") == desired["sku"] and type(sku.get("capacity")) is int
            and sku["capacity"] == desired["capacity"],
            "claude_deployment_mismatch",
        )
    roles = reader.get(True, binding.account_id + "/providers/Microsoft.Authorization/roleAssignments", "2022-04-01")
    assignments = object_rows(roles.get("value"), 256, "claude_role_inventory")
    matches = [
        row.get("properties", {}) for row in assignments
        if row.get("properties", {}).get("principalId") == binding["targetPrincipalId"]
    ]
    require(
        len(matches) == 1 and matches[0].get("scope", "").lower() == binding.account_id.lower()
        and matches[0].get("principalType") == "ServicePrincipal"
        and matches[0].get("roleDefinitionId", "").lower() == binding["targetInferenceRoleDefinitionId"].lower()
        and not matches[0].get("condition"),
        "claude_inference_role_mismatch",
    )
    role = reader.get(True, binding["targetInferenceRoleDefinitionId"], "2022-04-01")
    props = role.get("properties", {})
    require(
        role.get("id", "").lower() == binding["targetInferenceRoleDefinitionId"].lower()
        and props.get("type") == "CustomRole"
        and props.get("assignableScopes") == [binding.account_id.rsplit("/providers/", 1)[0]]
        and props.get("permissions") == [{
            "actions": [], "notActions": [], "dataActions": INFERENCE_ACTIONS, "notDataActions": [],
        }],
        "claude_inference_role_permissions",
    )
    return len(expected)


def target_preflight(binding: Binding, models: dict[str, Any], reader: Reader) -> int:
    """New isolated account only: exact offering/usageName/platform, no pool inference."""
    context = reader.command(True, ["account", "show"])
    require(
        context.get("id") == binding["targetSubscriptionId"]
        and context.get("tenantId") == binding["targetTenantId"]
        and context.get("user", {}).get("name") == binding["targetReaderClientId"],
        "claude_reader_scope_mismatch",
    )
    group = binding.account_id.rsplit("/providers/", 1)[0]
    accounts = reader.get(True, group + "/providers/Microsoft.CognitiveServices/accounts", "2025-04-01-preview")
    require(accounts.get("value") == [], "claude_target_group_not_empty")
    expected = requirements(models)
    region = expected[0]["region"]
    base = f"/subscriptions/{binding['targetSubscriptionId']}/providers/Microsoft.CognitiveServices"
    offers = object_rows(
        reader.get(True, base + f"/locations/{region}/models", "2024-10-01").get("value"),
        8192, "claude_offerings_unavailable",
    )
    quotas = object_rows(
        reader.get(True, base + f"/locations/{region}/usages", "2024-10-01").get("value"),
        4096, "claude_quota_unavailable",
    )
    capacities = {}
    now = datetime.now(UTC)
    for desired in expected:
        identity = desired["model"]
        matches = [
            row["model"] for row in offers if isinstance(row, dict) and isinstance(row.get("model"), dict)
            and all(row["model"].get(key) == value for key, value in identity.items())
        ]
        require(bool(matches), "claude_exact_offering_missing")
        projections = []
        for model in matches:
            require(
                model.get("lifecycleStatus") == "GenerallyAvailable"
                and model.get("capabilities", {}).get("hostedOn") == "azure",
                "claude_offering_not_ga_azure",
            )
            skus = [row for row in model.get("skus", []) if row.get("name") == desired["sku"]]
            require(len(skus) == 1, "claude_offered_sku_ambiguous")
            sku = skus[0]
            for date in (sku.get("deprecationDate"), model.get("deprecation", {}).get("inference")):
                parsed, state, _ = parse_date(date)
                require(state == "known" and parsed is not None and parsed > now + timedelta(days=7), "claude_retirement_unavailable_or_unsafe")
            counter = sku.get("usageName")
            require(isinstance(counter, str) and 0 < len(counter) <= 180, "claude_offered_counter_missing")
            bounds = sku.get("capacity", {})
            require(isinstance(bounds, dict), "claude_offered_capacity_unknown")
            capacity = desired["capacity"]
            if bounds.get("maximum") is not None:
                require(capacity <= number(bounds["maximum"]), "claude_offered_capacity_exceeded")
            if bounds.get("minimum") is not None:
                require(capacity >= number(bounds["minimum"]), "claude_offered_capacity_below_minimum")
            if bounds.get("allowedValues") is not None:
                require(capacity in bounds["allowedValues"], "claude_capacity_not_offered")
            if bounds.get("step") is not None:
                step = number(bounds["step"])
                require(step > 0 and capacity % step == 0, "claude_capacity_step")
            projections.append((counter, sku.get("deprecationDate"), model.get("deprecation", {}).get("inference")))
        require(len(set(projections)) == 1, "claude_conflicting_offerings")
        quota = [row for row in quotas if row.get("name", {}).get("value") == projections[0][0]]
        require(len(quota) == 1, "claude_exact_quota_counter_missing")
        remaining = number(quota[0].get("limit")) - number(quota[0].get("currentValue"))
        require(remaining >= desired["capacity"], "claude_raw_quota_insufficient")
        key = (identity["format"], identity["name"], identity["version"])
        if key not in capacities:
            # Fixed ARM host/path and catalog-only query values; no observed URL is followed.
            from urllib.parse import urlencode

            query = urlencode({"modelFormat": key[0], "modelName": key[1], "modelVersion": key[2]})
            capacities[key] = reader.command(True, [
                "rest", "--method", "GET", "--url",
                f"https://management.azure.com{base}/modelCapacities?api-version=2024-10-01&{query}",
            ]).get("value")
        platform = object_rows(capacities[key], 256, "claude_platform_capacity_unknown")
        rows = [
            row for row in platform if row.get("location") == region
            and row.get("properties", {}).get("skuName") == desired["sku"]
        ]
        require(
            len(rows) == 1 and rows[0].get("properties", {}).get("model") == identity,
            "claude_platform_identity_mismatch",
        )
        require(number(rows[0]["properties"].get("availableCapacity")) >= desired["capacity"], "claude_platform_capacity_insufficient")
    return len(expected)


def verify_routes(binding: Binding, reader: Reader, *, enabled: bool) -> None:
    apim = binding["sourceApimResourceId"]
    api_path = apim + "/apis/openai"
    before = reader.get(False, api_path + "/policies/policy", "2024-05-01")
    actual = before.get("properties", {}).get("value", "")
    expected = (ROOT / "infra" / "policies" / "simplel7proxy-priority-policy.xml").read_text(encoding="utf-8")
    root = ET.fromstring(actual)
    references = [node.attrib["fragment-id"] for node in root.iter("include-fragment")]
    expected_refs = [node.attrib["fragment-id"] for node in ET.fromstring(expected).iter("include-fragment")]
    require(len(references) == len(expected_refs), "claude_route_fragment_inventory")
    for reference, base in zip(references, expected_refs, strict=True):
        require(re.fullmatch(re.escape(base) + r"-[a-z0-9]{13}", reference) is not None, "claude_route_fragment_id")
        expected = expected.replace(f'"{base}"', f'"{reference}"')
        fragment = reader.get(False, apim + "/policyFragments/" + reference, "2024-05-01")
        filename = base + ".xml"
        if base == "claude_auth_v1":
            filename = "claude-federated-auth.xml" if enabled else "claude-disabled.xml"
        elif base == "endpoint_selection_setup_32":
            filename = "simplel7proxy-endpoints.xml"
        elif base.startswith("endpoint_selection_catalog_"):
            filename = "simplel7proxy-endpoints-catalog-" + base.split("_")[3] + ".xml"
        wanted = (ROOT / "infra" / "policies" / filename).read_text(encoding="utf-8")
        require(fragment.get("properties", {}).get("value", "").replace("\r\n", "\n") == wanted, "claude_route_fragment_changed")
    require(actual.replace("\r\n", "\n") == expected, "claude_route_policy_changed")
    named = reader.get(False, apim + "/namedValues", "2024-05-01")
    rows = object_rows(named.get("value"), 256, "claude_named_value_inventory")
    for name, value in binding.named_values.items():
        matches = [row for row in rows if row.get("name") == name]
        require(
            len(matches) == 1 and matches[0].get("properties", {}).get("value") == value
            and matches[0]["properties"].get("secret") is False,
            "claude_named_value_mismatch",
        )
    subscription = reader.get(False, apim + "/subscriptions/" + binding["workload"] + "-proxy-models", "2024-05-01")
    props = subscription.get("properties", {})
    require(
        props.get("scope", "").lower() in {api_path.lower(), "/apis/openai"}
        and props.get("state") == "active" and props.get("allowTracing") is False,
        "claude_proxy_subscription_scope",
    )
    after = reader.get(False, api_path + "/policies/policy", "2024-05-01")
    require(after == before, "claude_route_changed_during_readback")


def verify_binding_transition(binding: Binding, reader: Reader) -> None:
    """A changed binding cannot be installed under a currently live auth policy."""
    apim = binding["sourceApimResourceId"]
    inventory = object_rows(
        reader.get(False, apim + "/namedValues", "2024-05-01").get("value"),
        256, "claude_named_value_inventory",
    )
    wanted = binding.named_values
    rows = [row for row in inventory if row.get("name") in wanted]
    observed = {row["name"]: row.get("properties", {}).get("value") for row in rows}
    require(len(rows) == len(observed), "claude_named_value_duplicates")
    if observed == wanted:
        return
    policy = reader.get(False, apim + "/apis/openai/policies/policy", "2024-05-01")
    root = ET.fromstring(policy.get("properties", {}).get("value", ""))
    refs = [
        node.attrib.get("fragment-id", "") for node in root.iter("include-fragment")
        if node.attrib.get("fragment-id", "").startswith("claude_auth_v1-")
    ]
    if not observed and not refs:
        require(os.environ.get("AI4IA_CLAUDE_ENABLED", "").lower() != "true", "claude_initial_disabled_stage_required")
        return
    require(
        len(refs) == 1 and re.fullmatch(r"claude_auth_v1-[a-z0-9]{13}", refs[0]) is not None,
        "claude_current_auth_unknown",
    )
    fragment = reader.get(False, apim + "/policyFragments/" + refs[0], "2024-05-01")
    disabled = (ROOT / "infra" / "policies" / "claude-disabled.xml").read_text(encoding="utf-8")
    require(
        fragment.get("properties", {}).get("value", "").replace("\r\n", "\n") == disabled,
        "claude_disable_old_binding_before_change",
    )


def verify_configured(models: dict[str, Any], *, routed: bool = False, target_plan: bool = False) -> int:
    binding = configured_binding()
    if binding is None:
        return 0
    reader = Reader(binding, os.environ.get("AI4IA_CLAUDE_TARGET_AZURE_CONFIG_DIR", ""))
    if target_plan:
        return target_preflight(binding, models, reader)
    verify_identity(binding, reader, attached=routed)
    count = verify_target(binding, models, reader)
    if routed:
        verify_routes(binding, reader, enabled=os.environ.get("AI4IA_CLAUDE_ENABLED", "").lower() == "true")
    else:
        verify_binding_transition(binding, reader)
    return count
