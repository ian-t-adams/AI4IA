#!/usr/bin/env python3
"""Provision the AI4IA shared Foundry Agent Service toolbox from foundry/toolbox.manifest.json.

This is the operator-run, provisioning-time companion to the Foundry-toolbox
bridge described in docs/foundry-toolbox.md. It does NOT run during `azd up`, in CI, or in
the app runtime; `foundry/toolbox.manifest.json` is the canonical live `ai4ia-toolbox`
definition a new environment reproduces after infrastructure deploy.

What it does
------------
1. Loads + validates foundry/toolbox.manifest.json: hand-written structural checks (name
   pattern, at-most-one-unnamed-tool, ...) plus strict JSON Schema validation against
   foundry/toolbox.manifest.schema.json (required nested fields per tool type, cardinality,
   unknown-property rejection) when the optional ``jsonschema`` package is installed -- it ships
   with the ``foundry`` extra, and ``--create`` refuses to run without it.
2. Resolves the primary project endpoint (``--project-endpoint`` or the
   ``AZURE_FOUNDRY_PROJECT_ENDPOINT`` azd output).
3. Prints the *plan*: the tools it will create, the toolbox **consumer** MCP URL, and a
   ready-to-paste ``infra/mcp-servers.json`` entry that routes the toolbox through the
   existing official-MCP APIM (managed-identity bearer + the ``Foundry-Features`` header +
   ``api-version=v1`` query the toolbox endpoint requires).
4. Validates each active manifest skill from ``foundry/skills/<name>/SKILL.md``.
   With ``--create``, it reconciles those immutable Foundry skill versions first,
   reusing matching versions after interrupted activation instead of creating
   duplicates.
5. Reads the served toolbox default and calls ``project.toolboxes.create_version``
   only when its content differs, via ``azure-ai-projects`` (install the optional
   dependency group: ``foundry``). Without ``--create`` this remains a safe,
   offline dry run.
6. With ``--emit-yaml PATH``, writes the equivalent ``azd ai toolbox create --from-file``
   YAML so operators who prefer the CLI path do not hand-author it.

The bridge, in one line: the toolbox is consumed as a single "official MCP server";
tools reuse the official MCP execution path and skills use its MCP resources through
the governed progressive-disclosure loader. Everything is public preview; do not use
in production without validation.
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "foundry" / "toolbox.manifest.json"
DEFAULT_SKILLS_ROOT = REPO_ROOT / "foundry" / "skills"

# The toolbox endpoint's fixed contract (see docs/foundry-toolbox.md). APIM injects these
# so the app's OfficialMcpService can consume the toolbox like any other official MCP server.
TOOLBOX_MI_RESOURCE = "https://ai.azure.com"
TOOLBOX_FEATURES_HEADER = {
    "Foundry-Features": "Toolboxes=V1Preview,Skills=V1Preview"
}
TOOLBOX_API_VERSION_QUERY = {"api-version": "v1"}
SKILLS_FEATURES_HEADER = {"Foundry-Features": "Skills=V1Preview"}

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,38}[a-z0-9]$")  # matches infra/mcp-servers.schema.json
_SKILL_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SKILL_FRONT_MATTER_RE = re.compile(
    r"\A---\n(?P<header>.*?)\n---\n(?P<body>.*)\Z",
    re.DOTALL,
)
# Toolbox tool types that can actually be placed in a toolbox via azure-ai-projects, mapped to
# their discriminated model classes (resolved lazily in the live path so the pure functions stay
# dependency-free). NOTE: `computer_use` and `bing_custom_search` exist only as AGENT-level tools
# in the SDK (ComputerUsePreviewTool / BingCustomSearchPreviewTool); there is no matching
# *ToolboxTool, so they cannot go in a toolbox and are intentionally absent here. Browser automation
# is spelled `browser_automation_preview` to match the SDK discriminator.
# `toolbox_search` (ToolSearchToolboxTool) arrived in azure-ai-projects 2.4.0 as the GA spelling of
# tool search; 2.4.0 keeps `toolbox_search_preview` (ToolboxSearchPreviewToolboxTool) alongside it
# rather than replacing it, and both are live discriminators the service accepts. Both are mapped
# here deliberately: dropping the preview entry would break already-provisioned manifests, and the
# two carry an identical field set (only the common name/description/toolConfigs), so the GA branch
# in toolbox.manifest.schema.json mirrors the preview branch exactly. Prefer `toolbox_search` in new
# manifests.
_TYPE_TO_MODEL = {
    "web_search": "WebSearchToolboxTool",
    "azure_ai_search": "AzureAISearchToolboxTool",
    "code_interpreter": "CodeInterpreterToolboxTool",
    "file_search": "FileSearchToolboxTool",
    "browser_automation_preview": "BrowserAutomationPreviewToolboxTool",
    "openapi": "OpenApiToolboxTool",
    "toolbox_search_preview": "ToolboxSearchPreviewToolboxTool",
    "toolbox_search": "ToolSearchToolboxTool",
    "mcp": "MCPToolboxTool",
    "a2a_preview": "A2APreviewToolboxTool",
    "fabric_iq_preview": "FabricIQPreviewToolboxTool",
    "reminder_preview": "ReminderPreviewToolboxTool",
    "work_iq_preview": "WorkIQPreviewToolboxTool",
}
_ALLOWED_TOOL_TYPES = set(_TYPE_TO_MODEL)
# camelCase manifest keys -> snake_case payload keys (only the ones that differ).
_CAMEL_TO_SNAKE = {
    "serverLabel": "server_label",
    "serverUrl": "server_url",
    "connectorId": "connector_id",
    "requireApproval": "require_approval",
    "projectConnectionId": "project_connection_id",
    "azureAiSearch": "azure_ai_search",
    "indexName": "index_name",
    "indexAssetId": "index_asset_id",
    "queryType": "query_type",
    "topK": "top_k",
    "customSearchConfiguration": "custom_search_configuration",
    "instanceName": "instance_name",
    "vectorStoreIds": "vector_store_ids",
    "browserAutomationPreview": "browser_automation_preview",
    "fileIds": "file_ids",
    "memoryLimit": "memory_limit",
    "networkPolicy": "network_policy",
    "allowedDomains": "allowed_domains",
    "securityScheme": "security_scheme",
    "defaultParams": "default_params",
    # round 7: common ToolConfig + newly-modeled per-type fields.
    "toolConfigs": "tool_configs",
    "additionalSearchText": "additional_search_text",
    "userLocation": "user_location",
    "searchContextSize": "search_context_size",
    "maxNumResults": "max_num_results",
    "rankingOptions": "ranking_options",
    "scoreThreshold": "score_threshold",
    "hybridSearch": "hybrid_search",
    "embeddingWeight": "embedding_weight",
    "textWeight": "text_weight",
    "serverDescription": "server_description",
    "allowedTools": "allowed_tools",
    "deferLoading": "defer_loading",
    "toolNames": "tool_names",
    "readOnly": "read_only",
    "baseUrl": "base_url",
    "agentCardPath": "agent_card_path",
    "sendCredentialsForAgentCard": "send_credentials_for_agent_card",
}
# `indexAssetId` -> `index_asset_id` (azure-ai-projects 2.4.0 AISearchIndexResource.index_asset_id)
# IS mapped above as a documented, schema-enforced mutually-exclusive alternative to
# indexName+projectConnectionId, even though no current Microsoft Learn doc for the Azure AI
# Search tool demonstrates it (see foundry/toolbox.manifest.schema.json's azureAiSearch.indexes
# description and docs/foundry-toolbox.md).
# Manifest keys whose VALUE is an opaque, externally-authored payload that must be copied
# verbatim -- never key-rewritten -- even though _convert_keys() otherwise recurses through
# every nested dict/list. Keyed by the (parent key, key) pair so only the specific nesting we
# mean is treated as opaque (a "spec" key appearing somewhere unrelated is unaffected).
# `openapi.spec` is an arbitrary OpenAPI/JSON-Schema document describing someone else's API: its
# property/schema names (e.g. a request parameter genuinely named `topK`) are that API's
# contract, not AI4IA toolbox manifest config, so snake-casing them would silently corrupt the
# spec the model calls against.
_OPAQUE_NESTED_KEYS = {("openapi", "spec")}

# Manifest keys whose VALUE is a map with caller-defined, arbitrary keys (tool names, or the `*`
# catch-all) that must survive _convert_keys() verbatim -- never looked up in
# _CAMEL_TO_SNAKE -- even though the map's own VALUES are still AI4IA-shaped config objects that
# must keep being recursed into and snake_cased. This differs from _OPAQUE_NESTED_KEYS (which
# stops recursion entirely because the whole subtree is someone else's document): here the keys
# are opaque but each value (a ToolConfig: `pin`/`additionalSearchText`) is not. Without this, a
# tool literally named e.g. `topK` -- which collides with an unrelated, real _CAMEL_TO_SNAKE entry
# used for Azure AI Search's `indexes[].topK` -- would be silently renamed to `top_k`, making the
# config apply to a nonexistent tool instead of the one actually named `topK`
# (azure-ai-projects 2.4.0's ToolboxTool.tool_configs: "keys are tool names or `*`").
_ARBITRARY_KEYED_MAP_FIELDS = {"toolConfigs"}


# --------------------------------------------------------------------------------------
# Pure helpers (no Azure SDK import; unit-tested offline)
# --------------------------------------------------------------------------------------
def load_manifest(path: Path) -> dict[str, Any]:
    """Load + parse the toolbox manifest JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


