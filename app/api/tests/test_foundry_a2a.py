"""Guard the A2A provisioning script (docs/foundry-toolbox.md P7).

Pins the *pure* logic with no Azure SDK/network: manifest validation, the raw A2A endpoint and
the APIM **consumer** URL shapes (the front door), the emitted enable + APIM-front commands, and
that the agents.json stub is a valid AgentSpec so the links (agent-as-tool) seam can consume it.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "provision-foundry-a2a.py"
_SCHEMA = _REPO_ROOT / "foundry" / "a2a" / "a2a.schema.json"
_EXAMPLE = _REPO_ROOT / "foundry" / "a2a" / "example.a2a.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_a = _load("provision_foundry_a2a", _SCRIPT)


def _valid() -> dict:
    return {
        "agentName": "ai4ia-research-agent",
        "displayName": "Research Agent (A2A)",
        "description": "Remote agent.",
        "linkAs": "remote-research",
    }


def test_validate_accepts_a_well_formed_manifest():
    assert _a.validate_manifest(_valid()) == []


def test_validate_rejects_missing_fields_and_bad_linkas():
    errs = _a.validate_manifest({"linkAs": "Bad_Slug"})
    assert any("agentName" in e for e in errs)
    assert any("displayName" in e for e in errs)
    assert any("description" in e for e in errs)
    assert any("linkAs" in e for e in errs)


def test_raw_endpoint_and_consumer_url_shapes():
    assert _a.a2a_endpoint("https://p", "agentx") == "https://p/agents/agentx/a2a"
    assert _a.a2a_endpoint("https://p/", "agentx") == "https://p/agents/agentx/a2a"
    assert _a.consumer_url("https://gw", "agentx") == "https://gw/a2a/agentx"
    assert _a.consumer_url("https://gw/", "agentx") == "https://gw/a2a/agentx"


def test_build_agent_link_is_a_valid_agentspec():
    # The stub must construct as a real AgentSpec so the links seam can consume it.
    from ai4ia_api.agents.agent_catalog import AgentSpec

    stub = _a.build_agent_link(_valid())
    spec = AgentSpec(**stub)
    assert spec.name == "remote-research"
    assert spec.displayName == "Research Agent (A2A)"
    assert spec.enabled is True
    assert spec.tools == [] and spec.links == []


def test_emit_az_covers_enable_apim_front_and_mi_policy():
    cmds = _a.to_az_commands(_valid(), "apim-a2a.example.com", "apim-ai4ia", "rg-ai4ia", "https://p/agents/ai4ia-research-agent/a2a")
    blob = "\n".join(cmds)
    assert "a2aEnabled=true" in blob
    assert "az apim api create" in blob
    assert "--api-id \"a2a-ai4ia-research-agent\"" in blob
    assert "authentication-managed-identity" in blob
    assert "https://ai.azure.com" in blob


def test_resolve_fails_closed(monkeypatch):
    monkeypatch.delenv("AZURE_A2A_GATEWAY_URL", raising=False)
    with pytest.raises(SystemExit):
        _a.resolve(None, "AZURE_A2A_GATEWAY_URL", "gateway")
    assert _a.resolve("x", "AZURE_A2A_GATEWAY_URL", "gateway") == "x"


def test_example_validates_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    manifest = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)
    assert _a.validate_manifest(manifest) == []
