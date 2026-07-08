#!/usr/bin/env python3
"""Provision the AI4IA shared Foundry Agent Service toolbox from foundry/toolbox.manifest.json.

This is the operator-run, provisioning-time companion to the Foundry-toolbox
bridge described in docs/foundry-toolbox.md. It does NOT run during `azd up`, in CI, or in
the app runtime; `foundry/toolbox.manifest.json` is the canonical live `ai4ia-toolbox`
definition a new environment reproduces after infrastructure deploy.

What it does
------------
1. Loads + validates foundry/toolbox.manifest.json.
2. Resolves the primary project endpoint (``--project-endpoint`` or the
   ``AZURE_FOUNDRY_PROJECT_ENDPOINT`` azd output).
3. Prints the *plan*: the tools it will create, the toolbox **consumer** MCP URL, and a
   ready-to-paste ``infra/mcp-servers.json`` entry that routes the toolbox through the
   existing official-MCP APIM (managed-identity bearer + the ``Foundry-Features`` header +
   ``api-version=v1`` query the toolbox endpoint requires).
4. With ``--create``, calls ``project.toolboxes.create_version(...)`` via the
   ``azure-ai-projects`` SDK (install the optional dependency group: ``foundry``).
   Without it, the script is a dry run (safe, offline, no Azure calls).
5. With ``--emit-yaml PATH``, writes the equivalent ``azd ai toolbox create --from-file``
   YAML so operators who prefer the CLI path do not hand-author it.

The bridge, in one line: the toolbox is consumed as a single "official MCP server", so the
app needs ZERO new runtime code — it reuses the OfficialMcpService + tool picker shipped in
the official MCP plane. Everything is public preview; do not use in production without validation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "foundry" / "toolbox.manifest.json"

# The toolbox endpoint's fixed contract (see docs/foundry-toolbox.md). APIM injects these
# so the app's OfficialMcpService can consume the toolbox like any other official MCP server.
TOOLBOX_MI_RESOURCE = "https://ai.azure.com"
TOOLBOX_FEATURES_HEADER = {"Foundry-Features": "Toolboxes=V1Preview"}
TOOLBOX_API_VERSION_QUERY = {"api-version": "v1"}

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")  # matches infra/mcp-servers.schema.json
# Toolbox tool types that can actually be placed in a toolbox via azure-ai-projects, mapped to
# their discriminated model classes (resolved lazily in the live path so the pure functions stay
# dependency-free). NOTE: `computer_use` and `bing_custom_search` exist only as AGENT-level tools
# in the SDK (ComputerUsePreviewTool / BingCustomSearchPreviewTool); there is no matching
# *ToolboxTool, so they cannot go in a toolbox and are intentionally absent here. Browser automation
# is spelled `browser_automation_preview` to match the SDK discriminator.
_TYPE_TO_MODEL = {
    "web_search": "WebSearchToolboxTool",
    "azure_ai_search": "AzureAISearchToolboxTool",
    "code_interpreter": "CodeInterpreterToolboxTool",
    "file_search": "FileSearchToolboxTool",
    "browser_automation_preview": "BrowserAutomationPreviewToolboxTool",
    "openapi": "OpenApiToolboxTool",
    "toolbox_search_preview": "ToolboxSearchPreviewToolboxTool",
    "mcp": "MCPToolboxTool",
}
_ALLOWED_TOOL_TYPES = set(_TYPE_TO_MODEL)
# camelCase manifest keys -> snake_case payload keys (only the ones that differ).
_CAMEL_TO_SNAKE = {
    "serverLabel": "server_label",
    "serverUrl": "server_url",
    "requireApproval": "require_approval",
    "projectConnectionId": "project_connection_id",
    "azureAiSearch": "azure_ai_search",
    "indexName": "index_name",
    "customSearchConfiguration": "custom_search_configuration",
    "instanceName": "instance_name",
}


# --------------------------------------------------------------------------------------
# Pure helpers (no Azure SDK import; unit-tested offline)
# --------------------------------------------------------------------------------------
def load_manifest(path: Path) -> dict[str, Any]:
    """Load + parse the toolbox manifest JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty => valid to provision)."""
    errors: list[str] = []
    name = manifest.get("name")
    if not isinstance(name, str) or not _SLUG_RE.match(name):
        errors.append("`name` must be a lowercase slug matching ^[a-z][a-z0-9-]{0,38}[a-z0-9]$ (2-40 chars, starts with a letter).")
    if not manifest.get("description"):
        errors.append("`description` is required and must be non-empty.")

    tools = manifest.get("tools") or []
    skills = manifest.get("skills") or []
    connections = manifest.get("connections") or []
    if not (tools or skills or connections):
        errors.append(
            "manifest is inert: add at least one of `tools`, `skills`, or `connections` "
            "before provisioning (a toolbox must contain at least one item)."
        )

    unnamed = 0
    for i, tool in enumerate(tools):
        ttype = tool.get("type")
        if ttype not in _ALLOWED_TOOL_TYPES:
            errors.append(f"tools[{i}].type '{ttype}' is not one of {sorted(_ALLOWED_TOOL_TYPES)}.")
            continue
        # The service identifies a tool by `name` OR `serverLabel` (mcp tools use the latter).
        if not (tool.get("name") or tool.get("serverLabel")):
            unnamed += 1
    if unnamed > 1:
        errors.append(
            f"{unnamed} tools have no identifier; the service allows at most ONE tool total without "
            "a `name` (or `serverLabel` for mcp) -- give every other tool a unique `name`."
        )
    return errors


def _to_snake(key: str) -> str:
    return _CAMEL_TO_SNAKE.get(key, key)


def _convert_keys(value: Any) -> Any:
    """Recursively rewrite camelCase manifest keys to the API's snake_case."""
    if isinstance(value, dict):
        return {_to_snake(k): _convert_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert_keys(v) for v in value]
    return value