class SkillSource(NamedTuple):
    """Canonical local source for one Foundry skill version."""

    name: str
    description: str
    content: str
    path: Path


def _normalize_skill_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def parse_skill_source(path: Path) -> SkillSource:
    """Parse the Agent Skills front matter without adding a YAML dependency."""
    content = _normalize_skill_text(path.read_text(encoding="utf-8"))
    match = _SKILL_FRONT_MATTER_RE.fullmatch(content)
    if match is None:
        raise ValueError("must start with YAML front matter delimited by `---`")

    fields: dict[str, str] = {}
    for line in match.group("header").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"front matter line is not `key: value`: {line!r}")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()

    name = fields.get("name", "")
    description = fields.get("description", "")
    if not _SKILL_SLUG_RE.fullmatch(name) or "--" in name:
        raise ValueError(
            "name must match ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$, contain no "
            "consecutive hyphens, and be at most 64 characters"
        )
    if name[:1] in {'"', "'"} or name[-1:] in {'"', "'"}:
        raise ValueError("name must be unquoted")
    if not description or len(description) > 1024:
        raise ValueError("description must contain 1-1024 characters")
    if description[:1] in {'"', "'"} or description[-1:] in {'"', "'"}:
        raise ValueError("description must be unquoted")
    if not match.group("body").strip():
        raise ValueError("instruction body must not be empty")
    return SkillSource(
        name=name,
        description=description,
        content=content,
        path=path,
    )


