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
import sys
from pathlib import Path
from types import SimpleNamespace

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
    # both a default (connectionless) and a custom-search-scoped web_search are present.
    ws = [t for t in manifest["tools"] if t["type"] == "web_search"]
    assert len(ws) >= 2
    assert any("customSearchConfiguration" not in t for t in ws)
    assert any("customSearchConfiguration" in t for t in ws)
    # mcp tools are identified by serverLabel, not name (per the "one unnamed tool" rule).
    mcp = next(t for t in manifest["tools"] if t["type"] == "mcp")
    assert mcp.get("serverLabel") and not mcp.get("name")
    # Every projectConnectionId reference must resolve to a declared connection so the example
    # is actually self-consistent (not just individually schema-valid fields). azure_ai_search,
    # browser_automation_preview, and web_search's customSearchConfiguration each nest their
    # connection reference (see SDK-shape note above); checking only the tool root here would
    # silently stop covering them.
    conn_names = {c["name"] for c in manifest["connections"]}
    for t in manifest["tools"]:
        if t.get("projectConnectionId"):
            assert t["projectConnectionId"] in conn_names
        if t.get("azureAiSearch"):
            for idx in t["azureAiSearch"]["indexes"]:
                if idx.get("projectConnectionId"):
                    assert idx["projectConnectionId"] in conn_names
        if t.get("browserAutomationPreview"):
            assert t["browserAutomationPreview"]["connection"]["projectConnectionId"] in conn_names
        if t.get("customSearchConfiguration"):
            assert t["customSearchConfiguration"]["projectConnectionId"] in conn_names
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)


def test_example_manifest_code_interpreter_customization_is_managed_auto_shape_not_an_image():
    # Regression guard: the second code_interpreter example used to pass an ACR image URI
    # (e.g. "myacr.azurecr.io/ai4ia-code-interpreter:latest") as `container`, implying BYO
    # container image support. `CodeInterpreterToolboxTool.container`'s string form is an
    # EXISTING CONTAINER ID, not an image reference, and there is no toolbox-level BYO-image
    # mechanism in the SDK -- so that example silently modeled an unsupported capability. The
    # customized entry must use the real `AutoCodeInterpreterToolParam` nested-object shape
    # (managed sandbox with custom limits), never a bare string that looks like an image ref.
    manifest = _tb.load_manifest(_EXAMPLE_MANIFEST)
    ci = [t for t in manifest["tools"] if t["type"] == "code_interpreter"]
    assert len(ci) >= 2
    customized = [t for t in ci if "container" in t]
    assert customized, "expected at least one code_interpreter example with a non-default container"
    for t in customized:
        container = t["container"]
        assert isinstance(container, dict), f"container must be the nested auto-object shape, got {container!r}"
        assert container.get("type") == "auto"
        # No tool anywhere in the example may claim BYO-image support via a bare image-like string.
        assert not isinstance(container, str)
    # The false claim must not resurface in the manifest's own descriptions either.
    for t in manifest["tools"]:
        desc = (t.get("description") or "").lower()
        assert "byo" not in desc or "not a custom/byo container image" in desc
        assert ".azurecr.io" not in desc


def test_plan_tools_maps_file_search_vector_store_ids():
    # file_search's vectorStoreIds -> vector_store_ids mapping was missing from
    # _CAMEL_TO_SNAKE (found while adding the file_search example above); a real
    # provisioning run would have sent the untranslated camelCase key to the SDK.
    manifest = {**_valid_manifest(), "tools": [{"type": "file_search", "name": "fs", "vectorStoreIds": ["vs-1", "vs-2"]}]}
    planned = _tb.plan_tools(manifest)
    assert planned[0]["vector_store_ids"] == ["vs-1", "vs-2"]
    assert "vectorStoreIds" not in planned[0]


def test_plan_tools_recursively_converts_nested_azure_ai_search_and_browser_automation_keys():
    # azure_ai_search and browser_automation_preview nest their config one (or two) levels
    # deep. plan_tools/create_toolbox must snake_case those NESTED keys too, not just the
    # tool's top-level keys -- a flat, non-recursive conversion silently produces camelCase
    # keys the SDK's typed nested models do not recognize (they deserialize to None instead
    # of raising; see docs/foundry-toolbox.md's SDK-shape note).
    manifest = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "rag",
                "azureAiSearch": {
                    "indexes": [{"indexName": "docs", "projectConnectionId": "search-conn"}]
                },
            },
            {
                "type": "browser_automation_preview",
                "name": "browser",
                "browserAutomationPreview": {"connection": {"projectConnectionId": "browser-conn"}},
            },
        ],
    }
    planned = _tb.plan_tools(manifest)

    search = next(t for t in planned if t["type"] == "azure_ai_search")
    assert "azureAiSearch" not in search
    assert search["azure_ai_search"]["indexes"][0]["index_name"] == "docs"
    assert search["azure_ai_search"]["indexes"][0]["project_connection_id"] == "search-conn"

    browser = next(t for t in planned if t["type"] == "browser_automation_preview")
    assert "browserAutomationPreview" not in browser
    assert browser["browser_automation_preview"]["connection"]["project_connection_id"] == "browser-conn"


def test_plan_tools_snake_cases_web_search_custom_search_configuration_and_network_policy_domains():
    # Direct regression guard for the exact silent-corruption risk found in round 6: these SDK
    # "Model" classes accept raw dict kwargs for nested fields WITHOUT validating key names at
    # construction time (confirmed empirically), so a camelCase key missing from
    # _CAMEL_TO_SNAKE would not raise -- it would silently persist as the wrong wire key and the
    # service would just never see it. allowedDomains was the gap (added alongside
    # customSearchConfiguration/networkPolicy schema support); pin both conversions here as a
    # pure dict-level check, independent of the optional azure-ai-projects dependency.
    manifest = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "web_search",
                "name": "custom-web-search",
                "customSearchConfiguration": {"projectConnectionId": "bing-conn", "instanceName": "inst"},
            },
            {
                "type": "code_interpreter",
                "name": "restricted-ci",
                "container": {
                    "type": "auto",
                    "networkPolicy": {"type": "allowlist", "allowedDomains": ["pypi.org"]},
                },
            },
        ],
    }
    planned = _tb.plan_tools(manifest)

    web_search = next(t for t in planned if t["type"] == "web_search")
    assert "customSearchConfiguration" not in web_search
    csc = web_search["custom_search_configuration"]
    assert csc == {"project_connection_id": "bing-conn", "instance_name": "inst"}

    ci = next(t for t in planned if t["type"] == "code_interpreter")
    assert "networkPolicy" not in ci["container"]
    policy = ci["container"]["network_policy"]
    assert "allowedDomains" not in policy
    assert policy == {"type": "allowlist", "allowed_domains": ["pypi.org"]}


