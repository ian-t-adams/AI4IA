"""Guard the design-only A2A contract (docs/foundry-toolbox.md).

Pins manifest validation and the complete blocker inventory. The script must not
emit Azure commands or an AgentSpec until a callable integration contract exists.
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
        "manifestVersion": "1.0",
        "lifecycle": "design-preview",
        "owner": "repository-owner",
        "sdkContract": {
            "package": "azure-ai-projects",
            "version": "2.4.0",
            "status": "not-executable",
            "surface": "no-validated-inbound-a2a-surface",
        },
        "agentName": "ai4ia-research-agent",
        "displayName": "Research Agent (A2A)",
        "description": "Remote agent.",
        "linkAs": "remote-research",
        "blockingRequirements": sorted(_a.REQUIRED_BLOCKERS),
    }


def test_validate_accepts_a_well_formed_manifest():
    assert _a.validate_manifest(_valid()) == []
    assert _a.validate_manifest_schema(_valid()) == []


def test_schema_validation_rejects_missing_lifecycle_metadata():
    manifest = _valid()
    del manifest["manifestVersion"]
    errors = _a.validate_manifest_schema(manifest)
    assert any("manifestVersion" in error for error in errors)


def test_validate_rejects_missing_fields_and_bad_linkas():
    errs = _a.validate_manifest({"linkAs": "Bad_Slug"})
    assert any("agentName" in e for e in errs)
    assert any("displayName" in e for e in errs)
    assert any("description" in e for e in errs)
    assert any("linkAs" in e for e in errs)


def test_validate_rejects_an_incomplete_blocker_inventory():
    manifest = _valid()
    manifest["blockingRequirements"].remove("runtime-client-wiring")
    errors = _a.validate_manifest(manifest)
    assert any("runtime-client-wiring" in error for error in errors)


def test_validate_handles_non_string_blockers_without_crashing():
    manifest = _valid()
    manifest["blockingRequirements"] = [{}]
    assert _a.validate_manifest(manifest)
    assert _a.validate_manifest_schema(manifest)


def test_main_rejects_non_object_without_crashing(tmp_path, capsys):
    path = tmp_path / "root.a2a.json"
    path.write_text("[]", encoding="utf-8")
    assert _a.main(["--manifest", str(path), "--check"]) == 1
    assert "is not" in capsys.readouterr().err


def test_example_validates_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    manifest = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)
    assert _a.validate_manifest(manifest) == []
    assert set(manifest["blockingRequirements"]) == _a.REQUIRED_BLOCKERS
    assert manifest["lifecycle"] == "design-preview"
    assert manifest["sdkContract"]["status"] == "not-executable"


def test_cli_is_validation_only_and_rejects_emit_az(capsys):
    assert _a.main(["--manifest", str(_EXAMPLE)]) == 0
    output = capsys.readouterr().out
    assert "not callable" in output
    assert "No Azure commands" in output
    assert not hasattr(_a, "to_az_commands")
    assert not hasattr(_a, "build_agent_link")
    with pytest.raises(SystemExit) as exc_info:
        _a.main(["--manifest", str(_EXAMPLE), "--emit-az"])
    assert exc_info.value.code == 2
