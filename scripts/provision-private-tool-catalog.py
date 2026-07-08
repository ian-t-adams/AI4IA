#!/usr/bin/env python3
"""Register AI4IA's APIM-fronted MCP servers in an Azure API Center private tool catalog.

This is the operator-run, provisioning-time companion to the private tool
catalog described in docs/foundry-toolbox.md. The Bicep default is safe-off, but this repo's
live posture enables API Center; the script still does NOT run during `azd up`, in CI, or in
the app runtime.

What it does
------------
1. Loads infra/mcp-servers.json (the curated "official" MCP servers).
2. For each server, computes the **APIM consumer URL** the app actually calls
   (``https://<mcp-apim-gateway>/<name>/mcp``) -- i.e. the governed front door, not the raw
   upstream. Cataloging the APIM URL is the whole point: discovery + governance stay on the
   proxy.
3. Prints the *plan*: one MCP asset per server (api-id, title, MCP URL) and, with
   ``--emit-az``, ready-to-run ``az apic api create`` commands that register them in the API
   Center (which integrates with Microsoft Foundry private tool catalogs).
4. With ``--create``, registers them via the ``azure-mgmt-apicenter`` SDK (optional; install
   the ``foundry`` dependency group). Without it, the script is a safe offline dry run.

Inputs (fail-closed): the API Center name (``--api-center`` or ``AZURE_API_CENTER_NAME``, the
azd output emitted when enablePrivateToolCatalog=true) and the MCP APIM gateway base URL
(``--gateway-url`` or ``AZURE_OFFICIAL_MCP_GATEWAY_URL``). MCP-asset registration is public
preview; do not use in production without validation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPO_ROOT / "infra" / "mcp-servers.json"

# API Center currently supports a single default workspace for all child assets.
DEFAULT_WORKSPACE = "default"
# MCP is registered as a preview API "kind" in API Center.
MCP_KIND = "mcp"


# --------------------------------------------------------------------------------------
# Pure helpers (no Azure SDK; unit-tested offline)
# --------------------------------------------------------------------------------------
def load_servers(path: Path) -> list[dict[str, Any]]:
    """Load the servers[] array from infra/mcp-servers.json."""
    return json.loads(path.read_text(encoding="utf-8")).get("servers") or []


def consumer_url(gateway_url: str, name: str) -> str:
    """The APIM consumer MCP URL the app calls: https://<gateway>/<name>/mcp."""
    return f"{gateway_url.rstrip('/')}/{name}/mcp"


def build_asset(server: dict[str, Any], gateway_url: str) -> dict[str, Any]:
    """Project one mcp-servers.json entry to an API Center MCP asset descriptor."""
    name = server["name"]
    return {
        "apiId": name,
        "title": server.get("displayName") or name,
        "kind": MCP_KIND,
        "description": server.get("description", ""),
        "mcpUrl": consumer_url(gateway_url, name),
        "lifecycleStage": "preview",
    }


def plan_assets(servers: list[dict[str, Any]], gateway_url: str) -> list[dict[str, Any]]:
    """Project every server to its catalog asset (order preserved)."""
    return [build_asset(s, gateway_url) for s in servers]


def to_az_command(asset: dict[str, Any], api_center: str, resource_group: str) -> str:
    """Render a ready-to-run `az apic api create` command that registers one MCP asset.

    MCP is a preview API kind; the command groups discovery + governance on the APIM URL.
    """
    return (
        "az apic api create"
        f" --resource-group {json.dumps(resource_group)}"
        f" --service-name {json.dumps(api_center)}"
        f" --workspace-name {DEFAULT_WORKSPACE}"
        f" --api-id {json.dumps(asset['apiId'])}"
        f" --title {json.dumps(asset['title'])}"
        f" --type {asset['kind']}"
        f" --custom-properties {json.dumps(json.dumps({'mcpUrl': asset['mcpUrl']}))}"
    )


def resolve(arg: str | None, env_var: str, what: str) -> str:
    import os

    value = arg or os.environ.get(env_var)
    if not value:
        raise SystemExit(
            f"No {what}. Pass the flag or set {env_var} "
            "(emitted by `azd env get-values` when enablePrivateToolCatalog=true / enableOfficialMcp=true)."
        )
    return value


