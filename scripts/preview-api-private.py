#!/usr/bin/env python3
"""Bounded full-phase ARM preview using the existing trusted runner configuration."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any

from _api_private import api_name, from_environment
from _private_recovery_packets import (
    MODULES, PacketError, api_overrides, canonical_location, configuration_intent, consumption_headroom,
    location_evidence, read_record, record_parameters,
)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?:=([^}]*))?\}")
BINDING = re.compile(r"([0-9]{10}):([0-9a-f]{64})")
MAX_AGE = 1800


def load_sibling(name: str):
    spec = importlib.util.spec_from_file_location("private_preview_" + name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ROLLOUT = load_sibling("post-deploy-verify")
PROVENANCE = load_sibling("verify-image-provenance")
ADMISSION = load_sibling("check-api-private-network")


class PreviewError(ValueError):
    """Fixed diagnostic text only; never parameters or provider payloads."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PreviewError(message)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def resolve_parameters(source: dict, template: dict, env: dict[str, str]) -> dict:
    """Only this repo's whole-token scalar contract; unsupported forms refuse."""
    definitions = template["parameters"]
    result = {}
    for name, row in source["parameters"].items():
        require(name in definitions and isinstance(row, dict) and set(row) == {"value"},
                "Unsupported parameter declaration.")
        value = row["value"]
        if isinstance(value, str):
            match = TOKEN.fullmatch(value)
            if match is None:
                raise PreviewError("Preview requires whole-token parameter declarations.")
            value = env.get(match[1]) or match[2] or ""
            # azd 1.29 substitutes these scalars into JSON without escaping.
            # Refuse rather than produce a safe preview of different deploy bytes.
            require(not any(char in value for char in ('"', "\\", "\r", "\n", "\t"))
                    and "$(" not in value, "Parameter cannot use the unchanged azd scalar transport.")
        kind = definitions[name]["type"].lower()
        if kind in ("string", "securestring"):
            require(isinstance(value, str), "String parameter type mismatch.")
        elif kind == "bool":
            if isinstance(value, str):
                require(value in ("true", "false", "True", "False", "TRUE", "FALSE", "1", "0", "t", "f", "T", "F"),
                        "Boolean parameter type mismatch.")
                value = value in ("true", "True", "TRUE", "1", "t", "T")
            require(type(value) is bool, "Boolean parameter type mismatch.")
        elif kind == "int":
            if isinstance(value, str):
                require(re.fullmatch(r"[+-]?[0-9]+", value) is not None, "Integer parameter type mismatch.")
                value = int(value)
            require(type(value) is int and -(2 ** 63) <= value < 2 ** 63, "Integer parameter type mismatch.")
        else:
            raise PreviewError("Unsupported compiled parameter type.")
        result[name] = {"value": value}
    require(all("defaultValue" in definitions[name] for name in set(definitions) - set(result)),
            "A required compiled parameter is missing.")
    return result


def context(env: dict[str, str], template: dict, params: dict) -> dict:
    require(env.get("GITHUB_REF") == "refs/heads/main", "Preview requires trusted main.")
    head = env.get("GITHUB_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", head) is not None, "Source commit is unknown.")
    selected = from_environment(env)
    require(selected != "legacy", "Configure the reviewed private phase.")
    subscription = env.get("AZURE_SUBSCRIPTION_ID", "")
    require(re.fullmatch(r"[0-9a-fA-F-]{36}", subscription) is not None, "Subscription is unknown.")
    environment = env["AZURE_ENV_NAME"]
    api_name(environment, selected)
    workload = env.get("AI4IA_WORKLOAD") or "ai4ia"
    require(re.fullmatch(r"[a-z][a-z0-9-]{2,19}", workload) is not None, "Workload is invalid.")
    cosmos = re.fullmatch(r"https://([a-z0-9-]+)\.documents\.azure\.com(?::443)?/?",
                          env.get("AI4IA_PRIVATE_COSMOS_ENDPOINT", ""))
    if cosmos is None:
        raise PreviewError("Canonical Cosmos identity is unknown.")
    require(params["appEnvironment"]["value"] == "prod" and params["apiAuthProvider"]["value"] == "entra"
            and params["entraTenantId"]["value"] == env.get("AZURE_TENANT_ID"),
            "Private recovery cannot change the production authentication boundary.")
    return {
        "source": head, "phase": selected, "subscription": subscription.casefold(),
        "environment": environment, "workload": workload, "location": params["location"]["value"],
        "resourceGroup": f"rg-{workload}-{environment}", "template": digest(template), "parameters": digest(params),
        "cosmosAccount": cosmos[1],
        "vnetPrefix": params["apiPrivateVnetPrefix"]["value"],
        "apiSubnetPrefix": params["apiPrivateSubnetPrefix"]["value"],
        "endpointSubnetPrefix": params["cosmosPrivateEndpointSubnetPrefix"]["value"],
        "apiOrigin": env.get("AI4IA_PRIVATE_API_ORIGIN", ""),
    }


def env_rows(template: dict) -> dict[str, dict]:
    containers = template.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise PreviewError("Unexpected container inventory.")
    rows = containers[0].get("env")
    if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in rows):
        raise PreviewError("Container environment is incomplete.")
    result = {row["name"]: row for row in rows}
    require(len(result) == len(rows), "Duplicate container environment fields.")
    return result


def comparable(template: dict) -> str:
    # Reuse the existing provider-evidenced writable-template comparison.
    return ROLLOUT._comparable_template(template)


def created_fields(resource: dict) -> list[str]:
    """Report complete structural field names, never property or dynamic-map values."""
    fields = set()
    remaining = 4096
    def visit(value, path="", depth=0):
        nonlocal remaining
        remaining -= 1
        require(remaining >= 0 and depth <= 16, "Created resource field coverage exceeds its bound.")
        if path:
            fields.add(path)
        if path in ("tags", "identity.userAssignedIdentities"):
            return
        if isinstance(value, dict):
            for key, child in value.items():
                require(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", key) is not None,
                        "Created resource field name is unknown.")
                visit(child, f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, path + "[]", depth + 1)
    visit(resource)
    require(len(fields) <= 512 and all(len(path) <= 256 for path in fields),
            "Created resource field coverage exceeds its bound.")
    return sorted(fields)


