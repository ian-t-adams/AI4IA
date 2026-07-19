"""Guard the routine provisioning script (docs/foundry-toolbox.md P7).

Pins the *pure* validation/projection logic with no Azure SDK/network: manifest validation,
the toolbox-tool references a routine's steps make (the bridge point -- every call flows through
the APIM-fronted toolbox), the step projection, fail-closed endpoint resolution, and that the
shipped example validates against foundry/routines/routine.schema.json.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "provision-foundry-routine.py"
_SCHEMA = _REPO_ROOT / "foundry" / "routines" / "routine.schema.json"
_EXAMPLE = _REPO_ROOT / "foundry" / "routines" / "example.routine.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_r = _load("provision_foundry_routine", _SCRIPT)


def _valid() -> dict:
    return {
        "name": "research-brief",
        "description": "Two-step brief.",
        "model": "gpt-5",
        "toolbox": "ai4ia-toolbox",
        "steps": [
            {"name": "gather", "instructions": "collect", "tools": ["web_search", "ai4ia-rag"]},
            {"name": "synthesize", "instructions": "write", "tools": ["web_search"]},
        ],
    }


def test_validate_accepts_a_well_formed_routine():
    assert _r.validate_manifest(_valid()) == []


def test_validate_rejects_empty_steps():
    m = _valid()
    m["steps"] = []
    errs = _r.validate_manifest(m)
    assert any("no steps" in e for e in errs)


def test_validate_rejects_bad_name_missing_model_and_dupe_steps():
    m = _valid()
    m["name"] = "Bad_Name"
    m["model"] = ""
    m["steps"] = [
        {"name": "s", "instructions": "a"},
        {"name": "s", "instructions": "b"},
    ]
    errs = _r.validate_manifest(m)
    assert any("`name`" in e for e in errs)
    assert any("`model`" in e for e in errs)
    assert any("duplicated" in e for e in errs)


def test_validate_flags_step_missing_instructions():
    m = _valid()
    m["steps"] = [{"name": "only"}]
    errs = _r.validate_manifest(m)
    assert any("instructions" in e for e in errs)


def test_referenced_tools_dedupes_and_preserves_order():
    assert _r.referenced_tools(_valid()) == ["web_search", "ai4ia-rag"]


def test_plan_steps_normalizes_shape():
    planned = _r.plan_steps(_valid())
    assert [s["name"] for s in planned] == ["gather", "synthesize"]
    assert planned[0]["tools"] == ["web_search", "ai4ia-rag"]
    # a step without tools gets an explicit empty list
    assert _r.plan_steps({"steps": [{"name": "x", "instructions": "y"}]})[0]["tools"] == []


def test_resolve_project_endpoint_fails_closed(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    with pytest.raises(SystemExit):
        _r.resolve_project_endpoint(None)
    assert _r.resolve_project_endpoint("https://p") == "https://p"


def test_shipped_example_is_valid():
    manifest = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    assert _r.validate_manifest(manifest) == []


def test_example_validates_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    manifest = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)


def test_create_routine_missing_sdk_fallback_message_pins_the_audited_exact_version(monkeypatch):
    # Mirrors test_foundry_toolbox.py's round-9 regression: create_routine()'s ImportError
    # fallback used to suggest an unpinned `pip install azure-ai-projects azure-identity`,
    # bypassing the audited, exact `azure-ai-projects==2.3.0` pin in pyproject.toml/uv.lock.
    # Force the SDK to appear absent (the standard sys.modules=None cache trick; monkeypatch
    # restores the real module afterwards) and assert the real SystemExit message is pinned.
    monkeypatch.setitem(sys.modules, "azure.ai.projects", None)
    with pytest.raises(SystemExit) as exc_info:
        _r.create_routine(_valid(), "https://p")
    message = str(exc_info.value)
    assert "pip install azure-ai-projects==2.3.0 azure-identity" in message
    assert "pip install azure-ai-projects azure-identity" not in message