def manifest_skill_sources(
    manifest: dict[str, Any], *, skills_root: Path = DEFAULT_SKILLS_ROOT
) -> tuple[list[SkillSource], list[str]]:
    """Load local sources for every unpinned skill in an active manifest."""
    sources: list[SkillSource] = []
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return sources, errors
    if manifest.get("lifecycle") != "active":
        return sources, errors
    for entry in manifest.get("skills") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        if entry.get("version"):
            continue
        expected_name = entry["name"]
        path = skills_root / expected_name / "SKILL.md"
        if not path.is_file():
            errors.append(f"skill {expected_name!r}: source not found at {path}")
            continue
        try:
            source = parse_skill_source(path)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"skill {expected_name!r}: {exc}")
            continue
        if source.name != expected_name:
            errors.append(
                f"skill {expected_name!r}: SKILL.md name is {source.name!r}"
            )
            continue
        sources.append(source)
    return sources, errors


def _require_array_or_absent(manifest: dict[str, Any], key: str, errors: list[str]) -> list[Any]:
    """Return ``manifest[key]``, defaulting to ``[]`` only when the key is genuinely absent.

    A bare ``manifest.get(key) or []`` looks equivalent but is not: it coerces ANY *falsy* value
    actually present in the manifest (JSON ``null``, ``{}``, ``""``, ``false``, ``0``) into an
    empty list *before* an ``isinstance`` check ever runs -- so ``{"tools": null}`` or
    ``{"skills": {}}`` silently validated as "no tools/skills" instead of being reported as the
    malformed manifest it is (round-12 finding: this let a schema-rejectable manifest pass the
    no-``jsonschema`` fallback path and exit 0). Checking key presence (``key not in manifest``)
    rather than the retrieved value's truthiness keeps "absent" (default to ``[]``, no error) and
    "present but the wrong type -- including a present-but-falsy wrong type" (report an error)
    distinguishable, which a truthiness check can never do since ``None``/absent are otherwise
    indistinguishable via ``.get()``.
    """
    if key not in manifest:
        return []
    value = manifest[key]
    if not isinstance(value, list):
        errors.append(f"`{key}` must be a JSON array, got {type(value).__name__}.")
        return []
    return value