def rejected_report(raw: Any, bound: dict) -> dict:
    """Keep only diagnostic metadata; provider prose and resource values stay private."""
    def resource_id(value: Any) -> str | None:
        if not isinstance(value, str) or re.fullmatch(r"/[A-Za-z0-9_./()-]{1,511}", value) is None:
            return None
        normalized = value.casefold()
        root = f"/subscriptions/{bound['subscription']}".casefold()
        group = f"{root}/resourcegroups/{bound['resourceGroup']}".casefold()
        if (normalized == group or normalized.startswith(group + "/providers/")
                or normalized.startswith(root + "/providers/microsoft.resources/deployments/")):
            return value
        return None

    def property_path(value: Any) -> str | None:
        if isinstance(value, str) and re.fullmatch(
            r"(?:\$\.)?(?:properties|parameters|variables|resources)(?:[./][A-Za-z0-9_.\[\]/-]+){1,16}",
            value,
        ) and len(value) <= 128:
            return value
        return None

    source = raw if isinstance(raw, dict) else {}
    diagnostics = source.get("diagnostics")
    rows = diagnostics if isinstance(diagnostics, list) else []
    safe_diagnostics = []
    for item in rows[:16]:
        item = item if isinstance(item, dict) else {}
        code = item.get("code")
        level = item.get("level")
        target = item.get("target")
        safe_diagnostics.append({
            "code": code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Za-z0-9_.-]{0,95}", code) else "unknown",
            "level": level if level in ("Info", "Warning", "Error") else "unknown",
            "target": resource_id(target) or property_path(target),
            "targetOmitted": target is not None and not (resource_id(target) or property_path(target)),
        })
    changes = source.get("changes")
    changed_rows = changes if isinstance(changes, list) else []
    safe_changes = []
    kinds = {"Create", "Modify", "Delete", "Deploy", "Ignore", "NoChange", "Unsupported"}
    for item in changed_rows[:32]:
        item = item if isinstance(item, dict) else {}
        deltas = item.get("delta")
        deltas = deltas if isinstance(deltas, list) else []
        paths = [property_path(row.get("path")) for row in deltas[:8] if isinstance(row, dict)]
        kind = item.get("changeType")
        safe_changes.append({
            "resourceId": resource_id(item.get("resourceId")),
            "changeType": kind if isinstance(kind, str) and kind in kinds else "unknown",
            "fields": [path for path in paths if path is not None],
            "deltaPresent": "delta" in item,
            "deltaType": ({
                type(None): "null", list: "array", dict: "object", bool: "boolean",
                str: "string", int: "number", float: "number",
            }.get(type(item.get("delta")), "unknown") if "delta" in item else "absent"),
        })
    report = {
        "context": {key: bound[key] for key in ("source", "phase", "subscription", "resourceGroup", "template", "parameters")
                    if isinstance(bound.get(key), str) and len(bound[key]) <= 512},
        "status": "rejected",
        "category": ("arm_diagnostics_shape_unknown" if diagnostics is not None and not isinstance(diagnostics, list)
                     else "arm_diagnostics_present" if diagnostics else "preview_evidence_rejected"),
        "diagnostics": safe_diagnostics, "diagnosticCount": len(rows) if isinstance(diagnostics, list) else None,
        "diagnosticsShapeUnknown": diagnostics is not None and not isinstance(diagnostics, list),
        "changes": safe_changes, "changeCount": len(changed_rows) if isinstance(changes, list) else None,
        "truncated": len(rows) > 16 or len(changed_rows) > 32,
        "binding": None, "executionAuthorized": False,
    }
    while len(json.dumps(report, indent=2).encode()) > 64 * 1024 and report["changes"]:
        report["changes"].pop()
        report["truncated"] = True
    return report