def test_plan_tools_preserves_opaque_openapi_spec_keys_but_still_converts_auth():
    # Regression guard: _convert_keys() used to recurse into EVERY nested dict, including
    # openapi.spec -- an opaque, externally-authored OpenAPI/JSON-Schema document describing
    # someone else's API. That silently rewrote the API's own property names whenever they
    # happened to collide with an AI4IA manifest key (e.g. a request parameter or response
    # property genuinely named `topK`, `indexName`, `serverUrl`, or `requireApproval` would be
    # mangled to `top_k`/`index_name`/`server_url`/`require_approval`), corrupting the spec the
    # model calls against. `spec` must survive byte-for-byte; sibling `auth` (an AI4IA/SDK-known
    # shape, not opaque) must still be correctly snake_cased.
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/search": {
                "get": {
                    "operationId": "search_docs",
                    "parameters": [
                        {"name": "topK", "in": "query", "schema": {"type": "integer"}},
                        {"name": "serverUrl", "in": "query", "schema": {"type": "string"}},
                    ],
                }
            }
        },
        "components": {
            "schemas": {
                "SearchResult": {
                    "properties": {
                        "topK": {"type": "integer"},
                        "indexName": {"type": "string"},
                        "requireApproval": {"type": "boolean"},
                    }
                }
            }
        },
    }
    manifest = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "openapi",
                "name": "weird-api",
                "openapi": {
                    "name": "search_docs",
                    "auth": {
                        "type": "project_connection",
                        "securityScheme": {"projectConnectionId": "conn-1"},
                    },
                    "spec": spec,
                },
            }
        ],
    }
    planned = _tb.plan_tools(manifest)[0]
    # spec is untouched, including keys that collide with AI4IA manifest keys.
    assert planned["openapi"]["spec"] == spec
    # auth (a real, AI4IA/SDK-known nested shape) is still correctly snake_cased.
    assert planned["openapi"]["auth"]["security_scheme"]["project_connection_id"] == "conn-1"
    assert "securityScheme" not in planned["openapi"]["auth"]


def test_schema_rejects_root_level_azure_ai_search_fields_and_bare_browser_automation():
    # Direct regression guard for the exact shape that silently mis-provisioned before: root-
    # level indexName/projectConnectionId on azure_ai_search, and browser_automation_preview
    # with no connection at all. The schema must reject both and accept the correct nested shape.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    bad_search = {
        **_valid_manifest(),
        "tools": [{"type": "azure_ai_search", "name": "x", "indexName": "i", "projectConnectionId": "c"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_search, schema)

    bad_browser = {**_valid_manifest(), "tools": [{"type": "browser_automation_preview", "name": "b"}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_browser, schema)

    good = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {"indexes": [{"indexName": "i", "projectConnectionId": "c"}]},
            },
            {
                "type": "browser_automation_preview",
                "name": "b",
                "browserAutomationPreview": {"connection": {"projectConnectionId": "c"}},
            },
        ],
    }
    jsonschema.validate(good, schema)  # must not raise


def test_schema_rejects_azure_ai_search_empty_incomplete_or_multiple_indexes():
    # A tool with no indexes, an index entry missing either required field, or more than one
    # index is inert, unprovisionable, or unsupported by the SDK; the schema must reject all
    # of these instead of silently accepting them.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    empty_indexes = {
        **_valid_manifest(),
        "tools": [{"type": "azure_ai_search", "name": "x", "azureAiSearch": {"indexes": []}}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(empty_indexes, schema)

    missing_index_name = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {"indexes": [{"projectConnectionId": "c"}]},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_index_name, schema)

    # Round 6: indexName and projectConnectionId are BOTH Required per Microsoft's Azure AI
    # Search tool "Configure tool parameters" doc -- indexName alone (no connection to
    # resolve it against) used to validate but produces an unresolved-resource failure at
    # provisioning time. The schema must reject this direction too, not just the reverse.
    missing_project_connection_id = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {"indexes": [{"indexName": "docs"}]},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_project_connection_id, schema)

    # azure-ai-projects' AzureAISearchToolResource.indexes docstring: "There can be a maximum
    # of 1 index resource attached to the agent." A second index is not a richer config, it's
    # unrepresentable -- the SDK constructor accepts the list but the service only honors one.
    # This holds for indexAssetId-shaped entries too, not just indexName+projectConnectionId.
    two_indexes = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {
                    "indexes": [
                        {"indexName": "docs", "projectConnectionId": "search-conn"},
                        {"indexName": "docs2", "projectConnectionId": "search-conn"},
                    ]
                },
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(two_indexes, schema)

    two_index_asset_ids = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {"indexes": [{"indexAssetId": "asset-1"}, {"indexAssetId": "asset-2"}]},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(two_index_asset_ids, schema)


def test_schema_azure_ai_search_index_asset_id_is_mutually_exclusive_with_index_name():
    # Round 7 (reversing round 6): `indexAssetId` (azure-ai-projects 2.3.0's
    # AISearchIndexResource.index_asset_id) is now modeled as a documented, schema-enforced
    # mutually-exclusive ALTERNATIVE to indexName+projectConnectionId, per explicit product
    # direction -- even though no current Microsoft Learn doc for this tool demonstrates it
    # (see foundry/toolbox.manifest.schema.json's azureAiSearch.indexes description and
    # docs/foundry-toolbox.md). Exactly one of the two shapes must be present per index; both
    # together, or neither, must still fail.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    index_asset_id_alone = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {"indexes": [{"indexAssetId": "asset-1"}]},
            }
        ],
    }
    jsonschema.validate(index_asset_id_alone, schema)  # must NOT raise

    # The shared optional fields (queryType/topK/filter) are still allowed alongside indexAssetId.
    index_asset_id_with_optional_fields = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {"indexes": [{"indexAssetId": "asset-1", "topK": 5, "queryType": "vector"}]},
            }
        ],
    }
    jsonschema.validate(index_asset_id_with_optional_fields, schema)  # must NOT raise

    # Both shapes on the same index entry is not a richer config -- it is an unresolvable/
    # contradictory reference the SDK has no defined behavior for. Must still be rejected.
    both_together = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {
                    "indexes": [
                        {"indexAssetId": "asset-1", "indexName": "docs", "projectConnectionId": "search-conn"}
                    ]
                },
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(both_together, schema)

    both_together_partial = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "azure_ai_search",
                "name": "x",
                "azureAiSearch": {"indexes": [{"indexAssetId": "asset-1", "indexName": "docs"}]},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(both_together_partial, schema)

    neither = {
        **_valid_manifest(),
        "tools": [
            {"type": "azure_ai_search", "name": "x", "azureAiSearch": {"indexes": [{"topK": 5}]}},
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(neither, schema)


def test_provisioner_constructs_azure_ai_search_index_asset_id_against_real_sdk(monkeypatch):
    # Schema acceptance alone doesn't prove the provisioner maps indexAssetId to the SDK's
    # actual constructor kwarg; construct it against the real locked SDK model.
    m = pytest.importorskip("azure.ai.projects.models")
    tool = {
        "type": "azure_ai_search",
        "name": "x",
        "azureAiSearch": {"indexes": [{"indexAssetId": "asset-1", "topK": 5}]},
    }
    fields = {k: v for k, v in _tb._convert_keys(tool).items() if k != "type"}
    built = m.AzureAISearchToolboxTool(**fields)
    assert built.azure_ai_search["indexes"][0]["index_asset_id"] == "asset-1"
    assert built.azure_ai_search["indexes"][0]["top_k"] == 5


def test_schema_rejects_openapi_missing_required_nested_fields_and_bad_auth():
    # openapi.name/spec/auth are all required (OpenApiFunctionDefinition); a project_connection
    # auth without securityScheme.projectConnectionId is unusable at runtime. All must be rejected.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    missing_spec_and_auth = {
        **_valid_manifest(),
        "tools": [{"type": "openapi", "name": "o", "openapi": {"name": "x"}}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_spec_and_auth, schema)

    bad_project_connection_auth = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "openapi",
                "name": "o",
                "openapi": {
                    "name": "x",
                    "spec": {"openapi": "3.0.0"},
                    "auth": {"type": "project_connection"},
                },
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_project_connection_auth, schema)

    bad_managed_identity_auth = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "openapi",
                "name": "o",
                "openapi": {
                    "name": "x",
                    "spec": {"openapi": "3.0.0"},
                    "auth": {"type": "managed_identity"},
                },
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_managed_identity_auth, schema)

    good = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "openapi",
                "name": "o",
                "openapi": {
                    "name": "x",
                    "spec": {"openapi": "3.0.0"},
                    "auth": {"type": "project_connection", "securityScheme": {"projectConnectionId": "c"}},
                },
            }
        ],
    }
    jsonschema.validate(good, schema)  # must not raise


