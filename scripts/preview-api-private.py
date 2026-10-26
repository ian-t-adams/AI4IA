#!/usr/bin/env python3
"""Bounded full-phase ARM preview using the existing trusted runner configuration."""
from __future__ import annotations

import argparse
from collections.abc import Callable
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
from urllib.parse import urlencode

from _api_private import api_name, from_environment
from _private_recovery_packets import (
    MODULES, PacketError, api_overrides, candidate_template_intent, canonical_location, configuration_intent, consumption_headroom,
    location_evidence, read_record, record_parameters, activation_patch_template, activation_patch_body,
    activation_suffix, validate_activation_patch_body, activation_template_projection, SECRET_NAMES,
)
from _private_metadata import (
    RAW_METADATA_QUERY, MetadataError, project_proven_metadata, source_descriptor,
    validate_live_metadata, validate_native_metadata, env_metadata, secret_metadata,
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


ABSENT = object()


def at_path(value: dict, path: str) -> Any:
    current: Any = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return ABSENT
        current = current[key]
    return current


def remove_path(value: dict, path: str) -> None:
    keys = path.split(".")
    current = value
    for key in keys[:-1]:
        current = current[key]
    del current[keys[-1]]


def nsg_rules_state(resource: Any) -> str:
    if not isinstance(resource, dict):
        return "resource_invalid"
    if "properties" not in resource:
        return "properties_absent"
    props = resource["properties"]
    if not isinstance(props, dict):
        return "properties_null" if props is None else "properties_invalid"
    if "securityRules" not in props:
        return "rules_absent"
    rules = props["securityRules"]
    if rules is None:
        return "rules_null"
    if not isinstance(rules, list):
        return "rules_invalid"
    return "rules_empty" if not rules else "rules_nonempty"


def read_empty_nsg(rid: str, binding: dict, read: Callable[[list[str]], Any]) -> dict:
    prefix = f"/subscriptions/{binding['subscription']}/resourceGroups/{binding['resourceGroup']}/providers/"
    suffix = f"{binding['workload']}-{binding['environment']}-api-private"
    names = {
        (prefix + f"Microsoft.Network/networkSecurityGroups/vnet-{suffix}-{name}-nsg-{binding['location']}").casefold(): name
        for name in ("api", "cosmos")
    }
    require(rid in names, "NSG read is outside the exact foundation.")
    snapshot = read([
        "rest", "--method", "get", "--subscription", binding["subscription"],
        "--url", f"https://management.azure.com{rid}?api-version=2024-05-01",
        "--query", "{id:id,etag:etag,location:location,state:properties.provisioningState,"
        "rulesPresent:contains(keys(properties), 'securityRules'),rules:properties.securityRules,"
        "subnets:properties.subnets[].id}",
    ])
    require(isinstance(snapshot, dict) and isinstance(snapshot.get("id"), str)
            and snapshot["id"].casefold() == rid and snapshot.get("state") == "Succeeded"
            and snapshot.get("rulesPresent") is True and snapshot.get("rules") == []
            and isinstance(snapshot.get("etag"), str) and 0 < len(snapshot["etag"]) <= 128,
            "Live NSG rules are nonempty or unproven.")
    require(canonical_location(snapshot.get("location"), binding["locationIdentity"]) == binding["location"],
            "Live NSG Region changed.")
    expected_subnet = (prefix + f"Microsoft.Network/virtualNetworks/vnet-{suffix}/subnets/{names[rid]}").casefold()
    subnets = snapshot.get("subnets")
    require(isinstance(subnets, list) and len(subnets) == 1 and isinstance(subnets[0], str)
            and subnets[0].casefold() == expected_subnet, "Live NSG association changed.")
    return {"resourceId": rid, "digest": digest(snapshot), "observedAt": int(time.time())}


def verify_nsg_reads(preview: dict, binding: dict, read: Callable[[list[str]], Any]) -> list[dict]:
    observations = []
    for row in preview.get("nsgComparisons", []):
        if "liveReadDigest" in row:
            current = read_empty_nsg(row["resourceId"], binding, read)
            require(current["digest"] == row["liveReadDigest"], "Live NSG changed after reviewed preview.")
            observations.append(current)
    return observations


def legacy_inventory_proof(value: str, binding: dict) -> dict:
    proof = json.loads(value)
    expected_id = (f"/subscriptions/{binding['subscription']}/resourceGroups/{binding['resourceGroup']}"
                   f"/providers/Microsoft.App/containerApps/{api_name(binding['environment'], 'legacy')}")
    require(isinstance(proof, dict) and set(proof) == {
        "appId", "pageCount", "rowCount", "digest", "activeRevisions", "replicaReadCount",
    }, "Complete legacy inventory proof is unavailable.")
    require(isinstance(proof["appId"], str) and proof["appId"].casefold() == expected_id.casefold()
            and type(proof["pageCount"]) is int and 0 < proof["pageCount"] < 32
            and type(proof["rowCount"]) is int and 0 < proof["rowCount"] < 640
            and type(proof["replicaReadCount"]) is int and 0 <= proof["replicaReadCount"] <= 4
            and isinstance(proof["digest"], str) and re.fullmatch(r"[0-9a-f]{64}", proof["digest"]) is not None
            and isinstance(proof["activeRevisions"], list) and len(proof["activeRevisions"]) <= 1
            and all(isinstance(name, str) and name.startswith(api_name(binding["environment"], "legacy") + "--")
                    for name in proof["activeRevisions"]), "Complete legacy inventory proof is invalid.")
    return proof


def candidate_metadata_gate(binding: dict, descriptor: dict, read: Callable[[list[str]], Any]) -> dict:
    name = api_name(binding["environment"], "private")
    rid = (f"/subscriptions/{binding['subscription']}/resourceGroups/{binding['resourceGroup']}"
           f"/providers/Microsoft.App/containerApps/{name}")
    snapshot = read([
        "rest", "--method", "get", "--subscription", binding["subscription"],
        "--url", f"https://management.azure.com{rid}?api-version=2024-10-02-preview",
        "--query", RAW_METADATA_QUERY,
    ])
    flags = snapshot.get("flags") if isinstance(snapshot, dict) else None
    require(isinstance(flags, list) and len(flags) == 2
            and all(isinstance(row, dict) and set(row) == {"name", "value"}
                    and isinstance(row["name"], str) and row["value"] == "true" for row in flags)
            and {row["name"] for row in flags} == {"AI4IA_PRIVATE_RECOVERY_CANDIDATE", "AI4IA_PRIVATE_COSMOS_REQUIRED"},
            "Created candidate is not worker-suppressed with private readiness.")
    metadata = validate_live_metadata(snapshot, descriptor, rid)
    return {"status": "verified", "sourceDigest": digest(descriptor), "liveDigest": digest(metadata),
            "observedAt": int(time.time())}


def contain_candidate(binding: dict, read: Callable[[list[str]], Any], execute: Callable) -> dict:
    """An authorized failed-candidate stop; never touch the retained legacy app."""
    name = api_name(binding["environment"], "private")
    scope = ["--subscription", binding["subscription"], "-g", binding["resourceGroup"], "-n", name]
    query = ["containerapp", "revision", "list", *scope, "--all", "--query", "[].{name:name,active:properties.active}"]

    def inventory() -> list[dict]:
        rows = read(query)
        require(isinstance(rows, list) and 0 < len(rows) <= 32
                and all(isinstance(row, dict) and isinstance(row.get("name"), str)
                        and re.fullmatch(re.escape(name) + r"--[a-z0-9-]{1,80}", row["name"]) is not None
                        and type(row.get("active")) is bool for row in rows),
                "Candidate containment inventory is unknown.")
        require(len({row["name"] for row in rows}) == len(rows), "Candidate revision inventory is duplicated.")
        return rows

    active = [row["name"] for row in inventory() if row["active"]]
    for revision in active:
        execute(["az", "containerapp", "revision", "deactivate", *scope, "--revision", revision, "-o", "none"],
                limit=4096, timeout=120)
    require(not any(row["active"] for row in inventory()), "Candidate containment did not confirm inactivity.")
    return {"status": "inactive", "deactivatedRevisions": len(active), "legacyTouched": False}


def foundation_echo(change: dict, binding: dict, resource_kind: str) -> bool:
    """Only the reviewed source-identical response echoes, never general defaults."""
    rid = change["resourceId"].casefold()
    before, after = change["before"], change["after"]
    types = {
        "vnet": "Microsoft.Network/virtualNetworks",
        "endpoint": "Microsoft.Network/privateEndpoints",
        "link": "Microsoft.Network/privateDnsZones/virtualNetworkLinks",
        "zoneGroup": "Microsoft.Network/privateEndpoints/privateDnsZoneGroups",
        "environment": "Microsoft.App/managedEnvironments",
    }
    for resource in (before, after):
        if (not isinstance(resource.get("id"), str) or resource["id"].casefold() != rid
                or resource.get("type") != types[resource_kind]):
            return False
        if resource_kind == "zoneGroup":
            if resource.get("location") is not None:
                return False
        elif resource_kind == "link":
            if resource.get("location") != "global":
                return False
        elif canonical_location(resource.get("location"), binding["locationIdentity"]) != binding["location"]:
            return False
    allowed = {
        "vnet": {"properties.privateEndpointVNetPolicies": "Disabled"},
        "endpoint": {"properties.isIPv6EnabledPrivateEndpoint": False},
        "link": {"properties.resolutionPolicy": "Default"},
        "environment": {
            "properties.peerAuthentication": {"mtls": {"enabled": False}},
            "properties.peerTrafficConfiguration": {"encryption": {"enabled": False}},
            "properties.appLogsConfiguration.logAnalyticsConfiguration.dynamicJsonColumns": False,
        },
        "zoneGroup": {},
    }[resource_kind]
    left, right = deepcopy(before), deepcopy(after)
    deltas = change["delta"]
    paths = [delta["path"] for delta in deltas]
    if len(paths) != len(set(paths)):
        return False
    customer_path = "properties.appLogsConfiguration.logAnalyticsConfiguration.customerId"
    for delta in deltas:
        path = delta["path"]
        old, new = at_path(before, path), at_path(after, path)
        if resource_kind == "zoneGroup":
            if path != "properties.privateDnsZoneConfigs" or delta.get("propertyChangeType") != "Array":
                return False
            if not dns_config_echo(old, new, delta, rid, binding):
                return False
            remove_path(left, path)
            remove_path(right, path)
            continue
        if path == customer_path and resource_kind == "environment":
            customer = binding.get("logAnalyticsCustomerId")
            log_id = (f"/subscriptions/{binding['subscription']}/resourceGroups/{binding['resourceGroup']}"
                      f"/providers/Microsoft.OperationalInsights/workspaces/log-{binding['workload']}-{binding['environment']}")
            expression = f"[reference('{log_id}', '2023-09-01').customerId]"
            if (not isinstance(customer, str) or re.fullmatch(r"[0-9a-fA-F-]{36}", customer) is None
                    or old != customer or new not in (customer, expression)
                    or delta.get("propertyChangeType") != "Modify"
                    or delta.get("before") != old or delta.get("after") != new):
                return False
            remove_path(left, path)
            remove_path(right, path)
            continue
        if (path not in allowed or old is ABSENT or new is not ABSENT
                or digest(old) != digest(allowed[path]) or delta.get("propertyChangeType") != "Delete"
                or digest(delta.get("before")) != digest(old) or delta.get("after") is not None
                or delta.get("children") not in (None, [])):
            return False
        remove_path(left, path)
    return digest(left) == digest(right)


def dns_config_echo(before: Any, after: Any, delta: dict, rid: str, binding: dict) -> bool:
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != 1 or len(after) != 1:
        return False
    old, new = before[0], after[0]
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    zone_id = (f"/subscriptions/{binding['subscription']}/resourceGroups/{binding['resourceGroup']}"
               "/providers/Microsoft.Network/privateDnsZones/privatelink.documents.azure.com")
    expected = {"name": "cosmos-sql", "properties": {"privateDnsZoneId": zone_id}}
    if new != expected or not isinstance(old.get("properties"), dict):
        return False
    reduced = deepcopy(old)
    metadata = {
        "id": rid + "/privatednszoneconfigs/cosmos-sql",
        "type": "microsoft.network/privateendpoints/privatednszonegroups/privatednszoneconfigs",
        "resourceGroup": binding["resourceGroup"].casefold(),
    }
    for key, expected_value in metadata.items():
        if key in reduced:
            value = reduced.pop(key)
            if not isinstance(value, str) or value.casefold() != expected_value:
                return False
    if "etag" in reduced:
        etag = reduced.pop("etag")
        if not isinstance(etag, str) or re.fullmatch(r'W/"[0-9a-fA-F-]{36}"', etag) is None:
            return False
    props = reduced["properties"]
    if "provisioningState" in props and props.pop("provisioningState") != "Succeeded":
        return False
    if "recordSets" in props:
        records = props.pop("recordSets")
        if not isinstance(records, list) or len(records) > 32 or not all(isinstance(row, dict) for row in records):
            return False
    if reduced != expected:
        return False
    children = delta.get("children")
    if (not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], dict)
            or children[0].get("path") != "0"
            or children[0].get("propertyChangeType") != "Modify"):
        return False
    leaves = children[0].get("children")
    allowed = {"id", "etag", "type", "properties.provisioningState", "properties.recordSets"}
    if not isinstance(leaves, list) or not leaves or len(leaves) > len(allowed):
        return False
    seen = set()
    for leaf in leaves:
        if not isinstance(leaf, dict):
            return False
        path = leaf.get("path")
        if (not isinstance(path, str) or path not in allowed or path in seen or at_path(old, path) is ABSENT
                or leaf.get("propertyChangeType") != "Delete" or leaf.get("after") is not None
                or leaf.get("children") not in (None, []) or at_path(new, path) is not ABSENT
                or digest(leaf.get("before")) != digest(at_path(old, path))):
            return False
        seen.add(path)
    return True


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