def scope_preview(raw: dict, binding: dict, params: dict, baseline: dict | None = None) -> dict:
    require(raw.get("status") == "Succeeded" and not raw.get("error"), "ARM preview did not succeed.")
    diagnostics = raw.get("diagnostics")
    require(diagnostics is None or (isinstance(diagnostics, list) and not diagnostics),
            "ARM preview reported incomplete diagnostics.")
    changes = raw.get("changes")
    if not isinstance(changes, list) or not 0 < len(changes) <= 2048:
        raise PreviewError("ARM preview coverage is missing or oversized.")
    sub = binding["subscription"]
    group = binding["resourceGroup"]
    prefix = f"/subscriptions/{sub}/resourcegroups/{group}/providers/"
    suffix = f"{binding['workload']}-{binding['environment']}-api-private"
    vnet = prefix + f"microsoft.network/virtualnetworks/vnet-{suffix}"
    private_api = prefix + f"microsoft.app/containerapps/{api_name(binding['environment'], 'private')}"
    legacy_api = prefix + f"microsoft.app/containerapps/{api_name(binding['environment'], 'legacy')}"
    private_env = prefix + f"microsoft.app/managedenvironments/cae-{suffix}"
    zone = prefix + "microsoft.network/privatednszones/privatelink.documents.azure.com"
    endpoint = prefix + f"microsoft.network/privateendpoints/pe-{suffix}-cosmos"
    creates = {vnet, private_api, private_env, zone, zone + f"/virtualnetworklinks/{suffix}",
               endpoint, endpoint + "/privatednszonegroups/default"}
    app_ids = {private_api: "api", prefix + f"microsoft.app/containerapps/ca-web-{binding['environment']}": "web",
               prefix + f"microsoft.app/containerapps/ca-proxy-{binding['environment']}": "proxy"}
    packet = binding.get("packet")
    required = creates
    permitted = None
    if packet is not None:
        require(packet in MODULES, "Unknown scoped recovery packet.")
        permitted = {
            "foundation": creates - {private_api}, "candidate": {private_api},
            "activate": {private_api},
            "cutover": {prefix + f"microsoft.app/containerapps/ca-web-{binding['environment']}"},
        }[packet]
        required = permitted
        creates = permitted if packet in ("foundation", "candidate") else set()
    report = []
    seen = set()
    errors = []
    for change in changes:
        rid = change.get("resourceId")
        kind = change.get("changeType")
        require(isinstance(rid, str) and 0 < len(rid) <= 1024 and rid.startswith("/subscriptions/"),
                "ARM resource identity is incomplete.")
        rid = rid.casefold()
        require(rid not in seen, "Duplicate ARM resource change.")
        seen.add(rid)
        require(kind in ("Create", "Modify", "Delete", "Deploy", "Ignore", "NoChange", "Unsupported"),
                "Unknown ARM change type.")
        require(not change.get("unsupportedReason"), "ARM preview contains unsupported coverage.")
        if packet in ("candidate", "activate", "cutover") and rid in required:
            after_resource = change.get("after")
            if isinstance(after_resource, dict) and "configuration" in after_resource.get("properties", {}):
                configuration_intent(after_resource["properties"]["configuration"])
        if kind in ("Ignore", "NoChange"):
            require(not (kind == "Ignore" and rid in required), "Required resource coverage was ignored.")
            continue
        if not rid.startswith(prefix) and rid != prefix.split("/providers/")[0]:
            errors.append("change_outside_workload")
            continue
        before, after = change.get("before"), change.get("after")
        row = {"resourceId": rid, "changeType": kind, "fields": []}
        deltas = change.get("delta", [])
        if kind == "Create" and deltas is None:
            deltas = []
        require(isinstance(deltas, list), "ARM property changes are incomplete.")
        require(kind != "Modify" or bool(deltas), "ARM Modify property changes are missing.")
        for delta in deltas:
            require(isinstance(delta, dict), "ARM property change entry is malformed.")
            path = delta.get("path")
            require(isinstance(path, str) and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,256}", path) is not None,
                    "Unsupported ARM property path shape.")
            row["fields"].append(path)
        report.append(row)
        if permitted is not None and rid not in permitted:
            errors.append("change_outside_packet")
            continue
        if kind == "Create":
            if rid not in creates or not isinstance(after, dict):
                errors.append("unapproved_create")
                continue
            row["fields"] = created_fields(after)
            if "SecurityControl" in after.get("tags", {}):
                errors.append("security_exemption_tag")
            observed_location = after.get("location")
            if rid in (zone, zone + f"/virtualnetworklinks/{suffix}"):
                if observed_location != "global":
                    errors.append("unapproved_region")
                else:
                    row["location"] = {"raw": observed_location, "placement": "global-dns-resource"}
            elif rid != endpoint + "/privatednszonegroups/default" or observed_location is not None:
                canonical = canonical_location(observed_location, binding["locationIdentity"])
                row["location"] = {"raw": observed_location, "canonical": canonical}
                if canonical != binding["location"]:
                    errors.append("unapproved_region")
            props = after.get("properties", {})
            if rid == vnet:
                if (props.get("virtualNetworkPeerings") not in (None, [])
                        or (props.get("enableDdosProtection") is not None and props.get("enableDdosProtection") is not False)
                        or props.get("ddosProtectionPlan") is not None):
                    errors.append("unapproved_network_addition")
                expected = [params["apiPrivateVnetPrefix"]["value"]]
                if props.get("addressSpace", {}).get("addressPrefixes") != expected:
                    errors.append("vnet_address_drift")
                subnets = props.get("subnets", [])
                expected_subnets = {"api": params["apiPrivateSubnetPrefix"]["value"],
                                    "cosmos": params["cosmosPrivateEndpointSubnetPrefix"]["value"]}
                if (not isinstance(subnets, list) or len(subnets) != 2
                        or {row.get("name"): row.get("properties", {}).get("addressPrefix") for row in subnets} != expected_subnets
                        or props.get("dhcpOptions", {}).get("dnsServers") != []):
                    errors.append("subnet_or_dns_drift")
                if isinstance(subnets, list):
                    if any(subnet.get("properties", {}).get(key) not in (None, [])
                           for subnet in subnets for key in
                           ("natGateway", "networkSecurityGroup", "routeTable", "serviceEndpoints", "serviceEndpointPolicies")):
                        errors.append("unapproved_subnet_attachment")
                    api_subnet = next((row for row in subnets if row.get("name") == "api"), {})
                    pe_subnet = next((row for row in subnets if row.get("name") == "cosmos"), {})
                    delegates = api_subnet.get("properties", {}).get("delegations", [])
                    if (len(delegates) != 1 or delegates[0].get("properties", {}).get("serviceName") != "Microsoft.App/environments"
                            or pe_subnet.get("properties", {}).get("privateEndpointNetworkPolicies") != "Disabled"):
                        errors.append("subnet_delegation_drift")
            elif rid == private_env:
                network = props.get("vnetConfiguration", {})
                if (network.get("infrastructureSubnetId", "").casefold() != vnet + "/subnets/api"
                        or network.get("internal") is not False
                        or props.get("publicNetworkAccess") != "Enabled"
                        or props.get("workloadProfiles") != [{"name": "Consumption", "workloadProfileType": "Consumption"}]):
                    errors.append("environment_network_drift")
            elif rid == endpoint:
                connections = props.get("privateLinkServiceConnections", [])
                cosmos_id = prefix + "microsoft.documentdb/databaseaccounts/" + binding["cosmosAccount"]
                if (props.get("subnet", {}).get("id", "").casefold() != vnet + "/subnets/cosmos"
                        or len(connections) != 1 or connections[0].get("properties", {}).get("groupIds") != ["Sql"]
                        or connections[0].get("properties", {}).get("privateLinkServiceId", "").casefold() != cosmos_id):
                    errors.append("cosmos_endpoint_drift")
            elif rid == endpoint + "/privatednszonegroups/default":
                groups = props.get("privateDnsZoneConfigs", [])
                if len(groups) != 1 or groups[0].get("properties", {}).get("privateDnsZoneId", "").casefold() != zone:
                    errors.append("dns_zone_group_drift")
            elif rid == zone + f"/virtualnetworklinks/{suffix}":
                if props.get("virtualNetwork", {}).get("id", "").casefold() != vnet or props.get("registrationEnabled") is not False:
                    errors.append("dns_link_drift")
            elif rid == private_api:
                identity = after.get("identity", {})
                expected_identity = prefix + f"microsoft.managedidentity/userassignedidentities/id-api-{binding['environment']}"
                if identity.get("type") != "UserAssigned" or set(k.casefold() for k in identity.get("userAssignedIdentities", {})) != {expected_identity}:
                    errors.append("api_identity_drift")
                if props.get("managedEnvironmentId", props.get("environmentId", "")).casefold() != private_env:
                    errors.append("api_environment_drift")
                template = props.get("template", {})
                env = env_rows(template)
                if env.get("AI4IA_PRIVATE_RECOVERY_CANDIDATE", {}).get("value") != "true" or binding["phase"] != "candidate":
                    errors.append("unstaged_api_create")
                if env.get("AI4IA_PRIVATE_COSMOS_REQUIRED", {}).get("value") != "true":
                    errors.append("missing_private_readiness")
                cosmos_target = env.get("AI4IA_COSMOS_ENDPOINT", {}).get("value")
                if not isinstance(cosmos_target, str) or re.fullmatch(
                    r"https://" + re.escape(binding["cosmosAccount"]) + r"\.documents\.azure\.com(?::443)?/?",
                    cosmos_target,
                ) is None:
                    errors.append("api_cosmos_target_drift")
                for name, expected in (
                    ("AI4IA_ENV", "prod"), ("AI4IA_AUTH_PROVIDER", "entra"),
                    ("AI4IA_ENTRA_TENANT_ID", params["entraTenantId"]["value"]),
                    ("AI4IA_ENTRA_AUDIENCE", params["entraAudience"]["value"]),
                ):
                    if env.get(name, {}).get("value") != expected:
                        errors.append("api_authentication_drift")
                if (template.get("scale", {}).get("minReplicas") != 1
                        or template.get("scale", {}).get("maxReplicas") != 1
                        or type(template.get("scale", {}).get("minReplicas")) is not int
                        or type(template.get("scale", {}).get("maxReplicas")) is not int
                        or template["containers"][0].get("image") != params["apiPrivateInitialImage"]["value"]
                        or template["containers"][0].get("resources", {}).get("cpu") != 0.5
                        or template["containers"][0].get("resources", {}).get("memory") != "1Gi"
                        or props.get("configuration", {}).get("activeRevisionsMode") != "Single"):
                    errors.append("candidate_image_or_scale_drift")
                ingress = props.get("configuration", {}).get("ingress", {})
                if (ingress.get("external") is not True or ingress.get("targetPort") != 8080
                        or ingress.get("allowInsecure") is not False):
                    errors.append("api_ingress_drift")
                if packet == "candidate":
                    if not isinstance(baseline, dict):
                        raise PreviewError("Current API configuration evidence is missing.")
                    left = deepcopy(baseline["properties"]["template"])
                    right = deepcopy(template)
                    metric = "AI4IA_METRICS_CONTAINER_APP_RESOURCE_ID"
                    old_self, new_self = env_rows(left).get(metric, {}), env_rows(right).get(metric, {})
                    source_id = baseline.get("id")
                    if (not isinstance(source_id, str) or source_id.casefold() != legacy_api
                            or set(old_self) != {"name", "value"} or set(new_self) != {"name", "value"}
                            or not isinstance(old_self.get("value"), str)
                            or old_self["value"].casefold() != legacy_api
                            or not isinstance(new_self.get("value"), str)
                            or new_self["value"].casefold() != private_api):
                        errors.append("candidate_metrics_self_id_invalid")
                    else:
                        new_self["value"] = old_self["value"]
                    for current in (left, right):
                        rows = env_rows(current)
                        current["containers"][0]["env"] = [
                            rows[name] for name in sorted(rows)
                            if name not in ("AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED")
                        ]
                        current["containers"][0].pop("image", None)
                        current.get("scale", {}).pop("maxReplicas", None)
                    if comparable(left) != comparable(right):
                        errors.append("candidate_configuration_changed")
                    if digest(configuration_intent(baseline["properties"]["configuration"])) != digest(
                        configuration_intent(props.get("configuration", {}))
                    ):
                        errors.append("candidate_ingress_registry_or_secret_references_changed")
                    tags = deepcopy(baseline.get("tags", {}))
                    if tags.get("SecurityControl") == "Ignore":
                        tags.pop("SecurityControl")
                    tags["azd-service-name"] = "api-private-candidate"
                    if tags != after.get("tags"):
                        errors.append("candidate_tags_changed")
        elif kind == "Modify" and isinstance(before, dict) and isinstance(after, dict):
            if packet in ("candidate", "activate", "cutover"):
                if not isinstance(baseline, dict):
                    raise PreviewError("Current application snapshot is missing.")
                current_props = baseline["properties"]
                before_props, after_props = before.get("properties", {}), after.get("properties", {})
                if comparable(before_props.get("template", {})) != comparable(current_props["template"]):
                    errors.append("preview_before_differs_from_current_configuration")
                for resource, props in ((before, before_props), (after, after_props)):
                    if digest(configuration_intent(props.get("configuration", {}))) != digest(
                        configuration_intent(current_props["configuration"])
                    ):
                        errors.append("protected_application_configuration_changed")
                    observed_identity = resource.get("identity", {})
                    current_identity = baseline["identity"]
                    if (observed_identity.get("type") != current_identity.get("type")
                            or {key.casefold() for key in observed_identity.get("userAssignedIdentities", {})}
                            != {key.casefold() for key in current_identity.get("userAssignedIdentities", {})}
                            or props.get("managedEnvironmentId", props.get("environmentId", "")).casefold()
                            != current_props.get("managedEnvironmentId", "").casefold()
                            or canonical_location(resource.get("location"), binding["locationIdentity"])
                            != canonical_location(baseline.get("location"), binding["locationIdentity"])):
                        errors.append("protected_application_resource_changed")
                row["locations"] = {
                    name: {"raw": resource["location"],
                           "canonical": canonical_location(resource["location"], binding["locationIdentity"])}
                    for name, resource in (("before", before), ("after", after))
                }
            paths = set(row["fields"])
            old_tags, new_tags = deepcopy(before.get("tags", {})), deepcopy(after.get("tags", {}))
            old_control = old_tags.pop("SecurityControl", None)
            tag_change_ok = old_control in (None, "Ignore") and old_tags == new_tags and "SecurityControl" not in new_tags
            if paths and paths <= {"tags", "tags.SecurityControl"} and tag_change_ok:
                continue
            template_paths = paths - {"tags", "tags.SecurityControl"} if tag_change_ok else paths
            if rid in app_ids and template_paths and all(path.startswith("properties.template") for path in template_paths):
                left = deepcopy(before.get("properties", {}).get("template", {}))
                right = deepcopy(after.get("properties", {}).get("template", {}))
                service = app_ids[rid]
                allowed = set()
                if service == "api":
                    allowed = {"AI4IA_PRIVATE_RECOVERY_CANDIDATE"}
                    target = "true" if binding["phase"] == "candidate" else "false"
                    if env_rows(right).get("AI4IA_PRIVATE_RECOVERY_CANDIDATE", {}).get("value") != target:
                        errors.append("candidate_mode_drift")
                    if right.get("scale", {}).get("maxReplicas") != (1 if target == "true" else 3):
                        errors.append("api_scale_drift")
                    left.get("scale", {}).pop("maxReplicas", None)
                    right.get("scale", {}).pop("maxReplicas", None)
                elif service == "web" and binding["phase"] == "private":
                    allowed = {"API_BASE_URL", "API_PUBLIC_URL"}
                    before_env, after_env = env_rows(left), env_rows(right)
                    expected_origin = binding.get("apiOrigin")
                    if (not expected_origin or after_env.get("API_BASE_URL", {}).get("value") != expected_origin
                            or (("API_PUBLIC_URL" in before_env or "API_PUBLIC_URL" in after_env)
                                and after_env.get("API_PUBLIC_URL", {}).get("value") != expected_origin)):
                        errors.append("web_voice_destination_drift")
                for template in (left, right):
                    rows = env_rows(template)
                    template["containers"][0]["env"] = [rows[name] for name in sorted(rows) if name not in allowed]
                if comparable(left) != comparable(right):
                    errors.append("protected_app_template_changed")
            elif "/microsoft.insights/metricalerts/" in rid and binding["phase"] == "private" and paths == {"properties.scopes"}:
                left, right = deepcopy(before["properties"]), deepcopy(after["properties"])
                old_scopes, new_scopes = left.pop("scopes", []), right.pop("scopes", [])
                if ([v.casefold() for v in old_scopes] != [legacy_api]
                        or [v.casefold() for v in new_scopes] != [private_api] or digest(left) != digest(right)):
                    errors.append("alert_scope_drift")
            else:
                errors.append("unapproved_modify")
        else:
            errors.append("unreviewable_or_destructive_change")
    require(required <= seen, "ARM preview omitted required private resources.")
    require(len(json.dumps(report).encode()) <= 64 * 1024, "Full scoped field report exceeds its bound.")
    return {"changes": sorted(report, key=lambda row: row["resourceId"]),
            "blockingReasons": sorted(set(errors)),
            "rawChangeDigest": digest(sorted(changes, key=lambda row: row["resourceId"].casefold()))}