def test_schema_rejects_mcp_missing_both_server_url_and_connector_id():
    # MCPToolboxTool requires server_label plus ONE of server_url/connector_id; a tool with
    # neither is unreachable. Both individually-valid forms must still pass.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    neither = {**_valid_manifest(), "tools": [{"type": "mcp", "serverLabel": "x"}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(neither, schema)

    with_url = {**_valid_manifest(), "tools": [{"type": "mcp", "serverLabel": "x", "serverUrl": "https://x/mcp"}]}
    jsonschema.validate(with_url, schema)  # must not raise

    with_connector = {
        **_valid_manifest(),
        "tools": [{"type": "mcp", "serverLabel": "x", "connectorId": "connector_sharepoint"}],
    }
    jsonschema.validate(with_connector, schema)  # must not raise


def test_schema_rejects_unknown_tool_property_and_cross_type_field_pollution():
    # additionalProperties:false at the tool level must reject typos/unknown fields, and each
    # type's if/then branch must reject another type's nested field showing up on the wrong tool
    # (e.g. an azureAiSearch block on a code_interpreter tool -- a copy/paste mistake that would
    # otherwise silently no-op instead of erroring).
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    unknown_property = {**_valid_manifest(), "tools": [{"type": "web_search", "name": "x", "bogusField": 1}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(unknown_property, schema)

    cross_type_pollution = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "code_interpreter",
                "name": "x",
                "azureAiSearch": {"indexes": [{"indexName": "i"}]},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(cross_type_pollution, schema)


def test_schema_accepts_web_search_custom_search_configuration_and_rejects_incomplete_or_misplaced():
    # Round 6 finding: azure-ai-projects 2.3.0's WebSearchToolboxTool.custom_search_configuration
    # (WebSearchConfiguration: project_connection_id + instance_name, both Required with no SDK
    # default) was already snake_cased by the provisioner's _CAMEL_TO_SNAKE table, but the schema
    # rejected it outright -- a manifest the provisioner could actually construct was unusable.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    good = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "web_search",
                "name": "x",
                "customSearchConfiguration": {
                    "projectConnectionId": "bing-custom-conn",
                    "instanceName": "my-custom-search",
                },
            }
        ],
    }
    jsonschema.validate(good, schema)  # must not raise

    missing_instance_name = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "web_search",
                "name": "x",
                "customSearchConfiguration": {"projectConnectionId": "bing-custom-conn"},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_instance_name, schema)

    missing_project_connection_id = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "web_search",
                "name": "x",
                "customSearchConfiguration": {"instanceName": "my-custom-search"},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_project_connection_id, schema)

    # customSearchConfiguration is web_search-only; every other type's if/then branch must
    # reject it too, the same cross-type-pollution guard as azureAiSearch/container/etc.
    on_wrong_type = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "code_interpreter",
                "name": "x",
                "customSearchConfiguration": {
                    "projectConnectionId": "bing-custom-conn",
                    "instanceName": "my-custom-search",
                },
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(on_wrong_type, schema)


def test_schema_accepts_code_interpreter_network_policy_and_rejects_incomplete_or_secret_fields():
    # Round 6 finding: azure-ai-projects 2.3.0's AutoCodeInterpreterToolParam.network_policy
    # (ContainerNetworkPolicyParam: disabled | allowlist) was already snake_cased by the
    # provisioner, but the schema rejected `container.networkPolicy` outright.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    disabled = {
        **_valid_manifest(),
        "tools": [
            {"type": "code_interpreter", "name": "x", "container": {"type": "auto", "networkPolicy": {"type": "disabled"}}}
        ],
    }
    jsonschema.validate(disabled, schema)  # must not raise

    allowlist = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "code_interpreter",
                "name": "x",
                "container": {
                    "type": "auto",
                    "networkPolicy": {"type": "allowlist", "allowedDomains": ["pypi.org"]},
                },
            }
        ],
    }
    jsonschema.validate(allowlist, schema)  # must not raise

    allowlist_missing_domains = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "code_interpreter",
                "name": "x",
                "container": {"type": "auto", "networkPolicy": {"type": "allowlist"}},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(allowlist_missing_domains, schema)

    allowlist_empty_domains = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "code_interpreter",
                "name": "x",
                "container": {"type": "auto", "networkPolicy": {"type": "allowlist", "allowedDomains": []}},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(allowlist_empty_domains, schema)

    # domainSecrets carries a literal secret VALUE per domain (azure-ai-projects 2.3.0's
    # ContainerNetworkPolicyDomainSecretParam.value); this manifest is committed to source
    # control, so the schema must never accept it (AGENTS.md "no secret sprawl").
    with_domain_secrets = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "code_interpreter",
                "name": "x",
                "container": {
                    "type": "auto",
                    "networkPolicy": {
                        "type": "allowlist",
                        "allowedDomains": ["pypi.org"],
                        "domainSecrets": [{"domain": "pypi.org", "name": "token", "value": "shh"}],
                    },
                },
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_domain_secrets, schema)


# ----------------- live-SDK guard: real model construction (gated, optional dep) ------
def _install_fake_ai_project_client(monkeypatch, *, returned_version, captured):
    """Patch azure.ai.projects.AIProjectClient / azure.identity.DefaultAzureCredential with
    fakes that record calls, so create_toolbox() can be exercised without network access
    while still constructing REAL azure.ai.projects.models instances (only the client/
    credential/transport are faked, not the SDK's own model classes).
    """

    class _FakeToolboxesOps:
        def create_version(self, name, **kwargs):
            captured["create_version"] = (name, kwargs)
            return SimpleNamespace(version=returned_version)

        def update(self, name, **kwargs):
            captured.setdefault("update_calls", []).append((name, kwargs))
            return SimpleNamespace(name=name, default_version=kwargs.get("default_version"))

    class _FakeAIProjectClient:
        def __init__(self, *, endpoint, credential):
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            self.toolboxes = _FakeToolboxesOps()

    class _FakeCredential:
        pass

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", _FakeAIProjectClient)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _FakeCredential)


