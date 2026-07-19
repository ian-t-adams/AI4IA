"""Guard the routine provisioning script (docs/foundry-toolbox.md P7).

Pins the *pure* validation/projection logic with no Azure SDK/network: manifest validation,
the toolbox-tool references a routine's steps make (the bridge point -- every call flows through
the APIM-fronted toolbox), the step projection, fail-closed endpoint resolution, and that the
shipped example validates against foundry/routines/routine.schema.json. Also guards the round-10
finding that this script must NEVER grow a live `--create` path: azure-ai-projects 2.3.0 has no
`project.routines` compatible with this manifest's steps-based shape (see the module's "Why there
is no --create" docstring section), so faking a translation would be worse than not having one.
"""
from __future__ import annotations

import importlib.util
import json
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


def test_create_routine_and_dash_dash_create_do_not_exist(monkeypatch):
    # Round-10 regression: this script must never regain a live --create path. There is no
    # non-inventive translation from this manifest's steps to azure-ai-projects 2.3.0's actual
    # routines surface (project.beta.routines.create_or_update, an event-trigger-invokes-an-
    # existing-agent model) -- so both the function AND the CLI flag must stay gone.
    assert not hasattr(_r, "create_routine")
    monkeypatch.delenv("AZURE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        _r.main(["--manifest", str(_EXAMPLE), "--create"])
    # argparse rejects an unrecognized argument with exit code 2, not a live call.
    assert exc_info.value.code == 2


def test_main_dry_run_tolerates_a_missing_project_endpoint(monkeypatch, capsys, tmp_path):
    # The script never makes a live call, so a missing/unset project endpoint must not be fatal
    # -- it is shown for context only (round-10: main() used to hard-require it via
    # resolve_project_endpoint() before the --create removal made that check pointless).
    monkeypatch.delenv("AZURE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    manifest_path = tmp_path / "example.routine.json"
    manifest_path.write_text(_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    exit_code = _r.main(["--manifest", str(manifest_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "not configured" in out
    assert "no --create" in out


def test_main_reports_clean_validation_errors_without_crashing(tmp_path):
    bad = tmp_path / "bad.routine.json"
    bad.write_text(json.dumps({"name": "Bad_Name", "steps": []}), encoding="utf-8")

    exit_code = _r.main(["--manifest", str(bad)])

    assert exit_code == 1