def make_token(binding: dict, preview: dict, issued: int) -> str:
    return f"{issued}:{digest({'context': binding, 'preview': preview, 'issued': issued})}"


def verify_token(token: str, binding: dict, preview: dict, now: int) -> None:
    match = BINDING.fullmatch(token)
    if match is None:
        raise PreviewError("A reviewed preview binding is required.")
    issued = int(match[1])
    require(0 <= now - issued <= MAX_AGE, "Reviewed preview is stale or future-dated.")
    require(hmac.compare_digest(token, make_token(binding, preview, issued)), "Preview source, phase, configuration or resource delta changed.")
    require(not preview["blockingReasons"], "Preview contains unapproved resource changes.")


def run(command: list[str], *, limit: int, timeout: int) -> bytes:
    try:
        return PROVENANCE.run_bounded(command, limit=limit, timeout=timeout)
    except PROVENANCE.ProvenanceError as exc:
        raise PreviewError("Bounded command failed; no further resource execution is authorized.") from exc


def cloud_read(args: list[str]) -> Any:
    return json.loads(run(["az", *args, "-o", "json"], limit=8 * 1024 * 1024, timeout=60))


def compile_module(name: str) -> dict:
    require(name in set(MODULES.values()), "Unexpected recovery module.")
    raw = run(["bicep", "build", str(ROOT / "infra/modules" / name), "--stdout"],
              limit=8 * 1024 * 1024, timeout=120)
    return json.loads(raw)