# --------------------------------------------------------------------------------------
# Live path (isolated Azure SDK import; requires the optional `foundry` dependency group)
# --------------------------------------------------------------------------------------
def register_assets(
    assets: list[dict[str, Any]], api_center: str, resource_group: str, subscription_id: str
) -> None:  # pragma: no cover - exercised only on live provisioning
    """Register each MCP asset via azure-mgmt-apicenter. Imported lazily so dry runs need no SDK."""
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.apicenter import ApiCenterMgmtClient
        from azure.mgmt.apicenter.models import Api, ApiProperties
    except ImportError as exc:
        raise SystemExit(
            "azure-mgmt-apicenter is not installed. Install the optional provisioning group:\n"
            '  uv pip install -e "app/api[foundry]"   # or: pip install azure-mgmt-apicenter azure-identity\n'
            "Alternatively re-run with --emit-az and run the printed `az apic` commands."
        ) from exc

    client = ApiCenterMgmtClient(DefaultAzureCredential(), subscription_id)
    for asset in assets:
        # The MCP URL is carried in the description so the asset is self-describing without
        # requiring a pre-defined custom-property metadata schema on the API Center.
        description = f"{asset['description']} (MCP endpoint: {asset['mcpUrl']})".strip()
        client.apis.create_or_update(
            resource_group_name=resource_group,
            service_name=api_center,
            workspace_name=DEFAULT_WORKSPACE,
            api_name=asset["apiId"],
            resource=Api(
                properties=ApiProperties(
                    title=asset["title"],
                    kind=asset["kind"],
                    description=description,
                )
            ),
        )
        print(f"Registered '{asset['apiId']}' -> {asset['mcpUrl']}")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="Path to infra/mcp-servers.json.")
    parser.add_argument("--api-center", default=None, help="API Center name (else AZURE_API_CENTER_NAME).")
    parser.add_argument("--gateway-url", default=None, help="MCP APIM gateway base URL (else AZURE_OFFICIAL_MCP_GATEWAY_URL).")
    parser.add_argument("--resource-group", default=None, help="Resource group of the API Center (required for --emit-az / --create).")
    parser.add_argument("--subscription-id", default=None, help="Subscription ID (required for --create).")
    parser.add_argument("--emit-az", action="store_true", help="Print ready-to-run `az apic api create` commands and exit.")
    parser.add_argument("--create", action="store_true", help="Actually register assets via azure-mgmt-apicenter (needs the SDK). Default is a dry run.")
    args = parser.parse_args(argv)

    if not args.catalog.exists():
        raise SystemExit(f"Catalog not found: {args.catalog}")
    servers = load_servers(args.catalog)
    if not servers:
        print(f"No servers in {args.catalog}. Nothing to register.")
        return 0

    gateway_url = resolve(args.gateway_url, "AZURE_OFFICIAL_MCP_GATEWAY_URL", "MCP APIM gateway URL")
    assets = plan_assets(servers, gateway_url)

    if args.emit_az:
        rg = resolve(args.resource_group, "AZURE_RESOURCE_GROUP", "resource group")
        api_center = resolve(args.api_center, "AZURE_API_CENTER_NAME", "API Center name")
        for asset in assets:
            print(to_az_command(asset, api_center, rg))
        return 0

    api_center = resolve(args.api_center, "AZURE_API_CENTER_NAME", "API Center name")
    print(f"API Center       : {api_center}")
    print(f"Gateway URL      : {gateway_url}")
    print(f"Assets ({len(assets)})       :")
    for asset in assets:
        print(f"  - {asset['apiId']:<24} {asset['mcpUrl']}")

    if not args.create:
        print("\n(dry run) Re-run with --emit-az to print `az apic` commands, or --create to register")
        print("them via the SDK. Registering here makes the APIM-fronted MCP servers discoverable in")
        print("the API Center private tool catalog (and to Foundry private tool catalogs).")
        return 0

    rg = resolve(args.resource_group, "AZURE_RESOURCE_GROUP", "resource group")
    subscription_id = resolve(args.subscription_id, "AZURE_SUBSCRIPTION_ID", "subscription ID")
    register_assets(assets, api_center, rg, subscription_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
