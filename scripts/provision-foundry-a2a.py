#!/usr/bin/env python3
"""Expose a Foundry agent over A2A and front it through APIM (docs/foundry-toolbox.md P7).

Operator-run, provisioning-time companion to the A2A plan. It does NOT run during `azd up`,
in CI, or in the app runtime.

The A2A ([Agent2Agent](https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint))
endpoint of a deployed Foundry agent is fronted through APIM using the SAME pattern the toolbox
bridge uses: APIM injects the Foundry managed-identity bearer for ``https://ai.azure.com`` so
callers present only the APIM subscription key -- one auth path, gated on APIM.

What it does
------------
1. Loads + validates a foundry/a2a/*.a2a.json manifest (default: example.a2a.json).
2. Resolves the primary project endpoint and the A2A APIM gateway base URL.
3. Prints the *plan*: the raw A2A endpoint, the APIM **consumer** URL the app calls, and an
   ``agents.json`` AgentSpec stub so the remote agent can be linked (agent-as-tool seam).
4. With ``--emit-az``, prints ready-to-review ``az`` commands to enable the A2A endpoint and to
   register the APIM API + backend + MI-bearer policy that front it.

Runtime consumption (adding the stub to agents.json and calling the APIM-fronted endpoint) is an
operator step; A2A is Azure public preview. Nothing here is executed against Azure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "foundry" / "a2a" / "example.a2a.json"

# Same fixed contract the toolbox bridge uses; APIM injects these so callers need only the key.
A2A_MI_RESOURCE = "https://ai.azure.com"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")


# --------------------------------------------------------------------------------------
# Pure helpers (no Azure SDK import; unit-tested offline)
# --------------------------------------------------------------------------------------
def load_manifest(path: Path) -> dict[str, Any]:
    """Load + parse the A2A manifest JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors (empty => valid)."""
    errors: list[str] = []
    if not manifest.get("agentName"):
        errors.append("`agentName` is required (the deployed Foundry agent to expose over A2A).")
    if not manifest.get("displayName"):
        errors.append("`displayName` is required.")
    if not manifest.get("description"):
        errors.append("`description` is required.")
    link_as = manifest.get("linkAs")
    if not isinstance(link_as, str) or not _SLUG_RE.match(link_as):
        errors.append("`linkAs` must be a lowercase slug matching ^[a-z][a-z0-9-]{0,38}[a-z0-9]$.")
    return errors


def a2a_endpoint(project_endpoint: str, agent_name: str) -> str:
    """The raw Foundry A2A endpoint for the agent (minted when A2A is enabled)."""
    return f"{project_endpoint.rstrip('/')}/agents/{agent_name}/a2a"


def consumer_url(gateway_url: str, agent_name: str) -> str:
    """The APIM consumer URL the app calls: https://<gateway>/a2a/<agent_name>."""
    return f"{gateway_url.rstrip('/')}/a2a/{agent_name}"


def build_agent_link(manifest: dict[str, Any]) -> dict[str, Any]:
    """Project the manifest to an agents.json AgentSpec stub for the links (agent-as-tool) seam.

    Operators add this to app/api/src/ai4ia_api/data/agents.json so other agents can delegate to
    the remote A2A agent by listing `linkAs` in their `links`. Kept minimal: no systemPrompt/tools
    are assumed (the remote agent owns its own behavior).
    """
    return {
        "name": manifest["linkAs"],
        "displayName": manifest["displayName"],
        "description": manifest["description"],
        "systemPrompt": f"Delegated remote Foundry agent '{manifest['agentName']}', reached over A2A through APIM.",
        "tools": [],
        "links": [],
        "enabled": True,
    }