def plan_tools(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Project manifest tools to the REST/SDK payload shape (snake_case, `type` first)."""
    planned: list[dict[str, Any]] = []
    for tool in manifest.get("tools") or []:
        planned.append(_convert_keys(tool))
    return planned


def consumer_mcp_url(project_endpoint: str, name: str) -> str:
    """The toolbox *consumer* endpoint (always serves default_version)."""
    return f"{project_endpoint.rstrip('/')}/toolboxes/{name}/mcp?api-version=v1"


def build_mcp_server_entry(manifest: dict[str, Any], project_endpoint: str) -> dict[str, Any]:
    """Project the toolbox to a PORTABLE infra/mcp-servers.json entry (routed via the official-MCP APIM).

    The entry sets ``foundryToolbox: true`` and deliberately OMITS ``upstreamUrl`` so the catalog is
    not pinned to one project/tenant: main.bicep computes the URL from the deployed primary project
    endpoint (``<projectEndpoint>/toolboxes/<name>/mcp``). The APIM policy adds the managed-identity
    bearer, the static Foundry-Features header, and the api-version=v1 query. ``project_endpoint`` is
    accepted for signature stability and shown in the dry-run plan, but is not baked into the entry.
    """
    name = manifest["name"]
    return {
        "name": name,
        "displayName": f"Foundry toolbox: {name}",
        "description": manifest.get("description", ""),
        "foundryToolbox": True,
        "upstreamAuthMode": "managed_identity",
        "upstreamMiResource": TOOLBOX_MI_RESOURCE,
        "upstreamHeaders": dict(TOOLBOX_FEATURES_HEADER),
        "upstreamQueryParams": dict(TOOLBOX_API_VERSION_QUERY),
    }


def to_azd_yaml(manifest: dict[str, Any]) -> str:
    """Render the `azd ai toolbox create --from-file` YAML equivalent (no PyYAML dependency).

    Emits only the subset AI4IA uses; connection/tool credentials are never embedded.
    """
    lines: list[str] = [f"description: {json.dumps(manifest.get('description', ''))}"]
    connections = manifest.get("connections") or []
    if connections:
        lines.append("connections:")
        for c in connections:
            lines.append(f"  - name: {json.dumps(c['name'])}")
    tools = plan_tools(manifest)
    if tools:
        lines.append("tools:")
        for t in tools:
            lines.append(f"  - type: {t['type']}")
            for k, v in t.items():
                if k == "type":
                    continue
                lines.append(f"    {k}: {json.dumps(v)}")
    skills = manifest.get("skills") or []
    if skills:
        lines.append("skills:")
        for s in skills:
            lines.append(f"  - name: {json.dumps(s['name'])}")
            if s.get("version"):
                lines.append(f"    version: {json.dumps(str(s['version']))}")
    rai = manifest.get("raiPolicyName")
    if rai:
        lines += ["policies:", "  rai_config:", f"    rai_policy_name: {json.dumps(rai)}"]
    return "\n".join(lines) + "\n"


def resolve_project_endpoint(arg: str | None) -> str:
    import os

    endpoint = arg or os.environ.get("AZURE_FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise SystemExit(
            "No project endpoint. Pass --project-endpoint or set AZURE_FOUNDRY_PROJECT_ENDPOINT "
            "(emitted by `azd env get-values` when enableFoundryToolbox=true)."
        )
    return endpoint


# --------------------------------------------------------------------------------------
# Live path (isolated Azure SDK import; requires the optional `foundry` dependency group)
# --------------------------------------------------------------------------------------
def create_toolbox(manifest: dict[str, Any], project_endpoint: str) -> Any:
    """Create a toolbox version via azure-ai-projects. Imported lazily so dry runs need no SDK.

    Uses the real SDK surface: ``client.toolboxes.create_version(name, *, tools=[<typed
    ToolboxTool subclasses>], description, skills=[ToolboxSkillReference], policies=ToolboxPolicies)``.
    Each manifest tool maps to its discriminated model class (``_TYPE_TO_MODEL``); type-specific
    fields are passed through (snake_cased) best-effort.
    """
    try:
        from azure.ai.projects import AIProjectClient
        from azure.ai.projects import models as m
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - exercised only on live provisioning
        raise SystemExit(
            "azure-ai-projects is not installed. Install the optional provisioning group:\n"
            '  uv pip install -e "app/api[foundry]"   # or: pip install azure-ai-projects azure-identity'
        ) from exc

    project = AIProjectClient(endpoint=project_endpoint, credential=DefaultAzureCredential())

    tools: list[Any] = []
    for tool in manifest.get("tools") or []:
        cls_name = _TYPE_TO_MODEL.get(tool["type"])
        if cls_name is None:  # pragma: no cover - guarded earlier by validate_manifest
            raise SystemExit(
                f"tool type '{tool['type']}' cannot be placed in a toolbox via azure-ai-projects "
                f"(creatable types: {sorted(_ALLOWED_TOOL_TYPES)})."
            )
        model_cls = getattr(m, cls_name)
        fields = {_to_snake(k): v for k, v in tool.items() if k != "type"}
        tools.append(model_cls(**fields))

    kwargs: dict[str, Any] = {"tools": tools, "description": manifest.get("description", "")}
    if manifest.get("skills"):
        kwargs["skills"] = [
            m.ToolboxSkillReference(name=s["name"], version=s["version"])
            if s.get("version")
            else m.ToolboxSkillReference(name=s["name"])
            for s in manifest["skills"]
        ]
    if manifest.get("raiPolicyName"):
        kwargs["policies"] = m.ToolboxPolicies(
            rai_config=m.RaiConfig(rai_policy_name=manifest["raiPolicyName"])
        )
    return project.toolboxes.create_version(manifest["name"], **kwargs)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Path to toolbox.manifest.json.")
    parser.add_argument("--project-endpoint", default=None, help="Foundry project endpoint (else AZURE_FOUNDRY_PROJECT_ENDPOINT).")
    parser.add_argument("--create", action="store_true", help="Actually create the toolbox version (needs azure-ai-projects). Default is a dry run.")
    parser.add_argument("--emit-yaml", type=Path, default=None, metavar="PATH", help="Write the `azd ai toolbox create --from-file` YAML and exit.")
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        raise SystemExit(f"Manifest not found: {args.manifest}")
    manifest = load_manifest(args.manifest)

    errors = validate_manifest(manifest)
    if errors:
        print("Manifest is not ready to provision:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if args.emit_yaml:
        args.emit_yaml.write_text(to_azd_yaml(manifest), encoding="utf-8")
        print(f"Wrote azd toolbox YAML -> {args.emit_yaml}")
        return 0

    endpoint = resolve_project_endpoint(args.project_endpoint)
    planned = plan_tools(manifest)
    entry = build_mcp_server_entry(manifest, endpoint)

    print(f"Toolbox           : {manifest['name']}")
    print(f"Project endpoint  : {endpoint}")
    print(f"Consumer MCP URL  : {consumer_mcp_url(endpoint, manifest['name'])}")
    print(f"Tools ({len(planned)})        : {', '.join(t['type'] for t in planned) or '(none)'}")
    print(f"Skills ({len(manifest.get('skills') or [])})       : {', '.join(s['name'] for s in manifest.get('skills') or []) or '(none)'}")
    print("\nAdd this entry to infra/mcp-servers.json (servers[]), then set")
    print("enableOfficialMcp=true + enableFoundryToolbox=true and deploy:\n")
    print(json.dumps(entry, indent=2))

    if not args.create:
        print("\n(dry run) Re-run with --create to create the toolbox version in Foundry.")
        return 0

    result = create_toolbox(manifest, endpoint)
    version = getattr(result, "version", "?")
    print(f"\nCreated toolbox '{manifest['name']}' version {version} (first version becomes default).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