def _public_https_url_error(value: str) -> str | None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return "must be a public HTTPS base URL without credentials, query, or fragment"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "." not in host or host == "localhost" or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        return "must use a publicly reachable DNS host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_global:
        return "must not target loopback, private, link-local, or reserved IP space"
    return None


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty => valid to provision).

    Type-safe by construction: a malformed root shape (not a JSON object, or `tools`/`skills`/
    `connections` not arrays, or a non-object tool entry) produces a clean error here instead of
    an ``AttributeError``/``TypeError``. This matters because ``main()`` runs the strict,
    inherently type-safe ``validate_manifest_schema()`` (below) *first* and short-circuits on any
    schema error -- but this function must still be safe to call on its own (e.g. when the
    optional ``jsonschema`` dependency is not installed and it is the only check that runs).
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        errors.append(f"manifest must be a JSON object, got {type(manifest).__name__}.")
        return errors

    name = manifest.get("name")
    if not isinstance(name, str) or not _SLUG_RE.match(name):
        errors.append("`name` must be a lowercase slug matching ^[a-z][a-z0-9-]{0,38}[a-z0-9]$ (2-40 chars, starts with a letter).")
    if not manifest.get("description"):
        errors.append("`description` is required and must be non-empty.")

    tools = _require_array_or_absent(manifest, "tools", errors)
    skills = _require_array_or_absent(manifest, "skills", errors)
    connections = _require_array_or_absent(manifest, "connections", errors)
    if not (tools or skills or connections):
        errors.append(
            "manifest is inert: add at least one of `tools`, `skills`, or `connections` "
            "before provisioning (a toolbox must contain at least one item)."
        )

    unnamed = 0
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            errors.append(f"tools[{i}] must be a JSON object, got {type(tool).__name__}.")
            continue
        ttype = tool.get("type")
        if ttype not in _ALLOWED_TOOL_TYPES:
            errors.append(f"tools[{i}].type '{ttype}' is not one of {sorted(_ALLOWED_TOOL_TYPES)}.")
            continue
        if ttype == "a2a_preview" and "baseUrl" in tool:
            base_url = tool["baseUrl"]
            if not isinstance(base_url, str):
                errors.append(f"tools[{i}].baseUrl must be a string.")
            else:
                url_error = _public_https_url_error(base_url)
                if url_error:
                    errors.append(f"tools[{i}].baseUrl {url_error}.")
        # The service identifies a tool by `name` OR `serverLabel` (mcp tools use the latter).
        if not (tool.get("name") or tool.get("serverLabel")):
            unnamed += 1
    if unnamed > 1:
        errors.append(
            f"{unnamed} tools have no identifier; the service allows at most ONE tool total without "
            "a `name` (or `serverLabel` for mcp) -- give every other tool a unique `name`."
        )

    # Type-safe by construction, same as the `tools[]` loop above: without this, a malformed
    # entry (`skills: [null]`, a non-object connection, a missing/blank/non-string `name`) sails
    # through here with zero errors -- `skills`/`connections` were never per-entry validated -- and
    # then crashes downstream the first time something actually subscripts the entry: `main()`'s
    # dry-run summary print (`s["name"] for s in manifest.get("skills") or []`) and
    # `to_azd_yaml()`'s `s["name"]`/`c["name"]` renders both assume every entry is a dict with a
    # usable `name`. This is the ONLY check that runs when the optional `jsonschema` dependency is
    # not installed (see this function's docstring), so it must catch these shapes itself rather
    # than relying on `foundry/toolbox.manifest.schema.json`.
    for i, skill in enumerate(skills):
        if not isinstance(skill, dict):
            errors.append(f"skills[{i}] must be a JSON object, got {type(skill).__name__}.")
            continue
        skill_name = skill.get("name")
        if not isinstance(skill_name, str) or not skill_name:
            errors.append(f"skills[{i}].name is required and must be a non-empty string.")
        version = skill.get("version")
        if version is not None and not isinstance(version, str):
            errors.append(f"skills[{i}].version must be a string if present, got {type(version).__name__}.")

    for i, conn in enumerate(connections):
        if not isinstance(conn, dict):
            errors.append(f"connections[{i}] must be a JSON object, got {type(conn).__name__}.")
            continue
        conn_name = conn.get("name")
        if not isinstance(conn_name, str) or not conn_name:
            errors.append(f"connections[{i}].name is required and must be a non-empty string.")

    return errors


_MANIFEST_SCHEMA_PATH = REPO_ROOT / "foundry" / "toolbox.manifest.schema.json"