APP_SNAPSHOT = (
    "{id:id,location:location,tags:tags,identity:identity,etag:etag,systemData:systemData,"
    "properties:{managedEnvironmentId:properties.managedEnvironmentId,"
    "latestRevisionName:properties.latestRevisionName,latestReadyRevisionName:properties.latestReadyRevisionName,"
    "configuration:merge(properties.configuration,{secrets:properties.configuration.secrets[]."
    "{name:name,keyVaultUrl:keyVaultUrl,identity:identity}}),template:properties.template}}"
)


def app_snapshot(env: dict[str, str], name: str, read=cloud_read) -> dict:
    environment = env["AZURE_ENV_NAME"]
    require(name in (api_name(environment, "legacy"), api_name(environment, "private"), f"ca-web-{environment}"),
            "Application snapshot is outside the recovery scope.")
    group = f"rg-{env.get('AI4IA_WORKLOAD') or 'ai4ia'}-{env['AZURE_ENV_NAME']}"
    args = ["containerapp", "show", "--subscription", env["AZURE_SUBSCRIPTION_ID"],
            "-g", group, "-n", name, "--query", APP_SNAPSHOT]
    app = read(args)
    require(isinstance(app, dict) and app.get("id", "").casefold() == (
        f"/subscriptions/{env['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{group}"
        f"/providers/Microsoft.App/containerApps/{name}"
    ).casefold(), "Exact current application metadata is unavailable.")
    props = app["properties"]
    revision = props.get("latestReadyRevisionName")
    require(bool(revision) and revision == props.get("latestRevisionName"),
            "Current application has an unsettled template.")
    detail = read(["containerapp", "revision", "show", "--subscription", env["AZURE_SUBSCRIPTION_ID"],
                   "-g", group, "-n", name, "--revision", revision, "--query", "properties.template"])
    require(comparable(detail) == comparable(props["template"]) and read(args) == app,
            "Current application changed during snapshot.")
    service = "web" if name == f"ca-web-{environment}" else "api"
    containers = props["template"].get("containers", [])
    require(len(containers) == 1 and isinstance(containers[0].get("image"), str)
            and re.fullmatch(r"[a-z0-9]+\.azurecr\.io/ai4ia/" + service + "-" + re.escape(environment)
                             + r"@sha256:[0-9a-f]{64}", containers[0]["image"]) is not None,
            "Current application image is not an exact retained service digest.")
    return app