def to_az_commands(manifest: dict[str, Any], gateway_host: str, apim_name: str, resource_group: str, raw_endpoint: str) -> list[str]:
    """Render reviewable `az` commands to enable A2A and front it through APIM.

    Two stages: (1) enable the A2A endpoint on the deployed agent (CLI/portal/SDK), then (2) create
    the APIM API + backend + inbound MI-bearer policy that front the raw endpoint. The policy fragment
    mirrors the toolbox bridge (authentication-managed-identity for https://ai.azure.com).
    """
    agent = manifest["agentName"]
    api_id = f"a2a-{agent}"
    return [
        f"# 1) Enable the A2A endpoint on the deployed agent (captures {raw_endpoint})",
        f"az ml agent update --name {json.dumps(agent)} --resource-group {json.dumps(resource_group)} --set a2aEnabled=true",
        "# 2) Front it through APIM (backend -> raw endpoint; inbound policy injects the MI bearer)",
        (
            "az apim api create"
            f" --resource-group {json.dumps(resource_group)}"
            f" --service-name {json.dumps(apim_name)}"
            f" --api-id {json.dumps(api_id)}"
            f" --path {json.dumps(f'a2a/{agent}')}"
            f" --display-name {json.dumps(manifest['displayName'])}"
            f" --service-url {json.dumps(raw_endpoint)}"
            " --protocols https"
        ),
        (
            "# Inbound policy fragment (apply to the API): "
            '<authentication-managed-identity resource="' + A2A_MI_RESOURCE + '" />'
            f"  ->  callers use https://{gateway_host}/a2a/{agent} with only the APIM subscription key."
        ),
    ]


def resolve(arg: str | None, env_var: str, what: str) -> str:
    import os

    value = arg or os.environ.get(env_var)
    if not value:
        raise SystemExit(f"No {what}. Pass the flag or set {env_var}.")
    return value


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Path to a *.a2a.json manifest.")
    parser.add_argument("--project-endpoint", default=None, help="Foundry project endpoint (else AZURE_FOUNDRY_PROJECT_ENDPOINT).")
    parser.add_argument("--gateway-url", default=None, help="A2A APIM gateway base URL (else AZURE_A2A_GATEWAY_URL).")
    parser.add_argument("--apim-name", default=None, help="APIM service name (required for --emit-az).")
    parser.add_argument("--resource-group", default=None, help="Resource group (required for --emit-az).")
    parser.add_argument("--emit-az", action="store_true", help="Print reviewable `az` commands to enable A2A + front it through APIM, then exit.")
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        raise SystemExit(f"Manifest not found: {args.manifest}")
    manifest = load_manifest(args.manifest)

    errors = validate_manifest(manifest)
    if errors:
        print("A2A manifest is not ready:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    endpoint = resolve(args.project_endpoint, "AZURE_FOUNDRY_PROJECT_ENDPOINT", "project endpoint")
    gateway = resolve(args.gateway_url, "AZURE_A2A_GATEWAY_URL", "A2A APIM gateway URL")
    raw = a2a_endpoint(endpoint, manifest["agentName"])
    fronted = consumer_url(gateway, manifest["agentName"])

    if args.emit_az:
        apim = resolve(args.apim_name, "AZURE_APIM_NAME", "APIM service name")
        rg = resolve(args.resource_group, "AZURE_RESOURCE_GROUP", "resource group")
        host = gateway.rstrip("/").split("://")[-1]
        for line in to_az_commands(manifest, host, apim, rg, raw):
            print(line)
        return 0

    print(f"Agent (A2A)       : {manifest['agentName']}")
    print(f"Project endpoint  : {endpoint}")
    print(f"Raw A2A endpoint  : {raw}")
    print(f"APIM consumer URL : {fronted}")
    print("\nAPIM injects the Foundry managed-identity bearer (https://ai.azure.com), so callers")
    print("present only the APIM subscription key -- same governance as the toolbox bridge.")
    print("\nAdd this AgentSpec to app/api/src/ai4ia_api/data/agents.json, then reference")
    print(f"'{manifest['linkAs']}' in another agent's `links` to delegate to it:\n")
    print(json.dumps(build_agent_link(manifest), indent=2))
    print("\n(dry run) Re-run with --emit-az to print the enable + APIM-front commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