def test_create_toolbox_constructs_real_sdk_models_with_nested_fields_populated(monkeypatch):
    # The load-bearing SDK-construction guard: run create_toolbox() against the example
    # manifest's fixed shapes and inspect the REAL azure.ai.projects model instances it
    # builds. This is what actually failed silently before (SDK constructor kwargs it
    # didn't recognize were just dropped, producing None fields with no exception).
    pytest.importorskip("azure.ai.projects")
    pytest.importorskip("azure.identity")
    from azure.ai.projects import models as m

    captured: dict = {}
    _install_fake_ai_project_client(monkeypatch, returned_version="1", captured=captured)

    manifest = _tb.load_manifest(_EXAMPLE_MANIFEST)
    _tb.create_toolbox(manifest, _ENDPOINT)

    _, kwargs = captured["create_version"]
    tools = kwargs["tools"]

    search_tool = next(t for t in tools if isinstance(t, m.AzureAISearchToolboxTool))
    assert search_tool.azure_ai_search is not None
    idx = search_tool.azure_ai_search.indexes[0]
    assert idx.index_name == "ai4ia-docs"
    assert idx.project_connection_id == "ai4ia-search"

    browser_tool = next(t for t in tools if isinstance(t, m.BrowserAutomationPreviewToolboxTool))
    assert browser_tool.browser_automation_preview is not None
    conn = browser_tool.browser_automation_preview.connection
    assert conn is not None
    assert conn.project_connection_id == "ai4ia-browser-automation"

    # customSearchConfiguration (round 6): must reach WebSearchToolboxTool.custom_search_configuration
    # as a real WebSearchConfiguration, not a raw dict, with both required fields populated.
    web_search_tools = [t for t in tools if isinstance(t, m.WebSearchToolboxTool)]
    custom_web_search = next(t for t in web_search_tools if t.custom_search_configuration is not None)
    assert custom_web_search.custom_search_configuration.project_connection_id == "ai4ia-bing-custom-search"
    assert custom_web_search.custom_search_configuration.instance_name == "ai4ia-custom-search"

    # container.networkPolicy (round 6): must reach AutoCodeInterpreterToolParam.network_policy
    # with allowedDomains correctly snake_cased to allowed_domains (the exact silent-corruption
    # risk this round's fix closes -- see test_plan_tools_snake_cases_... above).
    ci_tools = [t for t in tools if isinstance(t, m.CodeInterpreterToolboxTool)]
    custom_ci = next(t for t in ci_tools if t.container is not None)
    assert custom_ci.container.network_policy.type == "allowlist"
    assert custom_ci.container.network_policy.allowed_domains == ["pypi.org", "files.pythonhosted.org"]

    # openapi.spec (OpenApiFunctionDefinition.spec: dict[str, Any]) must reach the real SDK
    # model completely unconverted, even though it sits next to auth's SDK-known nested shape.
    openapi_tool = next(t for t in tools if isinstance(t, m.OpenApiToolboxTool))
    example_manifest = json.loads(_EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    original_spec = next(t for t in example_manifest["tools"] if t["type"] == "openapi")["openapi"]["spec"]
    assert openapi_tool.openapi.spec == original_spec


def test_create_toolbox_passes_openapi_spec_through_untouched_even_with_colliding_keys(monkeypatch):
    # Direct regression guard for the exact failure mode: a spec property genuinely named like an
    # AI4IA manifest key (topK, indexName) must reach OpenApiFunctionDefinition.spec unchanged.
    pytest.importorskip("azure.ai.projects")
    pytest.importorskip("azure.identity")
    from azure.ai.projects import models as m

    captured: dict = {}
    _install_fake_ai_project_client(monkeypatch, returned_version="1", captured=captured)

    spec = {
        "openapi": "3.0.0",
        "components": {"schemas": {"Result": {"properties": {"topK": {"type": "integer"}, "indexName": {"type": "string"}}}}},
    }
    manifest = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "openapi",
                "name": "weird-api",
                "openapi": {"name": "search_docs", "auth": {"type": "anonymous"}, "spec": spec},
            }
        ],
    }
    _tb.create_toolbox(manifest, _ENDPOINT)

    _, kwargs = captured["create_version"]
    openapi_tool = next(t for t in kwargs["tools"] if isinstance(t, m.OpenApiToolboxTool))
    assert openapi_tool.openapi.spec == spec


@pytest.mark.parametrize("returned_version", ["1", "3"])
def test_create_toolbox_activates_returned_version_as_default(monkeypatch, returned_version):
    # create_version() only creates an immutable version; it does NOT change what the
    # toolbox's MCP endpoint serves. create_toolbox() must always explicitly activate the
    # version it just created via toolboxes.update(name, default_version=<that version>),
    # both on first create and on later versions (no special-casing "first time only").
    pytest.importorskip("azure.ai.projects")
    pytest.importorskip("azure.identity")

    captured: dict = {}
    _install_fake_ai_project_client(monkeypatch, returned_version=returned_version, captured=captured)

    manifest = _valid_manifest()
    _tb.create_toolbox(manifest, _ENDPOINT)

    assert captured["update_calls"] == [(manifest["name"], {"default_version": returned_version})]


def test_create_toolbox_fails_loud_when_create_version_has_no_version(monkeypatch):
    # If the SDK ever returns a result with no usable `version`, silently skipping
    # activation would leave a new version created but never served with no signal to the
    # operator. Must raise instead of continuing.
    pytest.importorskip("azure.ai.projects")
    pytest.importorskip("azure.identity")

    captured: dict = {}
    _install_fake_ai_project_client(monkeypatch, returned_version=None, captured=captured)

    with pytest.raises(SystemExit):
        _tb.create_toolbox(_valid_manifest(), _ENDPOINT)
    assert "update_calls" not in captured


# ----------------- provisioner-side schema enforcement (Finding 3, round 4) ------------
def test_validate_manifest_schema_reports_errors_for_invalid_manifest():
    pytest.importorskip("jsonschema")
    bad = {**_valid_manifest(), "tools": [{"type": "mcp", "serverLabel": "x"}]}
    errors = _tb.validate_manifest_schema(bad)
    assert errors is not None and errors != []

    assert _tb.validate_manifest_schema(_valid_manifest()) == []


def test_validate_manifest_schema_returns_none_when_jsonschema_unavailable(monkeypatch):
    # Deterministic regardless of whether jsonschema happens to be installed in this
    # environment: force the `import jsonschema` inside validate_manifest_schema() to raise
    # ImportError (sys.modules[name] = None is the standard trick) and confirm the function
    # reports "unknown" (None) rather than raising or silently treating it as valid.
    import sys

    monkeypatch.setitem(sys.modules, "jsonschema", None)
    assert _tb.validate_manifest_schema(_valid_manifest()) is None


def test_main_hard_fails_create_when_jsonschema_unavailable(tmp_path, monkeypatch):
    # Finding 3 (round 4): the provisioner must actually APPLY the schema before SDK
    # construction, not just rely on CI's separate check-jsonschema lint step. Since
    # --create needs jsonschema to do that, it must refuse to run (not silently skip
    # validation) when the optional dependency is missing -- regardless of whether the
    # manifest itself happens to be valid.
    import sys

    monkeypatch.setitem(sys.modules, "jsonschema", None)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_valid_manifest()), encoding="utf-8")

    rc = _tb.main(["--manifest", str(manifest_path), "--create"])
    assert rc == 1


def test_main_blocks_on_schema_violation_before_any_sdk_construction(tmp_path, capsys):
    # End-to-end guard for the literal Finding 3 complaint ("provisioner doesn't apply the
    # JSON schema"): main() must reject a manifest that passes the hand-written
    # validate_manifest() checks but violates the strict per-type schema (here: an mcp tool
    # missing both serverUrl/connectorId), and must do so WITHOUT ever attempting SDK
    # construction (no --create-only azure-ai-projects/azure-identity dependency needed here
    # since it fails before reaching create_toolbox()).
    pytest.importorskip("jsonschema")
    bad_manifest = {
        "name": "bad-toolbox",
        "description": "d",
        "tools": [{"type": "mcp", "serverLabel": "x"}],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")

    rc = _tb.main(["--manifest", str(manifest_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "schema:" in captured.err


def test_main_accepts_schema_valid_manifest_in_dry_run(tmp_path, capsys):
    # Sanity complement to the previous test: a schema-VALID manifest must still sail
    # through the dry-run path (no false positives from the new strict schema).
    pytest.importorskip("jsonschema")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_valid_manifest()), encoding="utf-8")

    rc = _tb.main(["--manifest", str(manifest_path), "--project-endpoint", _ENDPOINT])
    captured = capsys.readouterr()
    assert rc == 0
    assert "dry run" in captured.out


# ----------------- round 10: malformed-shape crash guard (Finding 3) -------------------
# The old main() ran the handwritten validate_manifest() BEFORE the inherently type-safe
# jsonschema-based validate_manifest_schema(), so a malformed root shape crashed with an
# unhandled AttributeError/TypeError instead of a clean "not ready to provision" message.
# These cases must report cleanly, both with jsonschema available (schema catches it first)
# and without it (validate_manifest()'s own isinstance() guards catch it as a fallback).
@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param([], id="root_is_a_list"),
        pytest.param(None, id="root_is_null"),
        pytest.param("ai4ia-toolbox", id="root_is_a_string"),
        pytest.param({**_valid_manifest(), "tools": [None]}, id="tools_contains_null"),
        pytest.param({**_valid_manifest(), "tools": "web_search"}, id="tools_is_a_string"),
        pytest.param({**_valid_manifest(), "tools": [{"name": "x"}, "not-a-dict"]}, id="tools_has_non_dict_entry"),
    ],
)
def test_main_reports_malformed_manifests_cleanly_instead_of_crashing(tmp_path, capsys, manifest):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rc = _tb.main(["--manifest", str(manifest_path)])  # must not raise

    captured = capsys.readouterr()
    assert rc == 1
    assert "not ready to provision" in captured.err