def compiled_context(env: dict[str, str], read=cloud_read) -> tuple[dict, dict, dict, dict | None, dict]:
    require(env.get("GITHUB_REF") == "refs/heads/main"
            and re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", "")) is not None,
            "Scoped recovery requires a known trusted main commit.")
    phase = from_environment(env)
    packet = env.get("AI4IA_PRIVATE_STEP") or {"candidate": "foundation", "activate": "activate", "private": "cutover"}.get(phase)
    if not isinstance(packet, str):
        raise PreviewError("Recovery packet does not match the selected phase.")
    require(packet in MODULES and ((phase == "candidate" and packet in ("foundation", "candidate"))
                                  or (phase == "activate" and packet == "activate")
                                  or (phase == "private" and packet == "cutover")),
            "Recovery packet does not match the selected phase.")
    environment, subscription = env["AZURE_ENV_NAME"], env["AZURE_SUBSCRIPTION_ID"]
    workload = env.get("AI4IA_WORKLOAD") or "ai4ia"
    api_name(environment, phase)
    require(re.fullmatch(r"[0-9a-fA-F-]{36}", subscription) is not None
            and re.fullmatch(r"[a-z][a-z0-9-]{2,19}", workload) is not None,
            "Scoped recovery subscription/workload is invalid.")
    group = f"rg-{workload}-{environment}"
    root = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/"
    api_template = compile_module("api.bicep")
    api_meta, api_values = read_record(read, api_template, subscription, group, "api")
    require(api_values.get("environmentName") == environment and api_values.get("appEnvironment") == "prod"
            and api_values.get("authProvider") == "entra" and api_values.get("entraTenantId") == env.get("AZURE_TENANT_ID"),
            "Existing API inputs do not match the approved production boundary.")
    old_api = app_snapshot(env, api_name(environment, "legacy"), read)
    old_rows = env_rows(old_api["properties"]["template"])
    require(old_rows.get("AI4IA_COSMOS_ENDPOINT", {}).get("value") == env.get("AI4IA_PRIVATE_COSMOS_ENDPOINT"),
            "Canonical source account changed.")
    location = location_evidence(read([
        "rest", "--method", "get", "--subscription", subscription,
        "--url", f"https://management.azure.com/subscriptions/{subscription}/locations?api-version=2022-12-01",
        "--query", "{rows:value[].{id:id,name:name,displayName:displayName,type:type},nextLink:nextLink}",
    ]), subscription, env.get("AZURE_LOCATION", ""))
    canonical = canonical_location(api_values.get("location"), location)
    require(canonical_location(old_api.get("location"), location) == canonical,
            "Current API location differs from recorded inputs.")
    template = api_template if packet in ("candidate", "activate") else compile_module(MODULES[packet])
    baseline = None
    proof = None
    quota = None
    recorded_web_location = None
    metadata = api_meta
    if packet == "foundation":
        log_id = root + f"Microsoft.OperationalInsights/workspaces/log-{workload}-{environment}"
        require(api_values["logAnalyticsWorkspaceId"].casefold() == log_id.casefold(),
                "Existing Log Analytics reference is outside the approved scope.")
        cosmos = re.fullmatch(r"https://([a-z0-9-]+)\.documents\.azure\.com(?::443)?/?",
                              env.get("AI4IA_PRIVATE_COSMOS_ENDPOINT", ""))
        if cosmos is None:
            raise PreviewError("Canonical Cosmos identity is unknown.")
        values = {
            "location": canonical, "tags": deepcopy(api_values["tags"]),
            "workload": workload, "environmentName": environment,
            "logAnalyticsName": f"log-{workload}-{environment}",
            "cosmosAccountId": root + "Microsoft.DocumentDB/databaseAccounts/" + cosmos[1],
            "vnetAddressPrefix": env["AI4IA_API_PRIVATE_VNET_PREFIX"],
            "apiSubnetPrefix": env["AI4IA_API_PRIVATE_SUBNET_PREFIX"],
            "privateEndpointSubnetPrefix": env["AI4IA_COSMOS_PRIVATE_ENDPOINT_SUBNET_PREFIX"],
        }
        if "SecurityControl" in values["tags"]:
            require(values["tags"].pop("SecurityControl") == "Ignore", "Unreviewed security tag.")
        params = {key: {"value": value} for key, value in values.items()}
    elif packet in ("candidate", "activate"):
        receipt = json.loads(env.get("AI4IA_PRIVATE_IMAGE_RECEIPT", "null"))
        require(isinstance(receipt, dict), "The reviewed prepared API image receipt is required.")
        image = receipt.get("image", "")
        require(isinstance(image, str) and re.fullmatch(r"[a-z0-9]+\.azurecr\.io/ai4ia/api-[a-z0-9-]+@sha256:[0-9a-f]{64}", image) is not None,
                "Prepared API image reference is invalid.")
        baseline = old_api if packet == "candidate" else app_snapshot(env, api_name(environment, "private"), read)
        if packet == "activate":
            require(baseline["properties"]["template"]["containers"][0]["image"] == image,
                    "Activation cannot substitute or rebuild the candidate image.")
        overrides = api_overrides(
            phase=packet, subscription=subscription, group=group, workload=workload, environment=environment,
            source=baseline, image=image,
        )
        require(canonical_location(baseline.get("location"), location) == canonical,
                "Current API is in a different Region.")
        overrides["location"] = canonical
        registry = image.split(".", 1)[0]
        run(["az", "acr", "login", "--subscription", subscription, "--name", registry], limit=4096, timeout=60)
        proof = PROVENANCE.consume_private_api(receipt, env)
        params = record_parameters(template, metadata, api_values, overrides)
        network = read(["containerapp", "env", "show", "--subscription", subscription, "-g", group,
                        "-n", f"cae-{workload}-{environment}-api-private",
                        "--query", "{id:id,properties:{provisioningState:properties.provisioningState,"
                        "vnetConfiguration:properties.vnetConfiguration,workloadProfiles:properties.workloadProfiles}}"])
        require(network.get("id", "").casefold() == overrides["containerEnvId"].casefold()
                and network["properties"].get("provisioningState") == "Succeeded"
                and network["properties"].get("vnetConfiguration", {}).get("infrastructureSubnetId", "").casefold()
                == (root + f"Microsoft.Network/virtualNetworks/vnet-{workload}-{environment}-api-private/subnets/api").casefold(),
                "The exact private foundation is not ready.")
        quota = consumption_headroom(read([
            "containerapp", "env", "list-usages", "--subscription", subscription, "-g", group,
            "-n", f"cae-{workload}-{environment}-api-private",
        ]), 0.5 if packet == "candidate" else 1.5)
    else:
        metadata, values = read_record(read, template, subscription, group, "web")
        recorded_web_location = values.get("location")
        baseline = app_snapshot(env, f"ca-web-{environment}", read)
        require(canonical_location(values.get("location"), location) == canonical
                and canonical_location(baseline.get("location"), location) == canonical,
                "Current web location differs from the approved Region.")
        origin = env.get("AI4IA_PRIVATE_API_ORIGIN", "")
        require(re.fullmatch(r"https://ca-api-private-" + re.escape(environment)
                             + r"\.[a-z0-9.-]+\.azurecontainerapps\.io", origin) is not None,
                "Verified private API origin is unavailable.")
        params = record_parameters(template, metadata, values, {
            "recoveryCutover": True, "apiBaseUrl": origin,
            "tags": baseline["tags"],
            "location": canonical,
            "webImage": baseline["properties"]["template"]["containers"][0]["image"],
        })
    expected = {name: {"value": api_values[name]} for name in ("appEnvironment", "entraTenantId", "entraAudience")}
    expected["apiAuthProvider"] = {"value": api_values["authProvider"]}
    expected.update({
        "location": {"value": canonical},
        "apiPrivateVnetPrefix": {"value": env["AI4IA_API_PRIVATE_VNET_PREFIX"]},
        "apiPrivateSubnetPrefix": {"value": env["AI4IA_API_PRIVATE_SUBNET_PREFIX"]},
        "cosmosPrivateEndpointSubnetPrefix": {"value": env["AI4IA_COSMOS_PRIVATE_ENDPOINT_SUBNET_PREFIX"]},
        "apiPrivateInitialImage": {"value": params.get("apiImage", {}).get("value")},
    })
    bound = context(env, template, expected)
    source_app = baseline or old_api
    source_template = source_app["properties"]["template"]
    bound.update(parameters=digest(params), packet=packet, recordedInputs=digest(metadata),
                 currentConfiguration=digest(baseline or old_api), imageProof=digest(proof) if proof else None,
                 privateCoreQuota=quota, sourceApplication={
                     "id": source_app["id"], "revision": source_app["properties"]["latestReadyRevisionName"],
                     "image": source_template["containers"][0]["image"],
                     "minReplicas": source_template.get("scale", {}).get("minReplicas"),
                     "maxReplicas": source_template.get("scale", {}).get("maxReplicas"),
                     "evidence": "configuration_reference_not_runtime_acceptance",
                 })
    bound.update(locationIdentity=location, locationObservations={
        "legacyApi": {"raw": old_api["location"], "canonical": canonical},
        "recordedApiParameter": {"raw": api_values["location"], "canonical": canonical},
        "sourceApplication": {"raw": source_app["location"], "canonical": canonical},
        "packetParameter": {"raw": params["location"]["value"], "canonical": canonical},
    })
    if packet == "cutover":
        bound["locationObservations"]["recordedWebParameter"] = {"raw": recorded_web_location, "canonical": canonical}
    return template, params, bound, baseline, expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify-binding", "apply", "cleanup"))
    args = parser.parse_args()
    env = dict(os.environ)
    packet_dir = None
    retain_packet = False
    try:
        if args.command == "cleanup":
            directory = Path(env["AI4IA_PRIVATE_PACKET_DIR"])
            require(directory.is_absolute() and not directory.is_symlink()
                    and directory.resolve().is_relative_to(Path(env["RUNNER_TEMP"]).resolve())
                    and directory.name.startswith(f"ai4ia-private-packet-{env['GITHUB_RUN_ID']}-"),
                    "Cleanup target is not this run's private packet.")
            (directory / "parameters.json").unlink(missing_ok=True)
            (directory / "template.json").unlink(missing_ok=True)
            directory.rmdir()
            return 0
        if args.command == "prepare":
            with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
                file.write("authorized=false\n")
        if args.command == "verify-binding" and (env.get("AI4IA_API_NETWORK_PHASE") or "legacy") == "legacy":
            return 0
        kind = env.get("AI4IA_RELEASE_KIND")
        if args.command == "verify-binding" and kind == "ordinary":
            require(env.get("GITHUB_REF") == "refs/heads/main", "Ordinary release requires trusted main.")
            current = ADMISSION.admission(env)
            require(current["AI4IA_PRIVATE_MIGRATION_PENDING"] == "false", "Ordinary release is not yet available.")
            return 0
        if args.command == "verify-binding":
            raise PreviewError("A scoped migration packet cannot authorize root azd provisioning; use exact packet apply.")
        template, params, bound, baseline, expected = compiled_context(env)
        if args.command == "apply":
            proof = json.loads(Path(env["AI4IA_PRIVATE_PREVIEW_PROOF"]).read_text())
            require(proof["context"] == bound, "Deployment inputs changed after reviewed preview.")
            verify_token(env.get("AI4IA_PRIVATE_PREVIEW_BINDING", ""), bound, proof["preview"], int(time.time()))
            if args.command == "apply":
                require(kind == "deploy" and env.get("AI4IA_RELEASE_PROVISION") == "true",
                        "Only the explicit reviewed scoped deployment may apply.")
                packet_dir = Path(env["AI4IA_PRIVATE_PACKET_DIR"]).resolve()
                require(packet_dir.is_relative_to(Path(env["RUNNER_TEMP"]).resolve()),
                        "Private packet is outside the current runner.")
                saved_template = json.loads((packet_dir / "template.json").read_text())
                saved_params = json.loads((packet_dir / "parameters.json").read_text())["parameters"]
                require(digest(saved_template) == bound["template"] and digest(saved_params) == bound["parameters"],
                        "Frozen packet changed after preview.")
                ADMISSION.admission(env)
                main = json.loads(run(["gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/git/ref/heads/main"],
                                      limit=32 * 1024, timeout=30))
                require(main.get("object", {}).get("sha") == env["GITHUB_SHA"], "Main changed before scoped apply.")
                name = f"ai4ia-private-{bound['packet']}-{env['AI4IA_PRIVATE_PREVIEW_BINDING'].split(':')[1][:16]}"
                existing = subprocess.run([
                    "az", "deployment", "group", "show", "--subscription", bound["subscription"],
                    "-g", bound["resourceGroup"], "-n", name, "--query", "properties.provisioningState", "-o", "json",
                ], capture_output=True, timeout=60, check=False)
                require(existing.returncode != 0 and len(existing.stderr) <= 16 * 1024
                        and re.search(rb"(?m)^\s*(?:ERROR:\s*)?\(DeploymentNotFound\)", existing.stderr) is not None,
                        "Deployment claim already exists or its absence is unproven; never replay.")
                verify_token(env["AI4IA_PRIVATE_PREVIEW_BINDING"], bound, proof["preview"], int(time.time()))
                run([
                    "az", "deployment", "group", "create", "--subscription", bound["subscription"],
                    "-g", bound["resourceGroup"], "-n", name, "--mode", "Incremental",
                    "--template-file", str(packet_dir / "template.json"),
                    "--parameters", "@" + str(packet_dir / "parameters.json"), "-o", "none",
                ], limit=4096, timeout=1800)
                (Path(env["RUNNER_TEMP"]) / "ai4ia-private-preview" / "execution.json").write_text(
                    json.dumps({"context": bound, "deploymentName": name, "resourceDeployment": "Succeeded",
                                "runtimeAcceptance": "not_yet_observed", "modelCalls": 0}, indent=2), encoding="utf-8",
                )
                print("Exact scoped packet applied once. Runtime acceptance remains a separate gate.")
            return 0
        require(kind in ("preview", "deploy"), "Explicit preview/deploy operation required.")
        output = Path(env["RUNNER_TEMP"]) / "ai4ia-private-preview"
        output.mkdir(exist_ok=True)
        packet_dir = Path(tempfile.mkdtemp(prefix=f"ai4ia-private-packet-{env['GITHUB_RUN_ID']}-", dir=env["RUNNER_TEMP"]))
        descriptor = os.open(packet_dir / "parameters.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        parameter_path = str(packet_dir / "parameters.json")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump({"parameters": params}, file)
            template_path = packet_dir / "template.json"
            template_path.write_text(json.dumps(template), encoding="utf-8")
            raw = run([
                "az", "deployment", "group", "what-if", "--subscription", bound["subscription"],
                "-g", bound["resourceGroup"], "--name", "ai4ia-private-preview",
                "--template-file", str(template_path), "--parameters", "@" + parameter_path,
                "--validation-level", "Provider", "--no-pretty-print", "--no-prompt", "true", "-o", "json",
            ], limit=8 * 1024 * 1024, timeout=300)
        except BaseException:
            os.unlink(parameter_path)
            raise
        if kind != "deploy":
            os.unlink(parameter_path)
        payload = json.loads(raw)
        try:
            preview = scope_preview(payload, bound, expected, baseline)
        except (PreviewError, KeyError, TypeError, ValueError, AttributeError):
            (output / "report.json").write_text(json.dumps(rejected_report(payload, bound), indent=2), encoding="utf-8")
            if Path(parameter_path).exists():
                os.unlink(parameter_path)
            raise
        issued = int(time.time())
        token = make_token(bound, preview, issued)
        report = {"context": bound, "preview": preview, "issuedAt": issued,
                  "binding": token if not preview["blockingReasons"] else None,
                  "executionAuthorized": False,
                  "executionBlockers": json.loads(env.get("AI4IA_PRIVATE_EXECUTION_BLOCKERS", "[]"))}
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        require(not preview["blockingReasons"], "Preview contains unapproved resource changes; review the sanitized report.")
        if kind == "deploy":
            verify_token(env.get("AI4IA_PRIVATE_PREVIEW_BINDING", ""), bound, preview, issued)
            proof = output / "proof.json"
            proof.write_text(json.dumps({"context": bound, "preview": preview}), encoding="utf-8")
            with Path(env["GITHUB_ENV"]).open("a", encoding="utf-8") as file:
                file.write(f"AI4IA_PRIVATE_PREVIEW_PROOF={proof}\n")
                file.write(f"AI4IA_PRIVATE_PACKET_DIR={packet_dir}\n")
            retain_packet = True
        with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
            file.write(f"authorized={'true' if kind == 'deploy' else 'false'}\n")
        print("Private phase preview retained; no resources changed. Execution requires its separate guarded path.")
        return 0
    except (PreviewError, PacketError, OSError, KeyError, TypeError, ValueError, AttributeError, subprocess.SubprocessError) as exc:
        print(str(exc) if isinstance(exc, PreviewError) else "Private preview failed with incomplete evidence.", file=sys.stderr)
        return 1
    finally:
        if packet_dir is not None and not retain_packet:
            (packet_dir / "parameters.json").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
