"""Guard the Foundry toolbox + skills provisioning seam (docs/foundry-toolbox.md).

These pin the *pure* projection/validation logic of the provisioning scripts without any
Azure SDK, network, or new runtime dependency. The load-bearing guarantee: the toolbox
script projects the toolbox to a catalog entry that is a VALID infra/mcp-servers.json entry
carrying exactly the managed-identity bearer + Foundry-Features header + api-version query the
bridge needs -- so the app consumes it with zero new runtime code.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLBOX_SCRIPT = _REPO_ROOT / "scripts" / "provision-foundry-toolbox.py"
_SKILLS_SCRIPT = _REPO_ROOT / "scripts" / "provision-foundry-skills.py"
_MANIFEST = _REPO_ROOT / "foundry" / "toolbox.manifest.json"
_MANIFEST_SCHEMA = _REPO_ROOT / "foundry" / "toolbox.manifest.schema.json"
_EXAMPLE_MANIFEST = _REPO_ROOT / "foundry" / "toolbox.manifest.example.json"
_MCP_SCHEMA = _REPO_ROOT / "infra" / "mcp-servers.schema.json"
_EXAMPLE_SKILL = _REPO_ROOT / "foundry" / "skills" / "citation-discipline" / "SKILL.md"

_ENDPOINT = "https://acct.services.ai.azure.com/api/projects/proj"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_tb = _load("provision_foundry_toolbox", _TOOLBOX_SCRIPT)
_sk = _load("provision_foundry_skills", _SKILLS_SCRIPT)


def _valid_manifest() -> dict:
    return {
        "name": "ai4ia-toolbox",
        "description": "Web search + code interpreter + tool search",
        "raiPolicyName": "ai4ia-annotate-only",
        "connections": [],
        "tools": [
            {"type": "web_search", "name": "web"},
            {"type": "code_interpreter", "name": "code", "container": {"type": "auto"}},
            {"type": "toolbox_search_preview"},
            {
                "type": "mcp",
                "serverLabel": "learn",
                "serverUrl": "https://learn.microsoft.com/api/mcp",
                "requireApproval": "never",
                "projectConnectionId": "learn-conn",
            },
        ],
        "skills": [{"name": "citation-discipline"}],
    }


# ----------------------------- manifest validation ------------------------------------
def test_checked_in_manifest_matches_the_live_toolbox():
    manifest = _tb.load_manifest(_MANIFEST)
    # The checked-in manifest is now the CANONICAL definition a new tenant reproduces 1:1
    # (it captures the deployed ai4ia-toolbox composition), so it must be provisionable.
    assert _tb.validate_manifest(manifest) == []
    assert manifest["name"] == "ai4ia-toolbox"
    tool_types = [t["type"] for t in manifest["tools"]]
    assert tool_types == ["web_search", "code_interpreter", "toolbox_search_preview"]
    # Every tool is named (the service allows at most one unnamed tool total).
    assert all(t.get("name") for t in manifest["tools"])


def test_valid_manifest_has_no_errors():
    assert _tb.validate_manifest(_valid_manifest()) == []


def test_manifest_rejects_bad_name_and_too_many_unnamed_tools():
    bad_name = {**_valid_manifest(), "name": "Toolbox_NOPE"}
    assert any("name" in e for e in _tb.validate_manifest(bad_name))

    # The service allows at most ONE unnamed tool total (across all types).
    dup = _valid_manifest()
    dup["tools"] = [{"type": "web_search"}, {"type": "code_interpreter"}]  # two unnamed, any type
    errs = _tb.validate_manifest(dup)
    assert any("at most ONE tool total" in e for e in errs)


def test_manifest_rejects_non_toolbox_tool_types():
    # computer_use / bing_custom_search are agent-level tools with no toolbox equivalent.
    for bad_type in ("computer_use", "bing_custom_search"):
        m = _valid_manifest()
        m["tools"] = [{"type": bad_type, "name": "x"}]
        assert any(bad_type in e for e in _tb.validate_manifest(m))


def test_every_allowed_type_maps_to_a_model_class():
    # The live create path resolves each allowed type to a discriminated SDK model class.
    assert _tb._ALLOWED_TOOL_TYPES == set(_tb._TYPE_TO_MODEL)
    assert all(name.endswith("ToolboxTool") for name in _tb._TYPE_TO_MODEL.values())
    assert "computer_use" not in _tb._ALLOWED_TOOL_TYPES
    assert "browser_automation_preview" in _tb._ALLOWED_TOOL_TYPES


# ----------------------------- tool projection ----------------------------------------
def test_plan_tools_camel_to_snake():
    planned = _tb.plan_tools(_valid_manifest())
    mcp = next(t for t in planned if t["type"] == "mcp")
    assert mcp["server_label"] == "learn"
    assert mcp["server_url"] == "https://learn.microsoft.com/api/mcp"
    assert mcp["require_approval"] == "never"
    assert mcp["project_connection_id"] == "learn-conn"
    # camelCase keys must not survive.
    assert not any(k in mcp for k in ("serverLabel", "serverUrl", "requireApproval", "projectConnectionId"))


def test_consumer_url_and_portable_entry_shape():
    assert _tb.consumer_mcp_url(_ENDPOINT, "ai4ia-toolbox") == (
        "https://acct.services.ai.azure.com/api/projects/proj/toolboxes/ai4ia-toolbox/mcp?api-version=v1"
    )
    entry = _tb.build_mcp_server_entry(_valid_manifest(), _ENDPOINT)
    assert entry["name"] == "ai4ia-toolbox"
    # PORTABLE: the entry carries no hardcoded upstreamUrl; main.bicep computes it per
    # environment from the deployed project endpoint. This is what keeps the catalog 1:1.
    assert "upstreamUrl" not in entry
    assert entry["foundryToolbox"] is True
    assert entry["upstreamAuthMode"] == "managed_identity"
    assert entry["upstreamMiResource"] == "https://ai.azure.com"
    assert entry["upstreamHeaders"] == {"Foundry-Features": "Toolboxes=V1Preview"}
    assert entry["upstreamQueryParams"] == {"api-version": "v1"}


def test_azd_yaml_translates_and_includes_rai_policy():
    yaml = _tb.to_azd_yaml(_valid_manifest())
    assert "server_label:" in yaml and "serverLabel" not in yaml
    assert "rai_policy_name:" in yaml
    assert "- type: toolbox_search_preview" in yaml


# ----------------- cross-seam guard: entry is a valid official-MCP entry --------------
def test_projected_entry_validates_against_official_mcp_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MCP_SCHEMA.read_text(encoding="utf-8"))
    entry = _tb.build_mcp_server_entry(_valid_manifest(), _ENDPOINT)
    # The toolbox's projected entry must be a valid member of infra/mcp-servers.json.
    jsonschema.validate({"servers": [entry]}, schema)


def test_manifest_matches_its_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(_tb.load_manifest(_MANIFEST), schema)
    jsonschema.validate(_valid_manifest(), schema)


def test_example_manifest_is_populated_valid_and_schema_valid():
    # The reference manifest shows one of *every* supported tool type (docs/foundry-toolbox.md
    # and foundry/README.md both claim this); unlike the shipped inert one it must be populated,
    # provisionable, and schema-valid so operators can copy it verbatim.
    manifest = _tb.load_manifest(_EXAMPLE_MANIFEST)
    assert _tb.validate_manifest(manifest) == []
    tool_types = {t["type"] for t in manifest["tools"]}
    # Pin the "one of each type" doc claim as an executable invariant so the two never drift
    # apart again (this is exactly what regressed before: the example was missing file_search,
    # openapi, and mcp while the docs still claimed full coverage).
    assert tool_types == _tb._ALLOWED_TOOL_TYPES
    # non-toolbox tool types must not appear
    assert "computer_use" not in tool_types and "bing_custom_search" not in tool_types
    # both a default and a custom code_interpreter are present (the custom one is named)
    ci = [t for t in manifest["tools"] if t["type"] == "code_interpreter"]
    assert any("name" in t for t in ci) and len(ci) >= 2
    # mcp tools are identified by serverLabel, not name (per the "one unnamed tool" rule).
    mcp = next(t for t in manifest["tools"] if t["type"] == "mcp")
    assert mcp.get("serverLabel") and not mcp.get("name")
    # Every projectConnectionId reference must resolve to a declared connection so the example
    # is actually self-consistent (not just individually schema-valid fields).
    conn_names = {c["name"] for c in manifest["connections"]}
    for t in manifest["tools"]:
        if t.get("projectConnectionId"):
            assert t["projectConnectionId"] in conn_names
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)


def test_plan_tools_maps_file_search_vector_store_ids():
    # file_search's vectorStoreIds -> vector_store_ids mapping was missing from
    # _CAMEL_TO_SNAKE (found while adding the file_search example above); a real
    # provisioning run would have sent the untranslated camelCase key to the SDK.
    manifest = {**_valid_manifest(), "tools": [{"type": "file_search", "name": "fs", "vectorStoreIds": ["vs-1", "vs-2"]}]}
    planned = _tb.plan_tools(manifest)
    assert planned[0]["vector_store_ids"] == ["vs-1", "vs-2"]
    assert "vectorStoreIds" not in planned[0]


# ----------------------------------- skills -------------------------------------------
def test_parse_skill_md_splits_frontmatter_and_body():
    parsed = _sk.parse_skill_md(
        '---\nname: greeting\ndescription: Say hi.\n---\n\nBe warm and brief.\n'
    )
    assert parsed["name"] == "greeting"
    assert parsed["description"] == "Say hi."
    assert parsed["instructions"] == "Be warm and brief."
    assert _sk.validate_skill(parsed) == []


def test_validate_skill_flags_bad_name_and_missing_fields():
    assert any("name" in e for e in _sk.validate_skill({"name": "Bad_Name", "description": "d", "instructions": "i"}))
    assert any("description" in e for e in _sk.validate_skill({"name": "ok", "description": "", "instructions": "i"}))
    assert any("instruction" in e for e in _sk.validate_skill({"name": "ok", "description": "d", "instructions": ""}))


def test_checked_in_example_skill_is_valid():
    parsed = _sk.parse_skill_md(_EXAMPLE_SKILL.read_text(encoding="utf-8"))
    assert parsed["name"] == "citation-discipline"
    assert _sk.validate_skill(parsed) == []


# ----------------------------- config fail-closed -------------------------------------
@pytest.mark.parametrize("module", [_tb, _sk], ids=["toolbox", "skills"])
def test_resolve_project_endpoint_fails_closed(module, monkeypatch):
    # No --project-endpoint arg and no env var => hard stop (never a silent default).
    monkeypatch.delenv("AZURE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    with pytest.raises(SystemExit):
        module.resolve_project_endpoint(None)
    # Explicit arg wins; env var is the fallback.
    assert module.resolve_project_endpoint("https://x/api/projects/p") == "https://x/api/projects/p"
    monkeypatch.setenv("AZURE_FOUNDRY_PROJECT_ENDPOINT", "https://env/api/projects/p")
    assert module.resolve_project_endpoint(None) == "https://env/api/projects/p"