@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param([], id="root_is_a_list"),
        pytest.param({**_valid_manifest(), "tools": [None]}, id="tools_contains_null"),
    ],
)
def test_main_reports_malformed_manifests_cleanly_even_without_jsonschema(tmp_path, capsys, monkeypatch, manifest):
    # Forces validate_manifest_schema() to report "unavailable" (None) so validate_manifest()'s
    # own isinstance() guards are the ONLY thing standing between a malformed manifest and a
    # crash -- proving the hardening is not merely redundant with the schema check.
    import sys

    monkeypatch.setitem(sys.modules, "jsonschema", None)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rc = _tb.main(["--manifest", str(manifest_path)])  # must not raise

    captured = capsys.readouterr()
    assert rc == 1
    assert "not ready to provision" in captured.err


def test_validate_manifest_isinstance_guards_cover_every_malformed_shape():
    # Direct unit coverage of validate_manifest() itself (independent of main()/schema).
    assert "manifest must be a JSON object" in _tb.validate_manifest([])[0]
    assert "manifest must be a JSON object" in _tb.validate_manifest(None)[0]
    assert "manifest must be a JSON object" in _tb.validate_manifest("nope")[0]

    tools_not_list = _tb.validate_manifest({**_valid_manifest(), "tools": "web_search"})
    assert any("`tools` must be a JSON array" in e for e in tools_not_list)

    tool_is_null = _tb.validate_manifest({**_valid_manifest(), "tools": [None]})
    assert any("tools[0] must be a JSON object" in e for e in tool_is_null)

    tool_is_string = _tb.validate_manifest({**_valid_manifest(), "tools": [{"type": "web_search", "name": "w"}, "nope"]})
    assert any("tools[1] must be a JSON object" in e for e in tool_is_string)


# ----------------- round 7: new SDK 2.3.0 toolbox types --------------------------------
# a2a_preview, fabric_iq_preview, work_iq_preview, reminder_preview were newly added to
# azure-ai-projects 2.3.0's toolbox model set but omitted from _TYPE_TO_MODEL / the schema /
# the example manifest before this round. Each gets a schema positive+negative case AND a
# real-SDK construction check (both directions matter: the schema alone doesn't prove the
# provisioner's camelCase mapping actually reaches the SDK constructor).
def test_schema_and_sdk_accept_a2a_preview_via_project_connection_id_or_base_url():
    # A2APreviewToolboxTool requires AT LEAST ONE of project_connection_id / base_url (the SDK
    # constructor enforces no exclusivity at all -- both may be set together); all three forms
    # (connection-only, base-url-only, and both together) must validate AND construct against
    # the real locked SDK.
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    via_connection = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "a2a_preview",
                "name": "a2a",
                "projectConnectionId": "a2a-conn",
                "agentCardPath": "/.well-known/agent-card.json",
            }
        ],
    }
    jsonschema.validate(via_connection, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(via_connection["tools"][0]).items() if k != "type"}
    built = m.A2APreviewToolboxTool(**fields)
    assert built.project_connection_id == "a2a-conn"
    assert built.agent_card_path == "/.well-known/agent-card.json"

    via_base_url = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "a2a_preview",
                "name": "a2a",
                "baseUrl": "https://agent.example.com",
                "sendCredentialsForAgentCard": True,
            }
        ],
    }
    jsonschema.validate(via_base_url, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(via_base_url["tools"][0]).items() if k != "type"}
    built = m.A2APreviewToolboxTool(**fields)
    assert built.base_url == "https://agent.example.com"
    assert built.send_credentials_for_agent_card is True

    # Round-10 finding: docs previously claimed "exactly one" of projectConnectionId/baseUrl,
    # implying the other is rejected when the first is present. Neither the schema (`anyOf`, not
    # `oneOf`) nor the SDK constructor (a bare passthrough with no cross-field validation) enforce
    # that -- both together must validate and construct cleanly too.
    both = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "a2a_preview",
                "name": "a2a",
                "projectConnectionId": "a2a-conn",
                "baseUrl": "https://agent.example.com",
            }
        ],
    }
    jsonschema.validate(both, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(both["tools"][0]).items() if k != "type"}
    built = m.A2APreviewToolboxTool(**fields)
    assert built.project_connection_id == "a2a-conn"
    assert built.base_url == "https://agent.example.com"


