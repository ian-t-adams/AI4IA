"""Guard the infra -> runtime MCP catalog generator + schema for the Foundry
toolbox seam.

The Foundry toolbox is consumed as a normal "official" MCP server whose catalog
entry carries two optional maps -- ``upstreamHeaders`` and ``upstreamQueryParams``
-- that APIM injects outbound (e.g. ``Foundry-Features: Toolboxes=V1Preview`` and
``api-version=v1``). These are *infra-only* wiring: they must never leak into the
packaged runtime catalog, and the generator must still enforce the managed-identity
contract. This test pins both, plus the schema shape, without any network or new
runtime dependency.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GEN = _REPO_ROOT / "scripts" / "gen-mcp-catalog.py"
_SCHEMA = _REPO_ROOT / "infra" / "mcp-servers.schema.json"

# A representative Foundry-toolbox catalog entry (the bridge that fronts the whole
# toolbox -- web/AI search, code interpreter, tool search, skills -- through APIM).
_TOOLBOX_ENTRY = {
    "name": "foundry-toolbox",
    "displayName": "Foundry Toolbox",
    "description": "Curated Foundry Agent Service toolbox via APIM.",
    "upstreamUrl": "https://acct.services.ai.azure.com/api/projects/proj/toolboxes/tb/mcp",
    "upstreamAuthMode": "managed_identity",
    "upstreamMiResource": "https://ai.azure.com",
    "upstreamHeaders": {
        "Foundry-Features": "Toolboxes=V1Preview,Skills=V1Preview"
    },
    "upstreamQueryParams": {"api-version": "v1"},
}

_INFRA_ONLY_FIELDS = (
    "upstreamUrl",
    "upstreamAuthMode",
    "upstreamMiResource",
    "upstreamHeaders",
    "upstreamQueryParams",
    "foundryToolbox",
)

# A PORTABLE Foundry-toolbox entry: no hardcoded upstreamUrl (main.bicep computes it from the
# deployed project endpoint), flagged with foundryToolbox: true.
_PORTABLE_TOOLBOX_ENTRY = {
    "name": "ai4ia-toolbox",
    "displayName": "Foundry toolbox: ai4ia-toolbox",
    "description": "AI4IA shared Foundry toolbox via APIM.",
    "foundryToolbox": True,
    "upstreamAuthMode": "managed_identity",
    "upstreamMiResource": "https://ai.azure.com",
    "upstreamHeaders": {"Foundry-Features": "Toolboxes=V1Preview"},
    "upstreamQueryParams": {"api-version": "v1"},
}


def _build_catalog():
    """Import ``build_catalog`` from the standalone generator script by path.

    The generator lives outside the installed package (repo ``scripts/``); it takes a
    raw dict, so no file IO or repo layout coupling is exercised here.
    """
    spec = importlib.util.spec_from_file_location("gen_mcp_catalog", _GEN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_catalog


def test_generator_projects_toolbox_to_runtime_shape_and_drops_infra_fields():
    out = _build_catalog()({"servers": [_TOOLBOX_ENTRY]})
    assert out["servers"] == [
        {
            "id": "foundry-toolbox",
            "displayName": "Foundry Toolbox",
            "description": "Curated Foundry Agent Service toolbox via APIM.",
            "path": "foundry-toolbox/mcp",
            "resourcesEnabled": False,
        }
    ]
    [item] = out["servers"]
    for field in _INFRA_ONLY_FIELDS:
        assert field not in item, f"infra-only field {field!r} leaked into runtime catalog"


def test_generator_still_enforces_managed_identity_resource():
    entry = {k: v for k, v in _TOOLBOX_ENTRY.items() if k != "upstreamMiResource"}
    with pytest.raises(SystemExit):
        _build_catalog()({"servers": [entry]})


def test_generator_accepts_portable_foundry_toolbox_without_upstream_url():
    # A foundryToolbox entry legitimately omits upstreamUrl (bicep computes it); the generator
    # must accept it and project only the runtime shape (dropping foundryToolbox + all infra fields).
    out = _build_catalog()({"servers": [_PORTABLE_TOOLBOX_ENTRY]})
    assert out["servers"] == [
        {
            "id": "ai4ia-toolbox",
            "displayName": "Foundry toolbox: ai4ia-toolbox",
            "description": "AI4IA shared Foundry toolbox via APIM.",
            "path": "ai4ia-toolbox/mcp",
            "resourcesEnabled": True,
        }
    ]
    [item] = out["servers"]
    for field in _INFRA_ONLY_FIELDS:
        assert field not in item


def test_generator_still_requires_url_for_non_toolbox_entries():
    # An external (non-foundryToolbox) entry with no upstreamUrl must still fail closed.
    entry = {k: v for k, v in _PORTABLE_TOOLBOX_ENTRY.items() if k != "foundryToolbox"}
    with pytest.raises(SystemExit):
        _build_catalog()({"servers": [entry]})


def test_schema_declares_optional_string_maps():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    props = schema["properties"]["servers"]["items"]["properties"]
    for field in ("upstreamHeaders", "upstreamQueryParams"):
        assert props[field]["type"] == "object"
        assert props[field]["additionalProperties"] == {"type": "string"}
    # The new fields are optional -- the default (empty) catalog must stay valid.
    assert "upstreamHeaders" not in schema["properties"]["servers"]["items"]["required"]
    assert "upstreamQueryParams" not in schema["properties"]["servers"]["items"]["required"]


def test_schema_accepts_toolbox_entry_and_rejects_unknown_fields():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate({"servers": [_TOOLBOX_ENTRY]}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"servers": [{**_TOOLBOX_ENTRY, "bogus": 1}]}, schema)
