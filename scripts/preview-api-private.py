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

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?:=([^}]*))?\}")
BINDING = re.compile(r"([0-9]{10}):([0-9a-f]{64})")
MAX_AGE = 1800


def load_rollout():
    spec = importlib.util.spec_from_file_location("private_preview_rollout", ROOT / "scripts/post-deploy-verify.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ROLLOUT = load_rollout()


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


def scope_preview(raw: dict, binding: dict, params: dict) -> dict:
    require(raw.get("status") == "Succeeded" and not raw.get("error"), "ARM preview did not succeed.")
    require(not raw.get("diagnostics"), "ARM preview reported incomplete diagnostics.")
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
        if kind in ("Ignore", "NoChange"):
            continue
        if not rid.startswith(prefix) and rid != prefix.split("/providers/")[0]:
            errors.append("change_outside_workload")
            continue
        before, after = change.get("before"), change.get("after")
        row = {"resourceId": rid, "changeType": kind, "fields": []}
        deltas = change.get("delta", [])
        require(isinstance(deltas, list), "ARM property changes are incomplete.")
        for delta in deltas:
            path = delta.get("path")
            require(isinstance(path, str) and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,256}", path) is not None,
                    "Unsupported ARM property path shape.")
            row["fields"].append(path)
        report.append(row)
        if kind == "Create":
            if rid not in creates or not isinstance(after, dict):
                errors.append("unapproved_create")
                continue
            if "SecurityControl" in after.get("tags", {}):
                errors.append("security_exemption_tag")
            if after.get("location") not in (None, binding["location"], "global"):
                errors.append("unapproved_region")
            props = after.get("properties", {})
            if rid == vnet:
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
                if (template.get("scale", {}).get("minReplicas") != 1
                        or template.get("scale", {}).get("maxReplicas") != 1
                        or template["containers"][0].get("image") != params["apiPrivateInitialImage"]["value"]
                        or props.get("configuration", {}).get("activeRevisionsMode") != "Single"):
                    errors.append("candidate_image_or_scale_drift")
        elif kind == "Modify" and isinstance(before, dict) and isinstance(after, dict):
            paths = set(row["fields"])
            old_tags, new_tags = deepcopy(before.get("tags", {})), deepcopy(after.get("tags", {}))
            old_tags.pop("SecurityControl", None)
            tag_change_ok = old_tags == new_tags and "SecurityControl" not in new_tags
            if paths and paths <= {"tags", "tags.SecurityControl"} and tag_change_ok:
                continue
            if rid in app_ids and paths and all(path.startswith("properties.template") for path in paths):
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
    require(creates <= seen, "ARM preview omitted required private resources.")
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
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    require(result.returncode == 0, "Preview command failed; no resource execution is authorized.")
    require(len(result.stdout) <= limit, "Preview output exceeds its bound.")
    return result.stdout


def compiled_context(env: dict[str, str]) -> tuple[dict, dict, dict]:
    run([sys.executable, str(ROOT / "scripts/validate-feature-prereqs.py"), "--require-deployment-attestation"],
        limit=256 * 1024, timeout=60)
    raw = run(["bicep", "build", str(ROOT / "infra/main.bicep"), "--stdout"], limit=8 * 1024 * 1024, timeout=120)
    template = json.loads(raw)
    params = resolve_parameters(json.loads((ROOT / "infra/main.parameters.json").read_text()), template, env)
    return template, params, context(env, template, params)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify-binding"))
    args = parser.parse_args()
    env = dict(os.environ)
    try:
        if args.command == "prepare":
            with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
                file.write("authorized=false\n")
        if args.command == "verify-binding" and (env.get("AI4IA_API_NETWORK_PHASE") or "legacy") == "legacy":
            return 0
        kind = env.get("AI4IA_RELEASE_KIND")
        if args.command == "verify-binding" and kind == "ordinary":
            require(env.get("GITHUB_REF") == "refs/heads/main", "Ordinary release requires trusted main.")
            spec = importlib.util.spec_from_file_location("private_hook_admission", ROOT / "scripts/check-api-private-network.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            current = module.admission(env)
            require(current["AI4IA_PRIVATE_MIGRATION_PENDING"] == "false", "Ordinary release is not yet available.")
            return 0
        template, params, bound = compiled_context(env)
        if args.command == "verify-binding":
            proof = json.loads(Path(env["AI4IA_PRIVATE_PREVIEW_PROOF"]).read_text())
            require(proof["context"] == bound, "Deployment inputs changed after reviewed preview.")
            verify_token(env.get("AI4IA_PRIVATE_PREVIEW_BINDING", ""), bound, proof["preview"], int(time.time()))
            return 0
        require(kind in ("preview", "deploy"), "Explicit preview/deploy operation required.")
        output = Path(env["RUNNER_TEMP"]) / "ai4ia-private-preview"
        output.mkdir(exist_ok=True)
        descriptor, parameter_path = tempfile.mkstemp(prefix="ai4ia-preview-", suffix=".json", dir=env["RUNNER_TEMP"])
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump({"parameters": params}, file)
            template_path = output / "template.json"
            template_path.write_text(json.dumps(template), encoding="utf-8")
            raw = run([
                "az", "deployment", "sub", "what-if", "--subscription", bound["subscription"],
                "--location", bound["location"], "--name", "ai4ia-private-preview",
                "--template-file", str(template_path), "--parameters", "@" + parameter_path,
                "--validation-level", "Provider", "--no-pretty-print", "--no-prompt", "true", "-o", "json",
            ], limit=8 * 1024 * 1024, timeout=300)
        finally:
            os.unlink(parameter_path)
        preview = scope_preview(json.loads(raw), bound, params)
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
        with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
            file.write(f"authorized={'true' if kind == 'deploy' else 'false'}\n")
        print("Private phase preview retained; no resources changed. Execution requires its separate guarded path.")
        return 0
    except (PreviewError, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc) if isinstance(exc, PreviewError) else "Private preview failed with incomplete evidence.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