def test_schema_and_sdk_reject_a2a_preview_missing_connection_and_foreign_fields():
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    neither = {**_valid_manifest(), "tools": [{"type": "a2a_preview", "name": "a2a"}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(neither, schema)

    # serverLabel belongs to mcp/fabric_iq_preview, not a2a_preview; the schema's per-type
    # exclusion list AND the real SDK constructor must both reject it.
    with_foreign_field = {
        **_valid_manifest(),
        "tools": [
            {"type": "a2a_preview", "name": "a2a", "projectConnectionId": "a2a-conn", "serverLabel": "nope"}
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_foreign_field, schema)
    fields = {k: v for k, v in _tb._convert_keys(with_foreign_field["tools"][0]).items() if k != "type"}
    with pytest.raises(TypeError):
        m.A2APreviewToolboxTool(**fields)


def test_schema_and_sdk_accept_fabric_iq_preview_and_reject_missing_connection():
    # FabricIQPreviewToolboxTool requires project_connection_id; server_label/server_url/
    # require_approval are optional and SHARED field names with mcp (unlike work_iq_preview,
    # which excludes them entirely -- see next test).
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    good = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "fabric_iq_preview",
                "name": "fabric",
                "projectConnectionId": "fabric-conn",
                "serverLabel": "fabric-iq",
                "requireApproval": "never",
            }
        ],
    }
    jsonschema.validate(good, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(good["tools"][0]).items() if k != "type"}
    built = m.FabricIQPreviewToolboxTool(**fields)
    assert built.project_connection_id == "fabric-conn"
    assert built.server_label == "fabric-iq"
    assert built.require_approval == "never"

    missing_connection = {
        **_valid_manifest(),
        "tools": [{"type": "fabric_iq_preview", "name": "fabric", "serverLabel": "fabric-iq"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_connection, schema)

    # allowedTools/deferLoading are mcp-only, NOT accepted by fabric_iq_preview (unlike
    # serverLabel/serverUrl/requireApproval, which fabric_iq_preview shares with mcp).
    with_mcp_only_field = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "fabric_iq_preview",
                "name": "fabric",
                "projectConnectionId": "fabric-conn",
                "allowedTools": ["x"],
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_mcp_only_field, schema)
    fields = {k: v for k, v in _tb._convert_keys(with_mcp_only_field["tools"][0]).items() if k != "type"}
    with pytest.raises(TypeError):
        m.FabricIQPreviewToolboxTool(**fields)


def test_schema_and_sdk_accept_work_iq_preview_and_reject_extra_fields():
    # WorkIQPreviewToolboxTool has exactly ONE field: project_connection_id. Unlike
    # fabric_iq_preview, server_label/server_url/require_approval are NOT accepted either.
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    good = {
        **_valid_manifest(),
        "tools": [{"type": "work_iq_preview", "name": "work", "projectConnectionId": "work-conn"}],
    }
    jsonschema.validate(good, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(good["tools"][0]).items() if k != "type"}
    built = m.WorkIQPreviewToolboxTool(**fields)
    assert built.project_connection_id == "work-conn"

    missing_connection = {**_valid_manifest(), "tools": [{"type": "work_iq_preview", "name": "work"}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_connection, schema)

    with_server_label = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "work_iq_preview",
                "name": "work",
                "projectConnectionId": "work-conn",
                "serverLabel": "not-allowed-here",
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_server_label, schema)
    fields = {k: v for k, v in _tb._convert_keys(with_server_label["tools"][0]).items() if k != "type"}
    with pytest.raises(TypeError):
        m.WorkIQPreviewToolboxTool(**fields)


def test_schema_and_sdk_accept_reminder_preview_with_no_type_specific_fields():
    # ReminderPreviewToolboxTool carries no tool-specific fields at all (only the common
    # type/name/description/toolConfigs); it must still validate and construct cleanly, and a
    # type-specific field belonging to another type must be rejected both ways.
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    good = {**_valid_manifest(), "tools": [{"type": "reminder_preview", "name": "reminder"}]}
    jsonschema.validate(good, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(good["tools"][0]).items() if k != "type"}
    built = m.ReminderPreviewToolboxTool(**fields)
    assert built.type == "reminder_preview"

    with_foreign_field = {
        **_valid_manifest(),
        "tools": [{"type": "reminder_preview", "name": "reminder", "projectConnectionId": "x"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_foreign_field, schema)
    fields = {k: v for k, v in _tb._convert_keys(with_foreign_field["tools"][0]).items() if k != "type"}
    with pytest.raises(TypeError):
        m.ReminderPreviewToolboxTool(**fields)


# ----------------- round 7: requireApproval / allowedTools / toolConfigs strict shapes ---
def test_schema_require_approval_accepts_literals_and_object_form_rejects_bogus_shapes():
    # requireApproval must be exactly one of: the literal "always", the literal "never", or an
    # object with an "always" and/or "never" key holding an mcpToolFilter (toolNames/readOnly).
    # Anything else (a bogus literal, an empty object, an unknown object key) must be rejected.
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    def mcp_tool(require_approval):
        return {
            **_valid_manifest(),
            "tools": [
                {
                    "type": "mcp",
                    "serverLabel": "x",
                    "serverUrl": "https://x/mcp",
                    "requireApproval": require_approval,
                }
            ],
        }

    for literal in ("always", "never"):
        manifest = mcp_tool(literal)
        jsonschema.validate(manifest, schema)  # must not raise
        fields = {k: v for k, v in _tb._convert_keys(manifest["tools"][0]).items() if k != "type"}
        assert m.MCPToolboxTool(**fields).require_approval == literal

    object_form = mcp_tool({"never": {"toolNames": ["search_docs"], "readOnly": True}})
    jsonschema.validate(object_form, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(object_form["tools"][0]).items() if k != "type"}
    built = m.MCPToolboxTool(**fields)
    assert built.require_approval == {"never": {"tool_names": ["search_docs"], "read_only": True}}

    both_keys = mcp_tool({"always": {"toolNames": ["a"]}, "never": {"readOnly": True}})
    jsonschema.validate(both_keys, schema)  # must not raise: always+never together is valid

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mcp_tool("sometimes"), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mcp_tool({}), schema)  # minProperties: 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mcp_tool({"maybe": {"toolNames": ["a"]}}), schema)  # unknown key
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mcp_tool({"always": "not-an-object"}), schema)


def test_schema_allowed_tools_accepts_array_or_filter_object_rejects_bogus_shapes():
    # allowedTools is either a plain non-empty array of tool-name strings, or an mcpToolFilter
    # object (toolNames/readOnly) restricting which discovered tools are auto-approved for
    # loading -- distinct from, and independent of, requireApproval's own filter shape.
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    def mcp_tool(allowed_tools):
        return {
            **_valid_manifest(),
            "tools": [
                {"type": "mcp", "serverLabel": "x", "serverUrl": "https://x/mcp", "allowedTools": allowed_tools}
            ],
        }

    array_form = mcp_tool(["search_docs", "get_doc"])
    jsonschema.validate(array_form, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(array_form["tools"][0]).items() if k != "type"}
    assert m.MCPToolboxTool(**fields).allowed_tools == ["search_docs", "get_doc"]

    object_form = mcp_tool({"toolNames": ["search_docs"], "readOnly": True})
    jsonschema.validate(object_form, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(object_form["tools"][0]).items() if k != "type"}
    assert m.MCPToolboxTool(**fields).allowed_tools == {"tool_names": ["search_docs"], "read_only": True}

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mcp_tool([]), schema)  # minItems: 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mcp_tool([1, 2]), schema)  # array items must be strings
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mcp_tool({"bogusKey": True}), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mcp_tool({}), schema)  # minProperties: 1


def test_schema_tool_configs_accepts_pin_and_additional_search_text_rejects_unknown_keys():
    # toolConfigs (common to every tool type -- ToolConfig: pin + additional_search_text,
    # keyed by tool name or "*" for the catch-all default) must accept both known sub-fields
    # and reject an unrecognized one under a tool-name key.
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    good = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "web_search",
                "name": "web",
                "toolConfigs": {"*": {"pin": False}, "web": {"pin": True, "additionalSearchText": "extra context"}},
            }
        ],
    }
    jsonschema.validate(good, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(good["tools"][0]).items() if k != "type"}
    built = m.WebSearchToolboxTool(**fields)
    assert built.tool_configs == {
        "*": {"pin": False},
        "web": {"pin": True, "additional_search_text": "extra context"},
    }

    bad = {
        **_valid_manifest(),
        "tools": [{"type": "web_search", "name": "web", "toolConfigs": {"web": {"bogusKey": 1}}}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


# ----------------- round 7: web_search / file_search new optional fields ----------------
def test_schema_web_search_accepts_filters_user_location_search_context_size():
    # web_search's filters (allowedDomains-only) is a DIFFERENT shape from file_search's
    # comparison/compound filter tree (see next test) even though both are named "filters" --
    # each type's shape must be enforced independently, not just "some object accepted".
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    good = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "web_search",
                "name": "web",
                "filters": {"allowedDomains": ["learn.microsoft.com"]},
                "userLocation": {"country": "US", "city": "Redmond", "region": "Washington"},
                "searchContextSize": "medium",
            }
        ],
    }
    jsonschema.validate(good, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(good["tools"][0]).items() if k != "type"}
    built = m.WebSearchToolboxTool(**fields)
    assert built.filters == {"allowed_domains": ["learn.microsoft.com"]}
    assert built.user_location["country"] == "US"
    assert built.search_context_size == "medium"

    # file_search's comparison-filter shape ({"type": "eq", ...}) must NOT validate as
    # web_search's filters (which only accepts allowedDomains).
    wrong_filter_shape = {
        **_valid_manifest(),
        "tools": [
            {"type": "web_search", "name": "web", "filters": {"type": "eq", "key": "language", "value": "en"}}
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(wrong_filter_shape, schema)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                **_valid_manifest(),
                "tools": [{"type": "web_search", "name": "web", "searchContextSize": "extreme"}],
            },
            schema,
        )  # not a member of the low/medium/high enum

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                **_valid_manifest(),
                # "type" is SDK-auto-set on WebSearchUserLocation and not caller-settable.
                "tools": [{"type": "web_search", "name": "web", "userLocation": {"type": "approximate"}}],
            },
            schema,
        )

    # These fields are web_search-only; another type carrying them must be rejected.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {**_valid_manifest(), "tools": [{"type": "file_search", "name": "fs", "searchContextSize": "low"}]},
            schema,
        )