def scope_preview(
    raw: dict, binding: dict, params: dict, baseline: dict | None = None, *,
    read: Callable[[list[str]], Any] | None = None, observations: list[dict] | None = None,
) -> dict:
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
    nsgs = {
        name: prefix + f"microsoft.network/networksecuritygroups/vnet-{suffix}-{name}-nsg-{binding['location']}"
        for name in ("api", "cosmos")
    }
    creates = {vnet, private_api, private_env, zone, zone + f"/virtualnetworklinks/{suffix}",
               endpoint, endpoint + "/privatednszonegroups/default", *nsgs.values()}
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
    nsg_evidence = []
    descriptor = params.get("sourceSecretMetadata")
    if binding.get("secretMetadataDigest") is not None:
        require(isinstance(descriptor, dict) and digest(descriptor) == binding["secretMetadataDigest"],
                "Source-bound secret metadata proof is missing or changed.")
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
        if rid in nsgs.values() and (permitted is None or rid in permitted):
            after_nsg = change.get("after")
            state = nsg_rules_state(after_nsg)
            default_only = state == "rules_empty"
            evidence = {"resourceId": rid, "customRulesEmpty": default_only, "snapshotState": state}
            if kind == "NoChange" and isinstance(change.get("before"), dict):
                if nsg_rules_state(change["before"]) not in ("properties_absent", "rules_absent", "rules_empty"):
                    errors.append("nsg_before_rules_nonempty_or_unknown")
            if kind == "NoChange" and state in ("properties_absent", "rules_absent") and read is not None:
                before_nsg = change.get("before")
                require(nsg_rules_state(before_nsg) in ("properties_absent", "rules_absent", "rules_empty"),
                        "Before NSG rules are nonempty or unproven.")
                for snapshot in (before_nsg, after_nsg):
                    require(isinstance(snapshot, dict) and isinstance(snapshot.get("id"), str)
                            and snapshot["id"].casefold() == rid
                            and snapshot.get("type") == "Microsoft.Network/networkSecurityGroups",
                            "NSG snapshot identity is incomplete.")
                proof = read_empty_nsg(rid, binding, read)
                default_only = True
                evidence.update(customRulesEmpty=True, liveReadDigest=proof["digest"])
                if observations is not None:
                    observations.append(proof)
            nsg_evidence.append(evidence)
            if not default_only:
                errors.append("nsg_rules_not_empty_or_unknown")
            if kind == "Modify":
                errors.append("unapproved_nsg_modify")
        if packet in ("candidate", "activate", "cutover") and rid in required:
            after_resource = change.get("after")
            if isinstance(after_resource, dict) and "configuration" in after_resource.get("properties", {}):
                configuration_intent(after_resource["properties"]["configuration"])
            if descriptor is not None:
                require(isinstance(after_resource, dict), "Native secret metadata structure is unavailable.")
                validate_native_metadata(after_resource, descriptor)
        if kind in ("Ignore", "NoChange"):
            require(not (kind == "Ignore" and rid in required), "Required resource coverage was ignored.")
            continue
        if not rid.startswith(prefix) and rid != prefix.split("/providers/")[0]:
            errors.append("change_outside_workload")
            continue
        before, after = change.get("before"), change.get("after")
        row = {"resourceId": rid, "changeType": kind, "fields": []}
        if descriptor is not None and rid in required:
            row["secretMetadataProof"] = "source-bound; native masked"
            row["secretMetadataDigest"] = digest(descriptor)
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
                require(isinstance(props, dict), "Created VNet properties are incomplete.")
                if (props.get("virtualNetworkPeerings") not in (None, [])
                        or (props.get("enableDdosProtection") is not None and props.get("enableDdosProtection") is not False)
                        or props.get("ddosProtectionPlan") is not None):
                    errors.append("unapproved_network_addition")
                expected = [params["apiPrivateVnetPrefix"]["value"]]
                address_space = props.get("addressSpace")
                address_matches = isinstance(address_space, dict) and address_space.get("addressPrefixes") == expected
                if not address_matches:
                    errors.append("vnet_address_drift")
                subnets = props.get("subnets")
                expected_subnets = {"api": params["apiPrivateSubnetPrefix"]["value"],
                                    "cosmos": params["cosmosPrivateEndpointSubnetPrefix"]["value"]}
                rows_valid = isinstance(subnets, list) and all(
                    isinstance(subnet, dict) and isinstance(subnet.get("name"), str)
                    and isinstance(subnet.get("properties"), dict) for subnet in subnets
                )
                count_matches = isinstance(subnets, list) and len(subnets) == 2
                names_match = rows_valid and count_matches and {subnet["name"] for subnet in subnets} == set(expected_subnets)
                evidence = {
                    "addressSpaceMatches": address_matches,
                    "subnetCount": min(len(subnets), 32) if isinstance(subnets, list) else None,
                    "subnetCountCapped": isinstance(subnets, list) and len(subnets) > 32,
                    "subnetRowsValid": rows_valid,
                    "subnetNamesMatch": names_match if rows_valid and count_matches else None,
                    "apiPrefixMatches": None, "endpointPrefixMatches": None,
                    "apiDelegationMatches": None, "endpointPolicyMatches": None,
                    "apiNsgMatches": None, "endpointNsgMatches": None,
                }
                row["networkComparisons"] = evidence
                if not rows_valid:
                    errors.append("subnet_inventory_invalid")
                elif not count_matches:
                    errors.append("subnet_count_drift")
                elif not names_match:
                    errors.append("subnet_names_drift")
                if rows_valid:
                    if any(subnet.get("properties", {}).get(key) not in (None, [])
                           for subnet in subnets for key in
                           ("natGateway", "routeTable", "serviceEndpoints", "serviceEndpointPolicies")):
                        errors.append("unapproved_subnet_attachment")
                    if any("dhcpOptions" in subnet["properties"] for subnet in subnets):
                        errors.append("subnet_dns_override")
                if names_match:
                    by_name = {subnet["name"]: subnet["properties"] for subnet in subnets}
                    for name, key in (("api", "apiNsgMatches"), ("cosmos", "endpointNsgMatches")):
                        attachment = by_name[name].get("networkSecurityGroup")
                        nsg_id = attachment.get("id") if isinstance(attachment, dict) else None
                        evidence[key] = isinstance(nsg_id, str) and nsg_id.casefold() == nsgs[name]
                        if not evidence[key]:
                            errors.append("subnet_nsg_drift")
                    evidence["apiPrefixMatches"] = by_name["api"].get("addressPrefix") == expected_subnets["api"]
                    evidence["endpointPrefixMatches"] = by_name["cosmos"].get("addressPrefix") == expected_subnets["cosmos"]
                    if not evidence["apiPrefixMatches"] or not evidence["endpointPrefixMatches"]:
                        errors.append("subnet_prefix_drift")
                    delegates = by_name["api"].get("delegations")
                    evidence["apiDelegationMatches"] = (
                        isinstance(delegates, list) and len(delegates) == 1 and isinstance(delegates[0], dict)
                        and isinstance(delegates[0].get("properties"), dict)
                        and delegates[0]["properties"].get("serviceName") == "Microsoft.App/environments"
                    )
                    evidence["endpointPolicyMatches"] = by_name["cosmos"].get("privateEndpointNetworkPolicies") == "Disabled"
                    if not evidence["apiDelegationMatches"]:
                        errors.append("api_subnet_delegation_drift")
                    if not evidence["endpointPolicyMatches"]:
                        errors.append("endpoint_subnet_policy_drift")
                dns = props.get("dhcpOptions")
                dns_evidence = {"optionsPresent": "dhcpOptions" in props, "representation": "invalid",
                                "serverCount": None, "serverCountCapped": False}
                row["dnsComparison"] = dns_evidence
                # Azure-provided DNS needs no configuration on a new VNet.
                # Only this Create/after omission is a default, never a read failure.
                if "dhcpOptions" not in props:
                    dns_evidence["representation"] = "omitted-create-default"
                elif isinstance(dns, dict) and set(dns) == {"dnsServers"} and isinstance(dns["dnsServers"], list):
                    dns_evidence["serverCount"] = min(len(dns["dnsServers"]), 128)
                    dns_evidence["serverCountCapped"] = len(dns["dnsServers"]) > 128
                    if not dns["dnsServers"]:
                        dns_evidence["representation"] = "explicit-empty"
                    else:
                        dns_evidence["representation"] = "nonempty-list"
                        errors.append("vnet_dns_not_default")
                else:
                    errors.append("vnet_dns_shape_invalid")
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
                    before_compare, after_compare = baseline, after
                    if descriptor is not None:
                        before_compare = project_proven_metadata(baseline, descriptor)
                        after_compare = project_proven_metadata(after, descriptor)
                    left = candidate_template_intent(before_compare["properties"]["template"])
                    right = candidate_template_intent(after_compare["properties"]["template"])
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
                    if digest(configuration_intent(before_compare["properties"]["configuration"])) != digest(
                        configuration_intent(after_compare["properties"].get("configuration", {}))
                    ):
                        errors.append("candidate_ingress_registry_or_secret_references_changed")
                    tags = deepcopy(baseline.get("tags", {}))
                    if tags.get("SecurityControl") == "Ignore":
                        tags.pop("SecurityControl")
                    tags["azd-service-name"] = "api-private-candidate"
                    if tags != after.get("tags"):
                        errors.append("candidate_tags_changed")
        elif kind == "Modify" and isinstance(before, dict) and isinstance(after, dict):
            echo_kind = {
                vnet: "vnet", endpoint: "endpoint", private_env: "environment",
                zone + f"/virtualnetworklinks/{suffix}": "link",
                endpoint + "/privatednszonegroups/default": "zoneGroup",
            }.get(rid)
            if packet == "foundation" and echo_kind and foundation_echo(change, binding, echo_kind):
                row["classification"] = "reviewed_source_identical_echo"
                continue
            if packet in ("candidate", "activate", "cutover"):
                if not isinstance(baseline, dict):
                    raise PreviewError("Current application snapshot is missing.")
                before_compare, after_compare, baseline_compare = before, after, baseline
                if descriptor is not None:
                    validate_native_metadata(before, descriptor)
                    before_compare = project_proven_metadata(before, descriptor)
                    after_compare = project_proven_metadata(after, descriptor)
                    baseline_compare = project_proven_metadata(baseline, descriptor)
                current_props = baseline_compare["properties"]
                before_props, after_props = before_compare.get("properties", {}), after_compare.get("properties", {})
                if comparable(candidate_template_intent(before_props.get("template", {}))) != comparable(
                    candidate_template_intent(current_props["template"])
                ):
                    errors.append("preview_before_differs_from_current_configuration")
                for resource, props in ((before_compare, before_props), (after_compare, after_props)):
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
                if descriptor is not None:
                    left = project_proven_metadata(before, descriptor)["properties"]["template"]
                    right = project_proven_metadata(after, descriptor)["properties"]["template"]
                left, right = candidate_template_intent(left), candidate_template_intent(right)
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
            "nsgComparisons": sorted(nsg_evidence, key=lambda row: row["resourceId"]),
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


