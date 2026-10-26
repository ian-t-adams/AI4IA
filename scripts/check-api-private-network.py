#!/usr/bin/env python3
"""Read-only migration admission. Flags select intent; observed state admits it."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from _api_private import api_name, deployment_operation, from_environment

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "mcr.microsoft.com/k8se/quickstart:latest"
APP_QUERY = (
    "{id:id,identity:identity,properties:{managedEnvironmentId:properties.managedEnvironmentId,"
    "latestReadyRevisionName:properties.latestReadyRevisionName,latestRevisionName:properties.latestRevisionName,"
    "configuration:{ingress:properties.configuration.ingress,activeRevisionsMode:properties.configuration.activeRevisionsMode}}}"
)
REVISION_QUERY = (
    "{properties:{active:properties.active,healthState:properties.healthState,"
    "provisioningState:properties.provisioningState,template:{containers:properties.template.containers[]."
    "{image:image,env:env[?name=='AI4IA_PRIVATE_RECOVERY_CANDIDATE' || name=='AI4IA_PRIVATE_COSMOS_REQUIRED' "
    "|| name=='AI4IA_COSMOS_ENDPOINT' || name=='API_BASE_URL' || name=='API_PUBLIC_URL']}}}}"
)


class AdmissionError(ValueError):
    """Only fixed, content-free errors may reach deployment output."""


def require(value: bool, message: str) -> None:
    if not value:
        raise AdmissionError(message)


def cli(args: list[str], *, absent: bool = False, timeout: float = 45):
    result = subprocess.run(["az", *args, "--only-show-errors", "-o", "json"],
                            capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        if absent and any(result.stderr.startswith(f"ERROR: ({code})".encode())
                          for code in ("ResourceNotFound", "ResourceGroupNotFound")):
            return None
        raise AdmissionError("Private API scoped metadata read failed.")
    require(len(result.stdout) <= 512 * 1024, "Private API metadata exceeds bound.")
    return json.loads(result.stdout)


REVISION_API_VERSION = "2024-10-02-preview"
REVISION_PAGE_SIZE = "20"
MAX_REVISION_PAGES = 32
MAX_REVISION_ROWS = 640
REVISION_QUERY_FIELDS = (
    "{rows:map(&{name:name,active:properties.active,replicas:properties.replicas,"
    "runningState:properties.runningState,provisioningState:properties.provisioningState},value),nextLink:nextLink}"
)


def revision_continuation(link: object, path: str) -> str:
    require(isinstance(link, str) and 0 < len(link) <= 16384 and re.fullmatch(r"[\x21-\x7e]+", link) is not None
            and "#" not in link and re.search(r"%(?![0-9a-fA-F]{2})", link) is None,
            "Invalid legacy revision continuation.")
    assert isinstance(link, str)
    try:
        parsed = urlsplit(link)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=3)
    except ValueError as exc:
        raise AdmissionError("Invalid legacy revision continuation.") from exc
    require(parsed.scheme == "https" and parsed.netloc.casefold() == "management.azure.com"
            and parsed.path == path and not parsed.fragment, "Legacy revision continuation changed scope.")
    parameters = dict(pairs)
    require(len(pairs) == 3 and set(parameters) == {"api-version", "pageSize", "skipToken"}
            and {part.split("=", 1)[0] for part in parsed.query.split("&")} == set(parameters)
            and parameters["api-version"] == REVISION_API_VERSION and parameters["pageSize"] == REVISION_PAGE_SIZE,
            "Legacy revision continuation changed query.")
    cursor = parameters["skipToken"]
    require(0 < len(cursor) <= 8192 and re.fullmatch(r"[\x21-\x7e]+", cursor) is not None,
            "Legacy revision cursor is unknown.")
    return cursor


def legacy_revision_inventory(
    subscription: str, group: str, name: str, read=cli, *,
    expected_active: str | None = None, require_drained: bool = False, clock=time.monotonic,
) -> dict:
    """Complete scalar list proof; no inactive-row detail sweep or scope fallback."""
    require(re.fullmatch(r"[0-9a-fA-F-]{36}", subscription) is not None
            and re.fullmatch(r"rg-[a-z0-9-]{3,64}", group) is not None
            and re.fullmatch(r"ca-api-[a-z0-9-]{2,20}", name) is not None and not name.startswith("ca-api-private-"),
            "Legacy revision inventory target is invalid.")
    path = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.App/containerApps/{name}/revisions"
    query = {"api-version": REVISION_API_VERSION, "pageSize": REVISION_PAGE_SIZE}
    started = clock()
    rows: list[dict] = []
    names, cursors = set(), set()
    pages = 0

    def bounded(args):
        remaining = 120 - (clock() - started)
        require(remaining > 0, "Legacy revision inventory exceeded its time cap.")
        result = read(args, timeout=min(45, remaining))
        require(clock() - started < 120, "Legacy revision inventory exceeded its time cap.")
        return result

    while True:
        require(pages + 1 < MAX_REVISION_PAGES, "Legacy revision inventory reached its page cap.")
        page = bounded([
            "rest", "--method", "get", "--subscription", subscription,
            "--url", "https://management.azure.com" + path + "?" + urlencode(query),
            "--query", REVISION_QUERY_FIELDS,
        ])
        pages += 1
        require(isinstance(page, dict) and isinstance(page.get("rows"), list),
                "Legacy revision page is incomplete.")
        page_rows = page["rows"]
        require(len(rows) + len(page_rows) < MAX_REVISION_ROWS, "Legacy revision inventory reached its row cap.")
        for row in page_rows:
            require(isinstance(row, dict) and set(row) == {"name", "active", "replicas", "runningState", "provisioningState"},
                    "Legacy revision row is incomplete.")
            revision = row["name"]
            require(isinstance(revision, str) and re.fullmatch(re.escape(name) + r"--[a-z0-9-]{1,80}", revision) is not None
                    and revision not in names and type(row["active"]) is bool,
                    "Legacy revision identity or activity is invalid or duplicated.")
            require(row["replicas"] is None or type(row["replicas"]) is int and row["replicas"] >= 0,
                    "Legacy replica count is invalid.")
            require(all(isinstance(row[key], str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", row[key]) is not None
                        for key in ("runningState", "provisioningState")), "Legacy revision state is unknown.")
            names.add(revision)
            rows.append(row)
        link = page.get("nextLink")
        if link is None:
            break
        require(bool(page_rows), "Empty nonterminal legacy revision page.")
        cursor = revision_continuation(link, path)
        require(cursor not in cursors, "Repeated legacy revision cursor.")
        cursors.add(cursor)
        query = {**query, "skipToken": cursor}
    require(bool(rows), "Legacy revision inventory is empty.")
    active = [row["name"] for row in rows if row["active"]]
    require(len(active) <= 1 and (expected_active is None or active == [expected_active]),
            "Legacy active revision does not match the approved drain.")
    probes = [row for row in rows if row["active"] or row["replicas"] is None or row["replicas"] != 0]
    require(len(probes) <= 4, "Legacy replica checks exceed their cap.")
    for row in probes:
        replicas = bounded([
            "containerapp", "replica", "list", "--subscription", subscription, "-g", group, "-n", name,
            "--revision", row["name"], "--query", "[].name",
        ])
        require(isinstance(replicas, list) and len(replicas) <= 1000
                and all(isinstance(replica, str) and 0 < len(replica) <= 256 for replica in replicas),
                "Legacy replica inventory is unavailable.")
        if not row["active"]:
            require(not replicas, "An inactive legacy revision still has replicas.")
    require(all(row["active"] or type(row["replicas"]) is int and row["replicas"] == 0 for row in rows),
            "Inactive legacy replica count is nonzero or unknown.")
    require(not require_drained or not active, "Legacy revisions must be inactive before activation.")
    proof = {
        "appId": path.removesuffix("/revisions"), "pageCount": pages, "rowCount": len(rows),
        "digest": hashlib.sha256(json.dumps(sorted(rows, key=lambda row: row["name"]),
                                          sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "activeRevisions": active, "replicaReadCount": len(probes),
    }
    require(clock() - started < 120, "Legacy revision inventory exceeded its time cap.")
    return {"rows": rows, "proof": proof}


def prove_ready(origin: str) -> bool:
    spec = importlib.util.spec_from_file_location("private_readiness", ROOT / "scripts" / "post-deploy-verify.py")
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    result = helper.http_request("GET", origin + "/health/ready", timeout=20,
                                 deadline=helper.Deadline(20), body_limit=4096)
    return result.status == 200 and result.json() == {"status": "ok", "stage": "session_store"}


def admission(env, read=cli, ready=prove_ready, *, preview=False):
    transport = read
    started = time.monotonic()
    calls = 0

    def bounded_read(args, **kwargs):
        nonlocal calls
        calls += 1
        require(calls <= 64 and time.monotonic() - started < 120, "Private API admission exhausted its bounded read budget.")
        remaining = 120 - (time.monotonic() - started)
        require(remaining > 0, "Private API admission exhausted its bounded read budget.")
        kwargs["timeout"] = min(kwargs.get("timeout", 45), remaining)
        result = transport(args, **kwargs)
        require(time.monotonic() - started < 120, "Private API admission exhausted its bounded read budget.")
        return result

    read = bounded_read
    selected = from_environment(env)
    environment = env["AZURE_ENV_NAME"]
    target = api_name(environment, selected)
    subscription = env["AZURE_SUBSCRIPTION_ID"]
    require(re.fullmatch(r"[0-9a-fA-F-]{36}", subscription) is not None, "Explicit subscription required.")
    workload = env.get("AI4IA_WORKLOAD") or "ai4ia"
    require(re.fullmatch(r"[a-z][a-z0-9-]{2,19}", workload) is not None, "Invalid workload token.")
    group = f"rg-{workload}-{environment}"
    scope = ["--subscription", subscription, "-g", group]
    private_name = api_name(environment, "candidate")
    app_read = ["containerapp", "show", *scope, "-n", private_name, "--query", APP_QUERY]
    candidate = read(app_read, absent=True)
    if selected == "legacy":
        require(candidate is None, "Private API exists; refuse an implicit return to the legacy target.")
        return {"AZURE_API_APP_NAME": target}
    require(selected == "private" or env.get("AI4IA_DEPLOY_VERIFY_CANARY") == "false",
            "Private recovery needs explicit no-model-call deployment acceptance (canary=false).")
    expected_vnet = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.Network/virtualNetworks/vnet-{workload}-{environment}-api-private"
    networks = read(["network", "vnet", "list", "--subscription", subscription, "--query",
                     "[].{id:id,prefixes:addressSpace.addressPrefixes,peerings:virtualNetworkPeerings}"])
    require(isinstance(networks, list) and len(networks) <= 128, "VNet address inventory is incomplete or oversized.")
    space = ipaddress.IPv4Network(env["AI4IA_API_PRIVATE_VNET_PREFIX"])
    for network in networks:
        if network["id"].casefold() == expected_vnet.casefold():
            require(network["prefixes"] == [str(space)] and network["peerings"] == [],
                    "Existing private VNet differs from the reviewed standalone address plan.")
        else:
            require(all(not space.overlaps(ipaddress.ip_network(prefix)) for prefix in network["prefixes"]),
                    "Private API address plan overlaps an observed subscription VNet.")
    legacy_query = ["containerapp", "show", *scope, "-n", api_name(environment, "legacy"), "--query", APP_QUERY]
    legacy = read(legacy_query)
    legacy_revision = legacy["properties"]["latestReadyRevisionName"]
    require(bool(legacy_revision), "Legacy API configuration has no exact source revision.")
    old_detail = read(["containerapp", "revision", "show", *scope, "-n", api_name(environment, "legacy"),
                       "--revision", legacy_revision, "--query", REVISION_QUERY])
    old_values = {row["name"]: row.get("value") for row in old_detail["properties"]["template"]["containers"][0]["env"]}
    old_cosmos = old_values.get("AI4IA_COSMOS_ENDPOINT")
    if not isinstance(old_cosmos, str) or not old_cosmos:
        raise AdmissionError("Legacy canonical Cosmos target is unknown.")
    host = urlsplit(old_cosmos).hostname or ""
    require(re.fullmatch(r"[a-z0-9-]+\.documents\.azure\.com", host) is not None, "Canonical Cosmos endpoint unknown.")
    cosmos = read(["cosmosdb", "show", *scope, "-n", host.split(".")[0]])
    require(cosmos.get("publicNetworkAccess") == "Disabled" and cosmos.get("disableLocalAuth") is True,
            "Cosmos must remain private and Entra-only before any recovery step.")
    require(read(legacy_query) == legacy, "Legacy API configuration changed during admission.")
    preserved = {}
    web_values = {}
    for service in ("web", "proxy"):
        name = f"ca-{service}-{environment}"
        query = ["containerapp", "show", *scope, "-n", name, "--query", APP_QUERY]
        app = read(query)
        require(app["properties"]["configuration"].get("activeRevisionsMode") == "Single",
                "Private recovery requires stable Single-mode web/proxy image evidence.")
        serving = app["properties"]["latestReadyRevisionName"]
        require(bool(serving) and serving == app["properties"]["latestRevisionName"],
                "Web/proxy serving revision is unsettled; cannot preserve its image.")
        detail = read(["containerapp", "revision", "show", *scope, "-n", name,
                       "--revision", serving, "--query", REVISION_QUERY])["properties"]
        require(detail.get("active") is True and detail.get("healthState") == "Healthy"
                and detail.get("provisioningState") == "Provisioned"
                and len(detail["template"]["containers"]) == 1, "Web/proxy is not healthy.")
        image = detail["template"]["containers"][0]["image"]
        if service == "web":
            web_values = {row["name"]: row.get("value") for row in detail["template"]["containers"][0]["env"]}
        require(re.fullmatch(rf"[a-z0-9]+\.azurecr\.io/ai4ia/{service}-{re.escape(environment)}@sha256:[0-9a-f]{{64}}", image) is not None,
                "Web/proxy must have an exact serving release digest.")
        require(read(query) == app, "Web/proxy changed during image capture.")
        preserved[f"AI4IA_PRIVATE_{service.upper()}_IMAGE"] = image
    legacy_origin = "https://" + legacy["properties"]["configuration"]["ingress"]["fqdn"]
    candidate_origin = None if candidate is None else "https://" + candidate["properties"]["configuration"]["ingress"]["fqdn"]
    allowed_origins = {legacy_origin}
    if selected == "private" and candidate_origin:
        allowed_origins.add(candidate_origin)
    require(web_values.get("API_BASE_URL") in allowed_origins
            and web_values.get("API_PUBLIC_URL", web_values.get("API_BASE_URL")) == web_values.get("API_BASE_URL"),
            "Web/voice source target is unknown or this phase would undo a completed cutover.")
    if selected == "private" and web_values.get("API_BASE_URL") == legacy_origin:
        require(env.get("AI4IA_DEPLOY_VERIFY_CANARY") == "false",
                "Initial private cutover requires nonbillable acceptance, not a model canary.")
    if candidate is None:
        require(selected == "candidate", "Stage and verify the private candidate before activation/cutover.")
        return {"AZURE_API_APP_NAME": target, "AI4IA_API_PRIVATE_INITIAL_IMAGE": PLACEHOLDER,
                "AI4IA_PRIVATE_MIGRATION_PENDING": "true", "AI4IA_PRIVATE_EXECUTION_BLOCKERS": "[]",
                "AI4IA_PRIVATE_COSMOS_ENDPOINT": old_cosmos, "AI4IA_PRIVATE_API_ORIGIN": "", **preserved}
    props = candidate["properties"]
    require(props["configuration"].get("activeRevisionsMode") == "Single",
            "Private API must retain Single revision mode.")
    expected_env = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.App/managedEnvironments/cae-{workload}-{environment}-api-private"
    require(props["managedEnvironmentId"].casefold() == expected_env.casefold(), "Candidate environment mismatch.")
    identity = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-api-{environment}"
    candidate_identity = dict(candidate["identity"])
    legacy_identity = dict(legacy["identity"])
    for observed in (candidate_identity, legacy_identity):
        assigned = observed["userAssignedIdentities"]
        require(isinstance(assigned, dict) and len(assigned) == 1
                and all(isinstance(key, str) for key in assigned), "API identity inventory is not exact.")
        observed["userAssignedIdentities"] = {key.casefold(): value for key, value in assigned.items()}
    require(candidate_identity.get("type") == "UserAssigned"
            and list(candidate_identity["userAssignedIdentities"]) == [identity.casefold()]
            and candidate_identity == legacy_identity,
            "Candidate must reuse only the existing API managed identity.")
    revision = props.get("latestRevisionName")
    require(bool(revision), "Candidate revision is unknown.")
    detail = read(["containerapp", "revision", "show", *scope, "-n", private_name,
                   "--revision", revision, "--query", REVISION_QUERY])
    current = detail["properties"]
    containers = current["template"]["containers"]
    require(len(containers) == 1, "Unexpected API container inventory.")
    image = containers[0]["image"]
    values = {row["name"]: row.get("value") for row in containers[0]["env"]}
    require(values.get("AI4IA_PRIVATE_COSMOS_REQUIRED", "").lower() == "true",
            "Candidate must prove private DNS as part of actual runtime readiness.")
    require(values.get("AI4IA_COSMOS_ENDPOINT") == old_cosmos, "Candidate changed the canonical Cosmos account.")
    mode = values.get("AI4IA_PRIVATE_RECOVERY_CANDIDATE")
    require(mode in ("true", "false"), "Candidate work-admission mode is unknown.")
    staged = mode == "true"
    placeholder = image == PLACEHOLDER
    if placeholder:
        require(selected == "candidate" and staged, "An inert first-stage placeholder cannot activate or cut over.")
    else:
        require(re.fullmatch(rf"[a-z0-9]+\.azurecr\.io/ai4ia/api-{re.escape(environment)}@sha256:[0-9a-f]{{64}}", image) is not None,
                "Candidate image must be an immutable existing ACR release.")
        require(revision == props.get("latestReadyRevisionName"), "Candidate has pending or unready revision.")
        require(current.get("active") is True and current.get("healthState") == "Healthy"
                and current.get("provisioningState") == "Provisioned", "Candidate is not healthy/provisioned.")
    blockers = []
    inventory_proof = None
    if selected == "candidate":
        require(staged, "Refuse to demote an active private API through candidate staging.")
    else:
        inventory = legacy_revision_inventory(subscription, group, api_name(environment, "legacy"), read,
                                               require_drained=not preview)
        inventory_proof = inventory["proof"]
        if inventory_proof["activeRevisions"]:
            require(inventory_proof["activeRevisions"] == [legacy_revision], "Unapproved legacy revision is active.")
            blockers.append("legacy_revisions_active")
        if selected == "private":
            require(preview or not staged, "Activate and verify private API before web/voice endpoint cutover.")
            if staged:
                blockers.append("candidate_not_activated")
    network = read(["containerapp", "env", "show", *scope, "-n", f"cae-{workload}-{environment}-api-private"])
    subnet = network["properties"]["vnetConfiguration"]["infrastructureSubnetId"]
    require(subnet.casefold() == (expected_vnet + "/subnets/api").casefold(), "Candidate is not in the exact private VNet.")
    host = urlsplit(values.get("AI4IA_COSMOS_ENDPOINT", "")).hostname or ""
    require(re.fullmatch(r"[a-z0-9-]+\.documents\.azure\.com", host) is not None, "Candidate Cosmos endpoint unknown.")
    endpoint = read(["network", "private-endpoint", "show", *scope, "-n", f"pe-{workload}-{environment}-api-private-cosmos"])
    connections = endpoint["privateLinkServiceConnections"]
    require(len(connections) == 1 and connections[0]["privateLinkServiceId"].casefold() == cosmos["id"].casefold()
            and connections[0]["groupIds"] == ["Sql"]
            and connections[0]["privateLinkServiceConnectionState"]["status"] == "Approved",
            "Exact Cosmos Sql private endpoint is not approved.")
    groups = read(["network", "private-endpoint", "dns-zone-group", "list", *scope,
                   "--endpoint-name", f"pe-{workload}-{environment}-api-private-cosmos"])
    expected_zone = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.Network/privateDnsZones/privatelink.documents.azure.com"
    require(isinstance(groups, list) and len(groups) == 1 and
            [c["privateDnsZoneId"].casefold() for c in groups[0]["privateDnsZoneConfigs"]] == [expected_zone.casefold()],
            "Cosmos private endpoint DNS zone group is missing or mismatched.")
    links = read(["network", "private-dns", "link", "vnet", "list", *scope,
                  "-z", "privatelink.documents.azure.com"])
    matches = [link for link in links if link["virtualNetwork"]["id"].casefold() == expected_vnet.casefold()]
    require(len(matches) == 1 and matches[0].get("registrationEnabled") is False
            and matches[0].get("virtualNetworkLinkState") == "Completed", "Private DNS VNet link is not ready.")
    origin = "https://" + props["configuration"]["ingress"]["fqdn"]
    require(re.fullmatch(re.escape(private_name) + r"\.[a-z0-9.-]+\.azurecontainerapps\.io",
                         props["configuration"]["ingress"]["fqdn"]) is not None,
            "Candidate public ingress identity is invalid.")
    if not placeholder:
        require(ready(origin), "Candidate runtime cannot prove Cosmos readiness.")
    again = read(app_read)
    require(again == candidate, "Candidate changed during admission; stop without mutation.")
    completed = selected == "private" and not staged and not blockers and web_values["API_BASE_URL"] == origin
    return {"AZURE_API_APP_NAME": target, "AI4IA_API_PRIVATE_INITIAL_IMAGE": image,
            "AI4IA_PRIVATE_MIGRATION_PENDING": "false" if completed else "true",
            "AI4IA_PRIVATE_EXECUTION_BLOCKERS": json.dumps(blockers, separators=(",", ":")),
            "AI4IA_PRIVATE_COSMOS_ENDPOINT": old_cosmos, "AI4IA_PRIVATE_API_ORIGIN": origin,
            **({"AI4IA_PRIVATE_LEGACY_INVENTORY": json.dumps(inventory_proof, separators=(",", ":"))} if inventory_proof else {}),
            **preserved}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--azd-env", action="store_true", help="Persist validated nonsecret target/image to local azd state.")
    parser.add_argument("--release-mode", action="store_true", help="Read-only trusted-main operation selection.")
    args = parser.parse_args()
    try:
        require(not (args.azd_env and args.release_mode), "Release selection cannot replace a strict azd hook.")
        values = admission(os.environ, preview=args.release_mode)
        if args.release_mode:
            pending = values.get("AI4IA_PRIVATE_MIGRATION_PENDING")
            require(pending in ("true", "false"), "Private migration completion is unknown.")
            blockers = json.loads(values["AI4IA_PRIVATE_EXECUTION_BLOCKERS"])
            operation = deployment_operation(os.environ, pending=pending == "true", blocked=bool(blockers))
            target = os.environ.get("GITHUB_OUTPUT")
            if not target:
                raise AdmissionError("Release selection requires workflow outputs.")
            with Path(target).open("a", encoding="utf-8") as file:
                file.write(f"operation={operation}\n")
                file.write(f"migration_pending={pending}\n")
                file.write(f"api_app_name={values['AZURE_API_APP_NAME']}\n")
                file.write(f"execution_blockers={values['AI4IA_PRIVATE_EXECUTION_BLOCKERS']}\n")
        if args.azd_env and any(os.environ.get(key) != value for key, value in values.items()):
            descriptor, path = tempfile.mkstemp(prefix="ai4ia-api-target-", suffix=".env")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    for key, value in values.items():
                        file.write(f"{key}='{value}'\n")
                result = subprocess.run(
                    ["azd", "env", "set", "--file", path, "--environment", os.environ["AZURE_ENV_NAME"]],
                    capture_output=True, timeout=30, check=False,
                )
                require(result.returncode == 0, "Cannot persist validated azd target/image.")
            finally:
                os.unlink(path)
        output = os.environ.get("GITHUB_ENV")
        if output:
            with Path(output).open("a", encoding="utf-8") as file:
                for key, value in values.items():
                    file.write(f"{key}={value}\n")
        print("Private API phase and exact deployment target validated; no cloud mutations.")
        return 0
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Private API preflight refused: {exc if isinstance(exc, AdmissionError) else 'invalid or incomplete metadata'}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