def test_schema_file_search_accepts_max_num_results_ranking_options_and_filters():
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    good = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "file_search",
                "name": "fs",
                "vectorStoreIds": ["vs1"],
                "maxNumResults": 10,
                "rankingOptions": {
                    "ranker": "auto",
                    "scoreThreshold": 0.5,
                    "hybridSearch": {"embeddingWeight": 0.6, "textWeight": 0.4},
                },
                "filters": {
                    "type": "and",
                    "filters": [
                        {"type": "eq", "key": "language", "value": "en"},
                        {"type": "eq", "key": "docType", "value": "runbook"},
                    ],
                },
            }
        ],
    }
    jsonschema.validate(good, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(good["tools"][0]).items() if k != "type"}
    built = m.FileSearchToolboxTool(**fields)
    assert built.max_num_results == 10
    assert built.ranking_options["hybrid_search"] == {"embedding_weight": 0.6, "text_weight": 0.4}
    assert built.filters["filters"][0] == {"type": "eq", "key": "language", "value": "en"}

    for bad_max in (0, 51):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                {
                    **_valid_manifest(),
                    "tools": [{"type": "file_search", "name": "fs", "maxNumResults": bad_max}],
                },
                schema,
            )

    incomplete_hybrid_search = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "file_search",
                "name": "fs",
                "rankingOptions": {"hybridSearch": {"embeddingWeight": 0.6}},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(incomplete_hybrid_search, schema)

    # web_search's allowedDomains-only filters shape must NOT validate as file_search's
    # comparison/compound filter tree.
    wrong_filter_shape = {
        **_valid_manifest(),
        "tools": [
            {"type": "file_search", "name": "fs", "filters": {"allowedDomains": ["learn.microsoft.com"]}}
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(wrong_filter_shape, schema)


# ----------------- round 7: mcp new optional fields + documented secret exclusions ------
def test_schema_and_sdk_accept_mcp_server_description_and_defer_loading():
    jsonschema = pytest.importorskip("jsonschema")
    m = pytest.importorskip("azure.ai.projects.models")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    good = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "mcp",
                "serverLabel": "x",
                "serverUrl": "https://x/mcp",
                "serverDescription": "Internal knowledge-base MCP server.",
                "deferLoading": True,
            }
        ],
    }
    jsonschema.validate(good, schema)  # must not raise
    fields = {k: v for k, v in _tb._convert_keys(good["tools"][0]).items() if k != "type"}
    built = m.MCPToolboxTool(**fields)
    assert built.server_description == "Internal knowledge-base MCP server."
    assert built.defer_loading is True


def test_schema_rejects_mcp_authorization_and_headers_as_documented_secrets():
    # MCPToolboxTool.authorization / .headers are real, SDK-constructible fields (verified
    # against the locked SDK) but carry literal credential material for the upstream MCP
    # server, so AI4IA deliberately never models them in the manifest schema (AGENTS.md "no
    # secret sprawl" -- see docs/foundry-toolbox.md and the reflection parity test below,
    # which pins this as an intentional exclusion rather than an accidental gap).
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    with_authorization = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "mcp",
                "serverLabel": "x",
                "serverUrl": "https://x/mcp",
                "authorization": {"type": "bearer", "token": "shh"},
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_authorization, schema)

    with_headers = {
        **_valid_manifest(),
        "tools": [
            {"type": "mcp", "serverLabel": "x", "serverUrl": "https://x/mcp", "headers": {"X-Api-Key": "shh"}}
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(with_headers, schema)


# ----------------- round 7: reflection-driven SDK field/type parity ---------------------
# The coordinator's explicit ask: rather than hand-verifying each new field individually
# (which silently drifts whenever the SDK adds/renames a field or type), enumerate every real
# azure.ai.projects.models.*ToolboxTool subclass via reflection and prove (1) every SDK
# toolbox type has a manifest "type" mapped to it in _TYPE_TO_MODEL, and (2) every field the
# SDK constructor accepts for that type has camelCase coverage in _CAMEL_TO_SNAKE (or is
# camelCase-identical, e.g. "container"/"filters"/"openapi") or is an explicitly documented
# secret exclusion -- so a field is never silently unmapped.
#
# Reflection mechanism note: inspect.signature(cls.__init__) returns a generic
# (*args, **kwargs) for these generated SDK model classes (the real parameter list only
# exists as an @overload-decorated stub for IDE ergonomics), and typing.get_type_hints()
# fails with NameError on the class's unresolved forward-reference annotation strings.
# cls.__annotations__ (the raw, unresolved dict) DOES carry the real per-class field names,
# but is not inherited -- each class in the MRO only contributes fields it newly declares --
# so the full field set requires walking cls.__mro__ and merging.
def _sdk_declared_fields(cls) -> set:
    fields: dict = {}
    for base in reversed(cls.__mro__):
        fields.update(getattr(base, "__annotations__", {}))
    fields.pop("_calculated", None)  # SDK-internal bookkeeping, not a manifest field.
    fields.pop("__mapping__", None)  # ditto (MutableMapping backing store).
    return set(fields)


def _snake_to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


# Fields azure-ai-projects 2.3.0 genuinely accepts but AI4IA deliberately never models in the
# committed manifest schema because they carry literal secret material (AGENTS.md "no secret
# sprawl"): MCPToolboxTool.authorization (bearer/API-key credentials for the upstream MCP
# server) and .headers (arbitrary, possibly-secret HTTP headers). See docs/foundry-toolbox.md
# and test_schema_rejects_mcp_authorization_and_headers_as_documented_secrets above.
_DOCUMENTED_SECRET_EXCLUSIONS = {"MCPToolboxTool": {"authorization", "headers"}}
# Fields common to every ToolboxTool subclass, already covered generically by
# test_every_allowed_type_maps_to_a_model_class / test_plan_tools_camel_to_snake /
# test_schema_tool_configs_accepts_pin_and_additional_search_text_rejects_unknown_keys, and
# intentionally excluded from the per-type comparison below.
_COMMON_TOOLBOX_FIELDS = {"type", "name", "description", "tool_configs"}


def test_reflection_driven_parity_covers_every_sdk_toolbox_type_and_field():
    pytest.importorskip("azure.ai.projects")
    from azure.ai.projects import models as m

    sdk_toolbox_classes: dict[str, type] = {}
    for class_name in dir(m):
        if not class_name.endswith("ToolboxTool") or class_name == "ToolboxTool":
            continue
        obj = getattr(m, class_name)
        if isinstance(obj, type):
            sdk_toolbox_classes[class_name] = obj
    assert len(sdk_toolbox_classes) >= 12, (
        f"expected at least the 12 known toolbox types via reflection, found: {sorted(sdk_toolbox_classes)}"
    )

    modeled_classes = set(_tb._TYPE_TO_MODEL.values())
    assert set(sdk_toolbox_classes) == modeled_classes, (
        "SDK toolbox classes and _TYPE_TO_MODEL have drifted -- "
        f"SDK-only (missing from _TYPE_TO_MODEL): {set(sdk_toolbox_classes) - modeled_classes}; "
        f"manifest-only (stale entries, no longer in SDK): {modeled_classes - set(sdk_toolbox_classes)}"
    )

    for class_name, cls in sdk_toolbox_classes.items():
        own_fields = _sdk_declared_fields(cls) - _COMMON_TOOLBOX_FIELDS
        secret_exclusions = _DOCUMENTED_SECRET_EXCLUSIONS.get(class_name, set())
        stale_exclusions = secret_exclusions - own_fields
        assert not stale_exclusions, (
            f"{class_name}: documented secret exclusion(s) no longer exist on the SDK class "
            f"(update _DOCUMENTED_SECRET_EXCLUSIONS): {stale_exclusions}"
        )

        uncovered = [
            field
            for field in sorted(own_fields - secret_exclusions)
            if _snake_to_camel(field) not in _tb._CAMEL_TO_SNAKE and _snake_to_camel(field) != field
        ]
        assert not uncovered, (
            f"{class_name}: SDK field(s) with no camelCase mapping in _CAMEL_TO_SNAKE and no "
            f"documented secret exclusion -- add a schema property + _CAMEL_TO_SNAKE entry, or "
            f"an explicit, justified _DOCUMENTED_SECRET_EXCLUSIONS entry: {uncovered}"
        )


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


# ----------------- round 8: toolConfigs map keys / strict openapi.auth ------------------
def test_convert_keys_preserves_arbitrary_tool_configs_map_keys_even_when_colliding_with_camel_to_snake():
    # Regression guard: toolConfigs (azure-ai-projects 2.3.0's ToolboxTool.tool_configs) is a
    # map keyed by ARBITRARY tool names (or "*"), not an AI4IA manifest schema shape -- but
    # _convert_keys() used to run _to_snake() over every dict key at every depth, including a
    # map's own keys. "topK" is deliberately chosen because it IS a real, pre-existing
    # _CAMEL_TO_SNAKE entry (used unrelatedly for Azure AI Search's indexes[].topK): before the
    # fix, a tool literally named "topK" in toolConfigs would be silently renamed to "top_k",
    # making the config apply to a nonexistent tool instead of the one actually named "topK".
    assert "topK" in _tb._CAMEL_TO_SNAKE  # sanity: this key really does collide

    converted = _tb._convert_keys(
        {"topK": {"additionalSearchText": "extra"}, "*": {"pin": True}},
        parent_key="toolConfigs",
    )
    assert converted == {"topK": {"additional_search_text": "extra"}, "*": {"pin": True}}

    # Same guarantee through the full manifest -> planned-tool projection.
    manifest = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "web_search",
                "name": "web",
                "toolConfigs": {"topK": {"additionalSearchText": "extra"}},
            }
        ],
    }
    planned = _tb.plan_tools(manifest)[0]
    assert planned["tool_configs"] == {"topK": {"additional_search_text": "extra"}}
    assert "top_k" not in planned["tool_configs"]

    # And against the real SDK model: the map key must survive on the constructed instance too.
    m = pytest.importorskip("azure.ai.projects.models")
    fields = {k: v for k, v in planned.items() if k != "type"}
    built = m.WebSearchToolboxTool(**fields)
    assert built.tool_configs == {"topK": {"additional_search_text": "extra"}}