ARM_ERROR_CODES = {
    "InvalidTemplate", "InvalidTemplateDeployment", "DeploymentFailed", "AuthorizationFailed",
    "ResourceNotFound", "DeploymentNotFound", "ResourceGroupNotFound", "InvalidResourceReference",
    "InvalidRequestContent", "BadRequest", "Conflict", "InternalServerError", "TooManyRequests",
    "RequestDisallowedByPolicy", "InvalidParameter", "OperationNotAllowed", "ParentResourceNotFound",
    "LinkedAuthorizationFailed", "NoRegisteredProviderFound", "MissingSubscriptionRegistration",
    "SkuNotAvailable", "InsufficientQuota", "InvalidAuthenticationToken", "AuthenticationFailed",
}


def command_diagnostic(command: list[str], kind: str, raw: bytes | None) -> dict[str, str | None]:
    operation = ("azure_deployment_create" if command[:4] == ["az", "deployment", "group", "create"]
                 else "azure_cli" if command[:1] == ["az"]
                 else "github_cli" if command[:1] == ["gh"] else "bounded_command")
    result: dict[str, str | None] = {
        "class": kind, "operation": operation, "code": None, "target": None,
        "message": "Provider error detail unavailable; no further execution is authorized.",
    }
    if kind != "command_failed" or raw is None or len(raw) > 16384:
        return result
    try:
        text = raw.decode("utf-8").strip().removeprefix("ERROR:").strip()
        try:
            parsed = PROVENANCE.strict_json(text.encode())
        except PROVENANCE.ProvenanceError:
            match = re.fullmatch(r"\(([A-Za-z][A-Za-z0-9]{1,63})\)\s+(.+)", text, re.DOTALL)
            parsed = {"code": match[1], "message": match[2].split("\nCode:", 1)[0]} if match else None
        if not isinstance(parsed, dict):
            return result
        error = parsed.get("error", parsed)
        if not isinstance(error, dict) or error.get("code") not in ARM_ERROR_CODES:
            return result
        result.update(code=error["code"], **{"class": "arm_error"})
        for key, limit in (("target", 256), ("message", 2048)):
            value = error.get(key)
            if not isinstance(value, str) or len(value) > 4096:
                continue
            if key == "target":
                result[key] = value if value in {
                    "properties.template.scale.maxReplicas", "apiApp", "secretSource",
                    "properties.configuration.secrets", "properties.template.containers",
                } else "[REDACTED_TARGET]"
                continue
            prefix = "Deployment template validation failed: '"
            if value.startswith(prefix) and (value.endswith("'.") or value.endswith("'")):
                value = "Deployment template validation failed: " + value[len(prefix):-(2 if value.endswith("'.") else 1)]
            value = value.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}))
            value = re.sub(
                r"(?i)(\b(?:client[_-]?secret|shared[_-]?key|connection[_-]?string|instrumentation[_-]?key)"
                r"\b[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1[REDACTED]", value,
            )
            safe = ROLLOUT.redact(value, max_chars=4096)
            if safe:
                safe = re.sub(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`''', "[REDACTED_VALUE]", safe)
                safe = re.sub(r'''["'`].*$''', "[REDACTED_VALUE]", safe)
                safe = re.sub(r"https?://[^\s<>\"']+", "[REDACTED_URL]", safe)
                safe = re.sub(r"[A-Za-z0-9_+/=-]{24,}", "[REDACTED]", safe)
                result[key] = safe[:limit]
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return result
    return result


def run(command: list[str], *, limit: int, timeout: float) -> bytes:
    try:
        return PROVENANCE.run_bounded(
            command, limit=limit, timeout=timeout,
            diagnostic=lambda kind, raw: command_diagnostic(command, kind, raw),
        )
    except PROVENANCE.ProvenanceError as exc:
        detail = "" if exc.diagnostic is None else " " + json.dumps(exc.diagnostic, separators=(",", ":"), ensure_ascii=True)
        raise PreviewError("Bounded command failed; no further resource execution is authorized." + detail) from exc


def cloud_read(args: list[str], *, timeout: float = 60) -> Any:
    return json.loads(run(["az", *args, "-o", "json"], limit=8 * 1024 * 1024, timeout=timeout))


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


PATCH_API_VERSION = "2024-10-02-preview"
PATCH_APP_QUERY = (
    "{id:id,type:type,location:location,tags:tags,identity:identity,"
    "properties:{managedEnvironmentId:properties.managedEnvironmentId,"
    "provisioningState:properties.provisioningState,latestRevisionName:properties.latestRevisionName,"
    "latestReadyRevisionName:properties.latestReadyRevisionName,patchingMode:properties.patchingMode,"
    "workloadProfileName:properties.workloadProfileName,configuration:properties.configuration,"
    "template:properties.template}}"
)


def patch_metadata(snapshot: dict) -> dict:
    props = snapshot["properties"]
    secrets = secret_metadata(props["configuration"].get("secrets"))
    require({row["name"] for row in secrets} <= SECRET_NAMES, "Activation secret inventory changed.")
    containers = props["template"].get("containers")
    require(isinstance(containers, list) and len(containers) == 1 and containers[0].get("name") == "api",
            "Activation metadata container changed.")
    rows = containers[0].get("env")
    refs, values, _ = env_metadata(rows)
    require(set(refs.values()) <= {row["name"] for row in secrets}, "Activation has an undeclared secret reference.")
    return {"secrets": secrets, "envSecretRefs": refs, "envValueNames": sorted(values),
            "valuePresent": {row["name"]: "value" in row for row in rows if row["name"] in values}}


def patch_protected(snapshot: dict) -> dict:
    props = snapshot["properties"]
    return {"identity": snapshot["identity"], "location": snapshot["location"], "tags": snapshot["tags"],
            **{key: props[key] for key in (
                "managedEnvironmentId", "configuration", "patchingMode", "workloadProfileName",
            )}}


def patch_raw_snapshot(env: dict, name: str, read: Callable, *, stable: bool = True, timeout: float = 45) -> dict:
    require(name in (api_name(env["AZURE_ENV_NAME"], "private"), api_name(env["AZURE_ENV_NAME"], "legacy")),
            "Activation raw target is outside scope.")
    group = f"rg-{env.get('AI4IA_WORKLOAD') or 'ai4ia'}-{env['AZURE_ENV_NAME']}"
    rid = f"/subscriptions/{env['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{group}/providers/Microsoft.App/containerApps/{name}"
    args = ["rest", "--method", "get", "--subscription", env["AZURE_SUBSCRIPTION_ID"],
            "--url", f"https://management.azure.com{rid}?api-version={PATCH_API_VERSION}", "--query", PATCH_APP_QUERY]
    snapshot = read(args, timeout=timeout)
    require(isinstance(snapshot, dict) and isinstance(snapshot.get("id"), str)
            and snapshot["id"].casefold() == rid.casefold() and snapshot.get("type") == "Microsoft.App/containerApps",
            "Activation raw application identity is unavailable.")
    props, identity = snapshot.get("properties"), snapshot.get("identity")
    identity_id = (f"/subscriptions/{env['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{group}"
                   f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-api-{env['AZURE_ENV_NAME']}")
    require(isinstance(props, dict) and isinstance(props.get("configuration"), dict)
            and isinstance(props.get("template"), dict) and isinstance(identity, dict)
            and identity.get("type") == "UserAssigned" and isinstance(identity.get("userAssignedIdentities"), dict)
            and all(isinstance(key, str) for key in identity["userAssignedIdentities"])
            and {key.casefold() for key in identity["userAssignedIdentities"]} == {identity_id.casefold()},
            "Activation raw configuration or identity is incomplete.")
    for key in ("latestRevisionName", "latestReadyRevisionName"):
        require(isinstance(props.get(key), str)
                and re.fullmatch(re.escape(name) + r"--[a-z0-9-]{1,80}", props[key]) is not None,
                "Activation raw revision identity is unavailable.")
    if name == api_name(env["AZURE_ENV_NAME"], "private"):
        configuration = props["configuration"]
        require(configuration.get("activeRevisionsMode") == "Single"
                and isinstance(configuration.get("ingress"), dict) and configuration["ingress"].get("external") is True,
                "Activation raw ingress or revision mode changed.")
    PROVENANCE.encode(snapshot)
    patch_metadata(snapshot)
    if stable:
        require(digest(read(args, timeout=timeout)) == digest(snapshot), "Activation raw snapshot changed during collection.")
    return snapshot


def private_revision_rows(binding: dict, read: Callable) -> list[dict]:
    name = api_name(binding["environment"], "private")
    path = (f"/subscriptions/{binding['subscription']}/resourceGroups/{binding['resourceGroup']}"
            f"/providers/Microsoft.App/containerApps/{name}/revisions")
    query = f"api-version={PATCH_API_VERSION}&pageSize=20"
    rows, names, cursors = [], set(), set()
    started = time.monotonic()
    for _ in range(31):
        remaining = 120 - (time.monotonic() - started)
        require(remaining > 0, "Private revision inventory exceeded its deadline.")
        data = read(["rest", "--method", "get", "--subscription", binding["subscription"],
                     "--url", "https://management.azure.com" + path + "?" + query,
                     "--query", "{rows:map(&{name:name,active:properties.active,health:properties.healthState,"
                     "state:properties.provisioningState},value),nextLink:nextLink}"], timeout=min(45, remaining))
        require(isinstance(data, dict) and isinstance(data.get("rows"), list)
                and len(rows) + len(data["rows"]) < 640, "Private revision coverage is invalid.")
        for row in data["rows"]:
            require(isinstance(row, dict) and isinstance(row.get("name"), str)
                    and re.fullmatch(re.escape(name) + r"--[a-z0-9-]{1,80}", row["name"]) is not None
                    and row["name"] not in names and type(row.get("active")) is bool,
                    "Private revision identity/activity is invalid or duplicated.")
            names.add(row["name"])
            rows.append(row)
        if data.get("nextLink") is None:
            require(bool(rows) and time.monotonic() - started < 120, "Private revision inventory is empty or late.")
            return rows
        cursor = ADMISSION.revision_continuation(data["nextLink"], path)
        require(bool(data["rows"]), "Private revision inventory has an empty nonterminal page.")
        require(cursor not in cursors, "Private revision cursor repeated.")
        cursors.add(cursor)
        query = urlencode({"api-version": PATCH_API_VERSION, "pageSize": "20", "skipToken": cursor})
    raise PreviewError("Private revision inventory reached its page cap.")


def activation_patch_context(env: dict, read: Callable = cloud_read) -> tuple[dict, dict, dict, dict, dict]:
    require(from_environment(env) == "activate" and env.get("AI4IA_PRIVATE_STEP", "activate") in ("", "activate")
            and env.get("GITHUB_REF") == "refs/heads/main", "Activation PATCH requires its exact trusted phase.")
    raw_receipt = env.get("AI4IA_PRIVATE_IMAGE_RECEIPT", "")
    require(bool(raw_receipt), "Activation PATCH requires the explicit prepared image receipt.")
    receipt = PROVENANCE.strict_json(raw_receipt.encode())
    if not isinstance(receipt, dict):
        raise PreviewError("Activation image receipt is invalid.")
    admitted = ADMISSION.admission(env)
    environment, subscription = env["AZURE_ENV_NAME"], env["AZURE_SUBSCRIPTION_ID"]
    group = f"rg-{env.get('AI4IA_WORKLOAD') or 'ai4ia'}-{environment}"
    current = patch_raw_snapshot(env, api_name(environment, "private"), read)
    legacy = patch_raw_snapshot(env, api_name(environment, "legacy"), read)
    require(current["properties"]["provisioningState"] == "Succeeded"
            and current["properties"]["latestRevisionName"] == current["properties"]["latestReadyRevisionName"],
            "Activation source revision is unsettled.")
    image = current["properties"]["template"]["containers"][0]["image"]
    require(image == receipt.get("image") == admitted["AI4IA_API_PRIVATE_INITIAL_IMAGE"],
            "Activation cannot change the reviewed image.")
    registry = image.split(".", 1)[0]
    run(["az", "acr", "login", "--subscription", subscription, "--name", registry], limit=4096, timeout=60)
    image_proof = PROVENANCE.consume_private_api(receipt, env)
    after = activation_patch_template(current, legacy)
    metadata = patch_metadata(current)
    origin = admitted["AI4IA_PRIVATE_API_ORIGIN"]
    bound = {
        "contract": "private-activation-patch-v1", "packet": "activate", "phase": "activate",
        "source": env["GITHUB_SHA"], "environment": environment, "subscription": subscription,
        "resourceGroup": group, "apiVersion": PATCH_API_VERSION, "apiOrigin": origin,
        "resourceId": current["id"], "image": image, "imageProof": digest(image_proof),
        "snapshot": digest(current), "protected": digest(patch_protected(current)),
        "configuration": digest(current["properties"]["configuration"]), "identity": digest(current["identity"]),
        "metadata": digest(metadata), "beforeTemplate": digest(current["properties"]["template"]),
        "afterTemplate": digest(after), "sourceRevision": current["properties"]["latestReadyRevisionName"],
        "legacyProfile": digest(legacy["properties"]["template"]),
        "legacyInventory": json.loads(admitted["AI4IA_PRIVATE_LEGACY_INVENTORY"]),
        "privateCoreQuota": consumption_headroom(read([
            "containerapp", "env", "list-usages", "--subscription", subscription, "-g", group,
            "-n", f"cae-{env.get('AI4IA_WORKLOAD') or 'ai4ia'}-{environment}-api-private",
        ]), 1.5),
    }
    require(bound["legacyInventory"]["activeRevisions"] == [], "Legacy workers are not drained.")
    if "imageReuse" in image_proof:
        bound.update(imageReuse=image_proof["imageReuse"], imageSourceChanges=image_proof["changedPaths"])
    preview = {
        "kind": "template_only_patch", "nativeWhatIf": False, "blockingReasons": [],
        "changes": [
            {"path": "properties.template.scale.maxReplicas", "before": 1, "after": 3, "source": "legacy_production"},
            {"environmentVariable": "AI4IA_PRIVATE_RECOVERY_CANDIDATE", "before": "true", "after": "false"},
        ],
        "providerFields": {"revisionSuffix": "binding_derived", "ephemeralStorage": "read_only_omitted"},
    }
    return bound, preview, current, legacy, after


def check_activation_post(snapshot: dict, revision: dict, body: dict, bound: dict, token: str) -> None:
    suffix = activation_suffix(token)
    name = api_name(bound["environment"], "private") + "--" + suffix
    props = snapshot["properties"]
    require(props["provisioningState"] == "Succeeded"
            and props["latestRevisionName"] == props["latestReadyRevisionName"] == revision.get("name") == name
            and revision.get("properties", {}).get("active") is True
            and revision["properties"].get("healthState") == "Healthy"
            and revision["properties"].get("provisioningState") == "Provisioned",
            "Deterministic activation revision is not ready.")
    require(digest(patch_protected(snapshot)) == bound["protected"]
            and digest(props["configuration"]) == bound["configuration"]
            and digest(snapshot["identity"]) == bound["identity"]
            and digest(patch_metadata(snapshot)) == bound["metadata"],
            "Activation changed protected configuration, identity or secret metadata.")
    expected = body["properties"]["template"]
    require(comparable(props["template"]) == comparable(expected)
            and comparable(revision["properties"]["template"]) == comparable(expected),
            "Activated template differs from the exact reviewed PATCH.")
    require(props["template"]["containers"][0]["image"] == bound["image"],
            "Activated image changed.")


def activation_ready(env: dict, bound: dict, body: dict, token: str, read: Callable, *, clock=time.monotonic, sleep=time.sleep) -> dict:
    expected = api_name(bound["environment"], "private") + "--" + activation_suffix(token)
    started = clock()

    def bounded_read(args, *, timeout=45):
        remaining = 240 - (clock() - started)
        require(remaining > 0, "Activation readiness deadline exhausted.")
        result = read(args, timeout=min(timeout, remaining))
        require(clock() - started < 240, "Activation readiness deadline exhausted.")
        return result

    for _ in range(12):
        remaining = 240 - (clock() - started)
        require(remaining > 0, "Activation readiness deadline exhausted.")
        snapshot = patch_raw_snapshot(env, api_name(bound["environment"], "private"), bounded_read, stable=False, timeout=min(45, remaining))
        props = snapshot["properties"]
        if props["latestRevisionName"] == props["latestReadyRevisionName"] == expected and props["provisioningState"] == "Succeeded":
            revision = bounded_read(["rest", "--method", "get", "--subscription", bound["subscription"],
                             "--url", f"https://management.azure.com{bound['resourceId']}/revisions/{expected}?api-version={PATCH_API_VERSION}",
                             "--query", "{name:name,properties:{active:properties.active,healthState:properties.healthState,"
                             "provisioningState:properties.provisioningState,template:properties.template}}"])
            check_activation_post(snapshot, revision, body, bound, token)
            require(digest(patch_raw_snapshot(env, api_name(bound["environment"], "private"), bounded_read)) == digest(snapshot),
                    "Activated application changed during verification.")
            remaining = 240 - (clock() - started)
            require(remaining > 0, "Activation post-check deadline exhausted.")
            health = ROLLOUT.http_request("GET", bound["apiOrigin"] + "/health/ready", timeout=min(20, remaining),
                                         deadline=ROLLOUT.Deadline(min(20, remaining)), body_limit=4096)
            require(health.status == 200 and health.json() == {"status": "ok", "stage": "session_store"},
                    "Activated private Cosmos readiness failed.")
            require(clock() - started < 240, "Activation post-check deadline exhausted.")
            return {"revision": expected, "configuration": bound["configuration"], "metadata": bound["metadata"],
                    "identity": bound["identity"], "template": digest(props["template"]),
                    "readiness": "passed", "ownerOptions": "required_separate_authenticated_gate"}
        remaining = 240 - (clock() - started)
        require(remaining > 0, "Activation readiness deadline exhausted.")
        sleep(min(5, remaining))
    raise PreviewError("Deterministic activation revision did not become ready within its bound.")


def verify_activation_owner(
    env: dict, execution: dict, read: Callable = cloud_read, *, admission_read: Callable | None = None,
) -> dict:
    """Owner-scoped read-only acceptance; never substitute or broaden the deployment actor."""
    token = env.get(ROLLOUT.DEFAULT_TOKEN_ENV, "")
    require(bool(token), "Authenticated owner acceptance token is unavailable.")
    bound = execution["context"]
    require(bound.get("contract") == "private-activation-patch-v1"
            and execution.get("status") == "ready_pending_owner_acceptance"
            and bound["environment"] == env["AZURE_ENV_NAME"] and bound["subscription"] == env["AZURE_SUBSCRIPTION_ID"]
            and bound["resourceGroup"] == f"rg-{env.get('AI4IA_WORKLOAD') or 'ai4ia'}-{env['AZURE_ENV_NAME']}",
            "Owner acceptance receipt is outside the approved activation.")
    admitted = ADMISSION.admission(env) if admission_read is None else ADMISSION.admission(env, read=admission_read)
    legacy = json.loads(admitted["AI4IA_PRIVATE_LEGACY_INVENTORY"])
    require(legacy["activeRevisions"] == [] and admitted["AI4IA_API_PRIVATE_INITIAL_IMAGE"] == bound["image"],
            "Activation image or legacy drain changed before owner acceptance.")
    snapshot = patch_raw_snapshot(env, api_name(bound["environment"], "private"), read)
    props = snapshot["properties"]
    expected = api_name(bound["environment"], "private") + "--" + activation_suffix(execution["binding"])
    require(props["latestRevisionName"] == props["latestReadyRevisionName"] == execution["revision"] == expected
            and props["provisioningState"] == "Succeeded"
            and digest(patch_protected(snapshot)) == bound["protected"]
            and digest(patch_metadata(snapshot)) == bound["metadata"]
            and digest(activation_template_projection(props["template"])) == bound["afterTemplate"],
            "Activation changed before owner acceptance.")
    origin = "https://" + props["configuration"]["ingress"]["fqdn"]
    require(origin == bound["apiOrigin"] and re.fullmatch(
        r"https://ca-api-private-" + re.escape(env["AZURE_ENV_NAME"]) + r"\.[a-z0-9.-]+\.azurecontainerapps\.io", origin,
    ) is not None, "Owner acceptance origin is unverified.")
    health = ROLLOUT.http_request("GET", origin + "/health/ready", timeout=20,
                                 deadline=ROLLOUT.Deadline(20), body_limit=4096)
    require(health.status == 200 and health.json() == {"status": "ok", "stage": "session_store"},
            "Owner acceptance private readiness failed.")
    response = ROLLOUT.http_request("GET", origin + "/api/decisions/options",
                                   headers={"Authorization": "Bearer " + token}, timeout=20,
                                   deadline=ROLLOUT.Deadline(20), body_limit=8192)
    result = response.json()
    catalog = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
    expected_models = {row["name"] for row in catalog["catalog"]
                       if row["category"] == "decision" and row.get("runtimeEnabled", True) is True}
    require(response.status == 200 and isinstance(result, dict) and isinstance(result.get("models"), list)
            and 0 < len(result["models"]) <= 32
            and all(isinstance(row, dict) and isinstance(row.get("id"), str)
                    and isinstance(row.get("residencies"), list) and bool(row["residencies"])
                    and all(isinstance(value, str) and bool(value) for value in row["residencies"])
                    for row in result["models"])
            and len({row["id"] for row in result["models"]}) == len(result["models"])
            and {row.get("id") for row in result["models"]} == expected_models,
            "Authenticated owner Decision options acceptance failed.")
    return {"status": "verified", "revision": expected, "configuration": bound["configuration"],
            "metadata": bound["metadata"], "readiness": "passed", "ownerOptions": "passed",
            "legacyInventory": legacy, "observedAt": int(time.time()), "modelCalls": 0}


def activation_patch_command(command: str, env: dict, read: Callable = cloud_read) -> int:
    if command == "apply":
        require(env.get("GITHUB_RUN_ATTEMPT") == "1"
                and not (Path(env["RUNNER_TEMP"]) / "ai4ia-private-preview/execution.json").exists(),
                "Activation dispatch was already attempted or recorded; never replay.")
    bound, preview, current, legacy, after = activation_patch_context(env, read)
    output = Path(env["RUNNER_TEMP"]) / "ai4ia-private-preview"
    output.mkdir(exist_ok=True)
    kind = env.get("AI4IA_RELEASE_KIND")
    require(kind in ("preview", "deploy"), "Activation PATCH requires an explicit preview/deploy.")
    now = int(time.time())
    token = make_token(bound, preview, now) if kind == "preview" else env.get("AI4IA_PRIVATE_PREVIEW_BINDING", "")
    verify_token(token, bound, preview, now)
    body = activation_patch_body(after, token)
    expected_revision = api_name(bound["environment"], "private") + "--" + activation_suffix(token)
    report = {"context": bound, "preview": preview, "binding": token, "issuedAt": int(token.split(":")[0]),
              "observedAt": now, "revision": expected_revision, "patchBodyDigest": digest(body),
              "executionAuthorized": False, "executionBlockers": []}
    if command == "prepare":
        rows = private_revision_rows(bound, read)
        require(bound["sourceRevision"] in {row["name"] for row in rows}
                and expected_revision not in {row["name"] for row in rows}, "Activation suffix is used or current coverage is missing.")
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if kind == "deploy":
            directory = Path(tempfile.mkdtemp(prefix=f"ai4ia-private-packet-{env['GITHUB_RUN_ID']}-", dir=env["RUNNER_TEMP"]))
            fd = os.open(directory / "patch.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(body, stream, allow_nan=False)
            proof = output / "proof.json"
            proof.write_text(json.dumps(report), encoding="utf-8")
            with Path(env["GITHUB_ENV"]).open("a", encoding="utf-8") as stream:
                stream.write(f"AI4IA_PRIVATE_PREVIEW_PROOF={proof}\nAI4IA_PRIVATE_PACKET_DIR={directory}\n")
        with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(f"authorized={'true' if kind == 'deploy' else 'false'}\n")
        print("Activation PATCH preview bound to fresh raw state; configuration and secrets are not in its body.")
        return 0
    require(command == "apply" and kind == "deploy" and env.get("AI4IA_RELEASE_PROVISION") == "true",
            "Only the explicitly reviewed activation PATCH may execute.")
    proof = PROVENANCE.read_object(Path(env["AI4IA_PRIVATE_PREVIEW_PROOF"]), 64 * 1024)
    require(proof["context"] == bound and proof["preview"] == preview and proof["binding"] == token,
            "Activation state changed after reviewed preview.")
    directory = Path(env["AI4IA_PRIVATE_PACKET_DIR"]).resolve()
    require(directory.is_relative_to(Path(env["RUNNER_TEMP"]).resolve()), "Activation packet is outside this runner.")
    frozen = PROVENANCE.read_object(directory / "patch.json", 512 * 1024)
    validate_activation_patch_body(frozen, current, legacy, token)
    require(digest(frozen) == proof["patchBodyDigest"], "Frozen activation PATCH changed.")
    admitted = ADMISSION.admission(env)
    require(json.loads(admitted["AI4IA_PRIVATE_LEGACY_INVENTORY"]) == bound["legacyInventory"],
            "Legacy drain changed immediately before PATCH.")
    rows = private_revision_rows(bound, read)
    require(bound["sourceRevision"] in {row["name"] for row in rows}
            and expected_revision not in {row["name"] for row in rows}, "Activation revision suffix already exists; never replay.")
    main = json.loads(run(["gh", "api", f"repos/{env['GITHUB_REPOSITORY']}/git/ref/heads/main"], limit=32768, timeout=30))
    require(main.get("object", {}).get("sha") == bound["source"], "Main changed before activation PATCH.")
    require(digest(patch_raw_snapshot(env, api_name(bound["environment"], "private"), read)) == bound["snapshot"],
            "Activation raw state changed immediately before PATCH.")
    verify_token(token, bound, preview, int(time.time()))
    execution = {"context": bound, "binding": token, "revision": expected_revision, "patchBodyDigest": digest(frozen),
                 "transport": "template_only_patch", "status": "dispatching", "modelCalls": 0}
    with (output / "execution.json").open("x", encoding="utf-8") as stream:
        json.dump(execution, stream, indent=2)
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write("receipt=true\n")
    try:
        run(["az", "rest", "--method", "patch", "--subscription", bound["subscription"],
             "--url", f"https://management.azure.com{bound['resourceId']}?api-version={PATCH_API_VERSION}",
             "--body", "@" + str(directory / "patch.json"), "--headers", "Content-Type=application/json",
             "--only-show-errors", "-o", "none"], limit=4096, timeout=120)
        execution["status"] = "accepted"
        execution["postChecks"] = activation_ready(env, bound, frozen, token, read)
        execution["status"] = "ready_pending_owner_acceptance"
    except (ValueError, OSError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        execution["status"] = "outcome_unknown" if execution["status"] == "dispatching" else "post_check_failed"
        raise
    finally:
        (output / "execution.json").write_text(json.dumps(execution, indent=2), encoding="utf-8")
        (directory / "patch.json").unlink(missing_ok=True)
    print("Exact activation PATCH delivered and readiness checked; authenticated owner options remain a separate gate.")
    return 0


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
    log_id = root + f"Microsoft.OperationalInsights/workspaces/log-{workload}-{environment}"
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
    if proof is not None and "imageReuse" in proof:
        bound.update(imageReuse=proof["imageReuse"], imageSourceChanges=proof["changedPaths"])
    bound.update(locationIdentity=location, locationObservations={
        "legacyApi": {"raw": old_api["location"], "canonical": canonical},
        "recordedApiParameter": {"raw": api_values["location"], "canonical": canonical},
        "sourceApplication": {"raw": source_app["location"], "canonical": canonical},
        "packetParameter": {"raw": params["location"]["value"], "canonical": canonical},
    })
    if packet == "cutover":
        bound["locationObservations"]["recordedWebParameter"] = {"raw": recorded_web_location, "canonical": canonical}
    if packet == "foundation":
        workspace = read([
            "monitor", "log-analytics", "workspace", "show", "--subscription", subscription,
            "-g", group, "-n", f"log-{workload}-{environment}", "--query", "{id:id,customerId:customerId}",
        ])
        require(isinstance(workspace, dict) and isinstance(workspace.get("id"), str)
                and workspace["id"].casefold() == log_id.casefold()
                and isinstance(workspace.get("customerId"), str)
                and re.fullmatch(r"[0-9a-fA-F-]{36}", workspace["customerId"]) is not None,
                "Scoped Log Analytics customer identity is unknown.")
        bound["logAnalyticsCustomerId"] = workspace["customerId"]
    if packet in ("candidate", "activate"):
        require(isinstance(baseline, dict), "Source application metadata is missing.")
        assert isinstance(baseline, dict)
        descriptor = source_descriptor(template, params, bound, baseline)
        source_id = baseline["id"]
        snapshot = read([
            "rest", "--method", "get", "--subscription", subscription,
            "--url", f"https://management.azure.com{source_id}?api-version=2024-10-02-preview",
            "--query", RAW_METADATA_QUERY,
        ])
        raw_metadata = validate_live_metadata(snapshot, descriptor, source_id, baseline=packet == "candidate")
        bound["secretMetadataDigest"] = digest(descriptor)
        bound["baselineSecretMetadataDigest"] = digest(raw_metadata)
        expected["sourceSecretMetadata"] = descriptor
    if packet in ("activate", "cutover"):
        bound["legacyInventory"] = legacy_inventory_proof(env.get("AI4IA_PRIVATE_LEGACY_INVENTORY", ""), bound)
    return template, params, bound, baseline, expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify-binding", "apply", "cleanup", "verify-activation"))
    args = parser.parse_args()
    env = dict(os.environ)
    packet_dir = None
    retain_packet = False
    try:
        if args.command == "verify-activation":
            execution = PROVENANCE.read_object(Path(env["AI4IA_PRIVATE_ACTIVATION_RECEIPT"]), 64 * 1024)
            print(json.dumps(verify_activation_owner(env, execution), indent=2))
            return 0
        if args.command == "cleanup":
            directory = Path(env["AI4IA_PRIVATE_PACKET_DIR"])
            require(directory.is_absolute() and not directory.is_symlink()
                    and directory.resolve().is_relative_to(Path(env["RUNNER_TEMP"]).resolve())
                    and directory.name.startswith(f"ai4ia-private-packet-{env['GITHUB_RUN_ID']}-"),
                    "Cleanup target is not this run's private packet.")
            (directory / "parameters.json").unlink(missing_ok=True)
            (directory / "template.json").unlink(missing_ok=True)
            (directory / "patch.json").unlink(missing_ok=True)
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
        if from_environment(env) == "activate":
            return activation_patch_command(args.command, env)
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
                admitted = ADMISSION.admission(env)
                if bound["packet"] in ("activate", "cutover"):
                    current_inventory = legacy_inventory_proof(admitted.get("AI4IA_PRIVATE_LEGACY_INVENTORY", ""), bound)
                    require(current_inventory == bound["legacyInventory"],
                            "Legacy inventory changed after the reviewed preview.")
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
                nsg_reads = verify_nsg_reads(proof["preview"], bound, cloud_read)
                verify_token(env["AI4IA_PRIVATE_PREVIEW_BINDING"], bound, proof["preview"], int(time.time()))
                descriptor = expected.get("sourceSecretMetadata")
                if bound["packet"] == "candidate":
                    require(isinstance(descriptor, dict) and digest(descriptor) == bound.get("secretMetadataDigest"),
                            "Post-create source metadata proof is missing.")
                run([
                    "az", "deployment", "group", "create", "--subscription", bound["subscription"],
                    "-g", bound["resourceGroup"], "-n", name, "--mode", "Incremental",
                    "--template-file", str(packet_dir / "template.json"),
                    "--parameters", "@" + str(packet_dir / "parameters.json"), "-o", "none",
                ], limit=4096, timeout=1800)
                execution = {"context": bound, "deploymentName": name, "resourceDeployment": "Succeeded",
                             "nsgReads": nsg_reads, "runtimeAcceptance": "not_yet_observed", "modelCalls": 0}
                metadata_failed = False
                if bound["packet"] == "candidate":
                    assert isinstance(descriptor, dict)
                    try:
                        execution["candidateSecretMetadata"] = candidate_metadata_gate(bound, descriptor, cloud_read)
                    except (MetadataError, PreviewError, OSError, KeyError, TypeError, ValueError, AttributeError):
                        metadata_failed = True
                        execution["candidateSecretMetadata"] = {"status": "failed", "sourceDigest": digest(descriptor)}
                        try:
                            execution["candidateContainment"] = contain_candidate(bound, cloud_read, run)
                        except (PreviewError, OSError, KeyError, TypeError, ValueError):
                            execution["candidateContainment"] = {"status": "unknown", "legacyTouched": False}
                (Path(env["RUNNER_TEMP"]) / "ai4ia-private-preview" / "execution.json").write_text(
                    json.dumps(execution, indent=2), encoding="utf-8")
                if env.get("GITHUB_OUTPUT"):
                    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
                        file.write("receipt=true\n")
                require(not metadata_failed,
                        "Candidate secret metadata verification failed; inspect candidate-only containment before continuing.")
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
        nsg_reads: list[dict] = []
        try:
            preview = scope_preview(payload, bound, expected, baseline, read=cloud_read, observations=nsg_reads)
        except (PreviewError, KeyError, TypeError, ValueError, AttributeError):
            (output / "report.json").write_text(json.dumps(rejected_report(payload, bound), indent=2), encoding="utf-8")
            if Path(parameter_path).exists():
                os.unlink(parameter_path)
            raise
        issued = int(time.time())
        token = make_token(bound, preview, issued)
        report = {"context": bound, "preview": preview, "issuedAt": issued,
                  "nsgReads": nsg_reads,
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
        print(str(exc) if isinstance(exc, (PreviewError, MetadataError)) else "Private preview failed with incomplete evidence.", file=sys.stderr)
        return 1
    finally:
        if packet_dir is not None and not retain_packet:
            (packet_dir / "parameters.json").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