def validate_manifest_schema(manifest: dict[str, Any]) -> list[str] | None:
    """Validate ``manifest`` against foundry/toolbox.manifest.schema.json.

    Returns ``None`` if the optional ``jsonschema`` dependency is not installed (callers decide
    how strict to be about that -- see ``main()``: ``--create`` requires it, the dependency-free
    dry run only best-effort validates). Otherwise returns a list of human-readable error strings
    (empty => schema-valid). This is the strict, per-tool-type structural check (required nested
    fields, cardinality, no unknown properties) that ``validate_manifest`` above does not cover;
    CI's separate `check-jsonschema` step lints the same schema but the provisioner did not use
    to apply it itself before constructing SDK models.
    """
    try:
        import jsonschema
    except ImportError:
        return None
    schema = json.loads(_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]


def _to_snake(key: str) -> str:
    return _CAMEL_TO_SNAKE.get(key, key)


def _convert_keys(value: Any, *, parent_key: str | None = None) -> Any:
    """Recursively rewrite camelCase manifest keys to the API's snake_case.

    ``(parent_key, key)`` pairs in ``_OPAQUE_NESTED_KEYS`` (currently just ``openapi.spec``) are
    copied verbatim -- not recursed into -- so an externally-authored payload's own property
    names are never mistaken for AI4IA manifest keys and rewritten (see ``_OPAQUE_NESTED_KEYS``).

    ``parent_key`` values in ``_ARBITRARY_KEYED_MAP_FIELDS`` (currently just ``toolConfigs``) get
    a different treatment: the map's own keys are copied verbatim (they are caller-defined tool
    names, not manifest schema keys), but -- unlike ``_OPAQUE_NESTED_KEYS`` -- each value is still
    recursed into and snake_cased, since ``ToolConfig`` fields like ``additionalSearchText`` are
    real AI4IA manifest keys (see ``_ARBITRARY_KEYED_MAP_FIELDS``).
    """
    if isinstance(value, dict):
        if parent_key in _ARBITRARY_KEYED_MAP_FIELDS:
            return {k: _convert_keys(v, parent_key=None) for k, v in value.items()}
        return {
            _to_snake(k): (v if (parent_key, k) in _OPAQUE_NESTED_KEYS else _convert_keys(v, parent_key=k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_convert_keys(v, parent_key=parent_key) for v in value]
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
def _project_client(project_endpoint: str) -> Any:
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - exercised only on live provisioning
        raise SystemExit(
            "azure-ai-projects is not installed. Install the optional provisioning group:\n"
            '  uv pip install -e "app/api[foundry]"   # or: pip install azure-ai-projects==2.4.0 azure-identity'
        ) from exc
    return AIProjectClient(endpoint=project_endpoint, credential=DefaultAzureCredential())


def _sdk_models() -> Any:
    try:
        from azure.ai.projects import models
    except ImportError as exc:  # pragma: no cover - exercised only without provisioning extra
        raise SystemExit(
            "azure-ai-projects is not installed. Install the optional provisioning group:\n"
            '  uv pip install -e "app/api[foundry]"   # or: pip install azure-ai-projects==2.4.0 azure-identity'
        ) from exc
    return models


def _skill_content_from_download(chunks: Any) -> str:
    """Extract the single SKILL.md from the Skills API's ZIP response."""
    payload = b"".join(chunks)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.rstrip("/").split("/")[-1].casefold() == "skill.md"
            ]
            if len(candidates) != 1:
                raise ValueError(
                    f"expected exactly one SKILL.md, found {len(candidates)}"
                )
            return _normalize_skill_text(
                archive.read(candidates[0]).decode("utf-8")
            )
    except (OSError, UnicodeError, zipfile.BadZipFile, ValueError) as exc:
        raise SystemExit(
            "Foundry returned an invalid skill archive; refusing to compare or "
            f"create another immutable version: {exc}"
        ) from exc


def _download_skill_version(
    project: Any, name: str, version: str
) -> str:
    return _skill_content_from_download(
        project.beta.skills.download_version(
            name,
            version,
            headers=SKILLS_FEATURES_HEADER,
        )
    )


def create_skill_version(
    source: SkillSource, project_endpoint: str, *, project: Any | None = None
) -> Any:
    """Create and activate one immutable Foundry skill version."""
    project = project or _project_client(project_endpoint)
    models = _sdk_models()
    result = project.beta.skills.create_from_files(
        source.name,
        content=models.CreateSkillVersionFromFilesBody(
            files=[("SKILL.md", source.content.encode("utf-8"))],
            default=True,
        ),
        headers=SKILLS_FEATURES_HEADER,
    )
    version = getattr(result, "version", None)
    if not version:
        raise SystemExit(
            f"skills.create_from_files('{source.name}') returned no usable "
            "`version`; refusing to skip activation silently."
        )
    # Keep activation explicit even though the upload asks for default=True. If
    # this second call fails, ensure_skill() will find and reuse the immutable
    # matching version on retry rather than append a duplicate.
    project.beta.skills.update(
        source.name,
        default_version=version,
        headers=SKILLS_FEATURES_HEADER,
    )
    return result


def ensure_skill(
    source: SkillSource, project_endpoint: str, *, project: Any | None = None
) -> tuple[Any, bool]:
    """Activate matching skill content, creating one version only if needed."""
    from azure.core.exceptions import ResourceNotFoundError

    project = project or _project_client(project_endpoint)
    try:
        skill = project.beta.skills.get(
            source.name,
            headers=SKILLS_FEATURES_HEADER,
        )
    except ResourceNotFoundError:
        return create_skill_version(source, project_endpoint, project=project), True

    default_version = getattr(skill, "default_version", None)
    if not default_version:
        raise SystemExit(
            f"skills.get('{source.name}') returned no default_version; refusing "
            "to guess which immutable version is active."
        )
    current = _download_skill_version(project, source.name, default_version)
    if current == source.content:
        return project.beta.skills.get_version(
            source.name,
            default_version,
            headers=SKILLS_FEATURES_HEADER,
        ), False

    # Reuse a matching version left behind by an interrupted activation.
    for candidate in project.beta.skills.list_versions(
        source.name,
        headers=SKILLS_FEATURES_HEADER,
    ):
        version = getattr(candidate, "version", None)
        if not version:
            continue
        if _download_skill_version(project, source.name, version) != source.content:
            continue
        project.beta.skills.update(
            source.name,
            default_version=version,
            headers=SKILLS_FEATURES_HEADER,
        )
        return candidate, False
    return create_skill_version(source, project_endpoint, project=project), True


def ensure_manifest_skills(
    sources: list[SkillSource],
    project_endpoint: str,
    *,
    project: Any | None = None,
) -> list[tuple[Any, bool]]:
    """Reconcile every locally owned skill before creating the toolbox version."""
    project = project or _project_client(project_endpoint)
    return [
        ensure_skill(source, project_endpoint, project=project)
        for source in sources
    ]


def _build_toolbox_kwargs(manifest: dict[str, Any], models: Any) -> dict[str, Any]:
    """Build the exact SDK request fields used for creation and live-state comparison."""
    tools: list[Any] = []
    for tool in manifest.get("tools") or []:
        cls_name = _TYPE_TO_MODEL.get(tool["type"])
        if cls_name is None:  # pragma: no cover - guarded earlier by validate_manifest
            raise SystemExit(
                f"tool type '{tool['type']}' cannot be placed in a toolbox via azure-ai-projects "
                f"(creatable types: {sorted(_ALLOWED_TOOL_TYPES)})."
            )
        model_cls = getattr(models, cls_name)
        fields = {k: v for k, v in _convert_keys(tool).items() if k != "type"}
        tools.append(model_cls(**fields))

    kwargs: dict[str, Any] = {"tools": tools, "description": manifest.get("description", "")}
    if manifest.get("skills"):
        kwargs["skills"] = [
            models.ToolboxSkillReference(name=s["name"], version=s["version"])
            if s.get("version")
            else models.ToolboxSkillReference(name=s["name"])
            for s in manifest["skills"]
        ]
    if manifest.get("raiPolicyName"):
        kwargs["policies"] = models.ToolboxPolicies(
            rai_config=models.RaiConfig(rai_policy_name=manifest["raiPolicyName"])
        )
    return kwargs


def _canonical_state(value: Any) -> Any:
    """Reduce SDK models to stable request-visible content, omitting null/default gaps."""
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    if isinstance(value, dict):
        return {
            key: _canonical_state(item)
            for key, item in sorted(value.items())
            if item is not None
        }
    if isinstance(value, list):
        return [_canonical_state(item) for item in value]
    return value


def _toolbox_state(value: Any) -> dict[str, Any]:
    fields = {
        "description": getattr(value, "description", None),
        "tools": getattr(value, "tools", None),
        # Foundry materializes omitted top-level optionals as empty service
        # defaults. Normalize only these known fields; nested tool payloads
        # (especially opaque OpenAPI specs) must preserve meaningful empty
        # arrays/objects such as operation-level `security: []`.
        "skills": getattr(value, "skills", None) or None,
        "policies": getattr(value, "policies", None) or None,
    }
    return _canonical_state(fields)


def _desired_toolbox_state(kwargs: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "description": kwargs.get("description"),
        "tools": kwargs.get("tools"),
        "skills": kwargs.get("skills") or None,
        "policies": kwargs.get("policies") or None,
    }
    return _canonical_state(fields)


def create_toolbox(manifest: dict[str, Any], project_endpoint: str, *, project: Any | None = None) -> Any:
    """Create a toolbox version via azure-ai-projects and activate it as the default version.

    Imported lazily so dry runs need no SDK. Uses the real SDK surface: ``client.toolboxes.
    create_version(name, *, tools=[<typed ToolboxTool subclasses>], description,
    skills=[ToolboxSkillReference], policies=ToolboxPolicies)``. Each manifest tool maps to its
    discriminated model class (``_TYPE_TO_MODEL``); type-specific fields (including nested
    objects such as ``azure_ai_search.indexes[]`` or ``browser_automation_preview.connection``)
    are recursively snake_cased and passed through as constructor kwargs.

    ``create_version`` only adds an immutable version -- it does NOT change which version the
    toolbox's MCP endpoint actually serves (``ToolboxObject.default_version`` is a separate
    pointer that is fixed at creation and does not auto-advance). So every successful call here
    also explicitly activates the new version via ``toolboxes.update(name,
    default_version=<new version>)``, otherwise consumers would keep being served the old
    ``default_version`` forever after the first `--create`.
    """
    project = project or _project_client(project_endpoint)
    m = _sdk_models()
    kwargs = _build_toolbox_kwargs(manifest, m)
    result = project.toolboxes.create_version(
        manifest["name"], headers=TOOLBOX_FEATURES_HEADER, **kwargs
    )

    version = getattr(result, "version", None)
    if not version:
        raise SystemExit(
            f"toolboxes.create_version('{manifest['name']}') returned no usable `version` "
            f"(got: {version!r} on {result!r}); refusing to skip activation silently. Inspect the "
            "SDK response and activate the correct version manually via "
            "`project.toolboxes.update(name, default_version=...)`."
        )
    project.toolboxes.update(
        manifest["name"],
        default_version=version,
        headers=TOOLBOX_FEATURES_HEADER,
    )
    return result


def check_toolbox_access(project_endpoint: str, *, project: Any | None = None) -> None:
    """Fail closed unless the caller can read the Foundry toolbox data plane."""
    from azure.core.exceptions import HttpResponseError

    project = project or _project_client(project_endpoint)
    try:
        next(
            iter(
                project.toolboxes.list(
                    limit=1,
                    headers=TOOLBOX_FEATURES_HEADER,
                )
            ),
            None,
        )
    except HttpResponseError as exc:
        status = getattr(exc, "status_code", None)
        if status in {401, 403}:
            raise SystemExit(
                "Foundry toolbox data-plane access denied. Grant the workflow OIDC "
                "identity the project-scoped 'Foundry User' role "
                "(53ca6127-db72-4b80-b1b0-d745d6d5456d) on the primary Foundry "
                "project; the Azure login alone grants no toolbox access."
            ) from exc
        raise


def ensure_toolbox(
    manifest: dict[str, Any], project_endpoint: str, *, project: Any | None = None
) -> tuple[Any, bool]:
    """Create and activate exactly one version when the served default differs."""
    from azure.core.exceptions import ResourceNotFoundError

    project = project or _project_client(project_endpoint)
    m = _sdk_models()
    kwargs = _build_toolbox_kwargs(manifest, m)
    try:
        toolbox = project.toolboxes.get(
            manifest["name"], headers=TOOLBOX_FEATURES_HEADER
        )
    except ResourceNotFoundError:
        return create_toolbox(manifest, project_endpoint, project=project), True

    current = project.toolboxes.get_version(
        manifest["name"],
        toolbox.default_version,
        headers=TOOLBOX_FEATURES_HEADER,
    )
    desired_state = _desired_toolbox_state(kwargs)
    if _toolbox_state(current) == desired_state:
        return current, False

    # create_version() and update(default_version=...) are separate service calls. If
    # activation failed after a successful create, retrying must reuse that immutable
    # version rather than append an identical one on every run.
    for candidate in project.toolboxes.list_versions(
        manifest["name"],
        headers=TOOLBOX_FEATURES_HEADER,
    ):
        if _toolbox_state(candidate) != desired_state:
            continue
        version = getattr(candidate, "version", None)
        if not version:
            raise SystemExit(
                f"toolboxes.list_versions('{manifest['name']}') returned matching content "
                f"without a usable `version` (got: {candidate!r}); refusing to create a "
                "duplicate immutable version."
            )
        project.toolboxes.update(
            manifest["name"],
            default_version=version,
            headers=TOOLBOX_FEATURES_HEADER,
        )
        return candidate, False
    return create_toolbox(manifest, project_endpoint, project=project), True


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Path to toolbox.manifest.json.")
    parser.add_argument("--project-endpoint", default=None, help="Foundry project endpoint (else AZURE_FOUNDRY_PROJECT_ENDPOINT).")
    parser.add_argument("--create", action="store_true", help="Actually create the toolbox version (needs azure-ai-projects). Default is a dry run.")
    parser.add_argument(
        "--check-access",
        action="store_true",
        help="Verify project-scoped toolbox data-plane access and exit.",
    )
    parser.add_argument("--emit-yaml", type=Path, default=None, metavar="PATH", help="Write the `azd ai toolbox create --from-file` YAML and exit.")
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        raise SystemExit(f"Manifest not found: {args.manifest}")
    manifest = load_manifest(args.manifest)

    # Schema validation runs FIRST: a JSON Schema validator never raises on a malformed shape (a
    # root array, `tools: [null]`, a non-object tool entry, etc. all produce clean validation
    # errors, never a crash), so running it before the handwritten `validate_manifest()` below
    # guarantees a confirmed-malformed manifest is reported cleanly and stops here -- we never
    # reach `validate_manifest()`'s `.get()` calls on a shape it cannot safely process. (Round-10
    # finding: the old order let `validate_manifest()` run first and crash with an unhandled
    # AttributeError/TypeError on exactly these shapes.) `validate_manifest()` is *also* hardened
    # with isinstance() guards (see its docstring) as a fallback for when the optional
    # `jsonschema` dependency below is not installed and it is the only check that runs.
    schema_errors = validate_manifest_schema(manifest)
    errors: list[str] = []
    schema_unavailable_msg: str | None = None
    if schema_errors is None:
        if args.create:
            errors.append(
                "jsonschema is not installed; --create requires strict schema validation before "
                "SDK construction. Install the optional provisioning group: "
                'uv pip install -e "app/api[foundry]"   # or: pip install jsonschema'
            )
        else:
            schema_unavailable_msg = (
                "(jsonschema not installed -- skipping strict schema validation; only the "
                "hand-written checks below ran. Install it, or use --create's `foundry` extra, "
                "for full validation.)"
            )
    else:
        errors.extend(f"schema: {e}" for e in schema_errors)

    if errors:
        print("Manifest is not ready to provision:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    if schema_unavailable_msg:
        print(schema_unavailable_msg, file=sys.stderr)

    # Strict, per-tool-type schema validation already ran above; this hand-written pass adds the
    # checks the schema does not express (at-most-one-unnamed-tool cardinality across the whole
    # manifest, etc.) -- applied here, before any SDK construction, not just linted separately in
    # CI.
    errors = validate_manifest(manifest)
    skill_sources, skill_errors = manifest_skill_sources(manifest)
    errors.extend(skill_errors)
    if errors:
        print("Manifest is not ready to provision:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if (args.create or args.emit_yaml) and manifest.get("lifecycle") != "active":
        print("Manifest is not ready to provision:", file=sys.stderr)
        print(
            "  - live creation/YAML emission requires lifecycle='active'; "
            "reference manifests are validation examples only.",
            file=sys.stderr,
        )
        return 1

    if args.emit_yaml:
        args.emit_yaml.write_text(to_azd_yaml(manifest), encoding="utf-8")
        print(f"Wrote azd toolbox YAML -> {args.emit_yaml}")
        return 0

    endpoint = resolve_project_endpoint(args.project_endpoint)
    if args.check_access:
        check_toolbox_access(endpoint)
        print(
            "Foundry toolbox data-plane access is ready "
            "(project-scoped Foundry User role confirmed)."
        )
        return 0
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
        print("\n(dry run) Re-run with --create to ensure the toolbox in Foundry.")
        return 0

    project = _project_client(endpoint)
    skill_results = ensure_manifest_skills(
        skill_sources,
        endpoint,
        project=project,
    )
    for source, (skill, created) in zip(
        skill_sources, skill_results, strict=True
    ):
        action = "Created" if created else "Reconciled"
        print(
            f"\n{action} skill '{source.name}' version "
            f"{getattr(skill, 'version', '?')} as the default."
        )

    result, changed = ensure_toolbox(
        manifest,
        endpoint,
        project=project,
    )
    if changed:
        version = getattr(result, "version", "?")
        print(f"\nCreated toolbox '{manifest['name']}' version {version} and activated it as the default version.")
    else:
        print(
            f"\nToolbox '{manifest['name']}' reconciled to existing version "
            f"{getattr(result, 'version', '?')}; no version created."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