def test_schema_openapi_auth_rejects_anonymous_with_extra_fields_and_security_scheme_extras():
    # Strict per-branch closure: OpenApiAnonymousAuthDetails takes NO field besides `type` (not
    # even an empty/absent securityScheme); OpenApiProjectConnectionAuthDetails/
    # OpenApiManagedAuthDetails's securityScheme has EXACTLY one field each
    # (projectConnectionId / audience) -- any additional key (e.g. a stray "token") must be
    # rejected, not silently ignored.
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8"))

    def _tool_with_auth(auth: dict) -> dict:
        return {
            **_valid_manifest(),
            "tools": [
                {
                    "type": "openapi",
                    "name": "o",
                    "openapi": {"name": "x", "spec": {"openapi": "3.0.0"}, "auth": auth},
                }
            ],
        }

    bad_shapes = [
        {"type": "anonymous", "securityScheme": {"projectConnectionId": "c"}},
        {"type": "anonymous", "token": "shh"},
        {
            "type": "project_connection",
            "securityScheme": {"projectConnectionId": "c", "token": "shh"},
        },
        {"type": "managed_identity", "securityScheme": {"audience": "a", "apiKey": "shh"}},
    ]
    for auth in bad_shapes:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_tool_with_auth(auth), schema)

    good_shapes = [
        {"type": "anonymous"},
        {"type": "project_connection", "securityScheme": {"projectConnectionId": "c"}},
        {"type": "managed_identity", "securityScheme": {"audience": "a"}},
    ]
    for auth in good_shapes:
        jsonschema.validate(_tool_with_auth(auth), schema)  # must not raise


@pytest.mark.parametrize(
    ("auth", "expected_cls_name", "expected_attrs"),
    [
        ({"type": "anonymous"}, "OpenApiAnonymousAuthDetails", {}),
        (
            {"type": "project_connection", "securityScheme": {"projectConnectionId": "conn-1"}},
            "OpenApiProjectConnectionAuthDetails",
            {"security_scheme.project_connection_id": "conn-1"},
        ),
        (
            {"type": "managed_identity", "securityScheme": {"audience": "https://api.example.com"}},
            "OpenApiManagedAuthDetails",
            {"security_scheme.audience": "https://api.example.com"},
        ),
    ],
    ids=["anonymous", "project_connection", "managed_identity"],
)
def test_create_toolbox_constructs_the_correct_discriminated_openapi_auth_type(
    monkeypatch, auth, expected_cls_name, expected_attrs
):
    # Real-SDK guard for all three OpenApiAuthDetails subclasses -- managed_identity in
    # particular was not previously exercised against a live model instance anywhere.
    pytest.importorskip("azure.ai.projects")
    pytest.importorskip("azure.identity")
    from azure.ai.projects import models as m

    captured: dict = {}
    _install_fake_ai_project_client(monkeypatch, returned_version="1", captured=captured)

    manifest = {
        **_valid_manifest(),
        "tools": [
            {
                "type": "openapi",
                "name": "o",
                "openapi": {"name": "x", "spec": {"openapi": "3.0.0"}, "auth": auth},
            }
        ],
    }
    _tb.create_toolbox(manifest, _ENDPOINT)

    _, kwargs = captured["create_version"]
    openapi_tool = next(t for t in kwargs["tools"] if isinstance(t, m.OpenApiToolboxTool))
    built_auth = openapi_tool.openapi.auth
    assert isinstance(built_auth, getattr(m, expected_cls_name))
    for dotted_attr, expected_value in expected_attrs.items():
        obj = built_auth
        for part in dotted_attr.split("."):
            obj = getattr(obj, part)
        assert obj == expected_value


# ----------------- round 9: pinned SDK version in missing-dependency fallback -----------
def _simulate_azure_ai_projects_missing(monkeypatch):
    # The standard, reversible way to force `from azure.ai.projects import ...` to raise
    # ImportError even though the real package IS installed in this venv: cache `None` for the
    # module name (see https://docs.python.org/3/reference/import.html#the-module-cache).
    # monkeypatch.setitem restores the real module object afterwards, so later tests in this
    # file that construct real SDK models are unaffected.
    monkeypatch.setitem(sys.modules, "azure.ai.projects", None)


@pytest.mark.parametrize(
    ("call", "label"),
    [
        (lambda: _tb.create_toolbox({"tools": []}, _ENDPOINT), "toolbox"),
        (lambda: _sk._project_client(_ENDPOINT), "skills"),
    ],
    ids=["toolbox", "skills"],
)
def test_missing_sdk_fallback_message_pins_the_audited_exact_version(monkeypatch, call, label):
    # Round 8 pinned `azure-ai-projects==2.3.0` exactly in pyproject.toml/uv.lock so every
    # install path lands on the one version this whole audit reflection-verified field-by-field
    # -- but both create_toolbox()'s and _project_client()'s ImportError fallback still told an
    # operator without `uv` to run a bare, unpinned `pip install azure-ai-projects
    # azure-identity`, silently reopening exactly the drift the pin was meant to close. Simulate
    # the SDK genuinely being absent and assert the real, user-facing SystemExit message now
    # carries the exact pin (not just the source string, so a future refactor that changes how
    # the message is built still has to keep the guarantee).
    _simulate_azure_ai_projects_missing(monkeypatch)
    with pytest.raises(SystemExit) as exc_info:
        call()
    message = str(exc_info.value)
    assert "pip install azure-ai-projects==2.3.0 azure-identity" in message, label
    assert "pip install azure-ai-projects azure-identity" not in message, label
