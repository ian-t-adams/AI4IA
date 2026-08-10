"""Guard the routine provisioning script (docs/foundry-toolbox.md P7).

Pins the *pure* validation/projection logic with no Azure SDK/network: manifest validation,
the toolbox-tool references a routine's steps make (the bridge point -- every call flows through
the APIM-fronted toolbox), the step projection, fail-closed endpoint resolution, and that the
shipped example validates against foundry/routines/routine.schema.json. Also guards the round-10
finding that this script must NEVER grow a live `--create` path: azure-ai-projects 2.4.0 has no
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
_TOOLBOX = _REPO_ROOT / "foundry" / "toolbox.manifest.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_r = _load("provision_foundry_routine", _SCRIPT)


def _valid() -> dict:
    return {
        "manifestVersion": "1.0",
        "lifecycle": "design-preview",
        "owner": "repository-owner",
        "sdkContract": {
            "package": "azure-ai-projects",
            "version": "2.4.0",
            "status": "not-executable",
            "surface": "project.beta.routines.create_or_update",
        },
        "name": "research-brief",
        "description": "Two-step brief.",
        "model": "gpt-5",
        "toolbox": "ai4ia-toolbox",
        "steps": [
            {"name": "gather", "instructions": "collect", "tools": ["web-search", "code-interpreter"]},
            {"name": "synthesize", "instructions": "write", "tools": ["web-search"]},
        ],
    }


def test_validate_accepts_a_well_formed_routine():
    assert _r.validate_manifest(_valid()) == []
    assert _r.validate_manifest_schema(_valid()) == []


def test_schema_validation_rejects_missing_lifecycle_metadata():
    manifest = _valid()
    del manifest["sdkContract"]
    errors = _r.validate_manifest_schema(manifest)
    assert any("sdkContract" in error for error in errors)


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
    assert _r.referenced_tools(_valid()) == ["web-search", "code-interpreter"]


def test_validate_rejects_unknown_tool_names_against_canonical_toolbox():
    manifest = _valid()
    manifest["steps"][0]["tools"] = ["web_search", "does-not-exist"]
    errors = _r.validate_manifest(manifest)
    assert any("unknown toolbox tool 'web_search'" in error for error in errors)
    assert any("unknown toolbox tool 'does-not-exist'" in error for error in errors)
    assert any(
        "web-search" in error and "code-interpreter" in error for error in errors
    )


def test_validate_rejects_falsey_and_truthy_non_object_toolbox_manifests():
    for malformed in ([], [1]):
        errors = _r.validate_manifest(_valid(), malformed)
        assert any("toolbox manifest must be a JSON object" in error for error in errors)


def test_plan_steps_normalizes_shape():
    planned = _r.plan_steps(_valid())
    assert [s["name"] for s in planned] == ["gather", "synthesize"]
    assert planned[0]["tools"] == ["web-search", "code-interpreter"]
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
    toolbox = json.loads(_TOOLBOX.read_text(encoding="utf-8"))
    assert set(_r.referenced_tools(manifest)) <= _r.toolbox_tool_names(toolbox)
    assert manifest["lifecycle"] == "design-preview"
    assert manifest["sdkContract"]["status"] == "not-executable"


def test_example_validates_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    manifest = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)


def test_create_routine_and_dash_dash_create_do_not_exist(monkeypatch):
    # Round-10 regression: this script must never regain a live --create path. There is no
    # non-inventive translation from this manifest's steps to azure-ai-projects 2.4.0's actual
    # routines surface (project.beta.routines.create_or_update, an event-trigger-invokes-an-
    # existing-agent model) -- so both the function AND the CLI flag must stay gone.
    assert not hasattr(_r, "create_routine")
    monkeypatch.delenv("AZURE_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        _r.main(["--manifest", str(_EXAMPLE), "--create"])
    # argparse rejects an unrecognized argument with exit code 2, not a live call.
    assert exc_info.value.code == 2


def test_check_runs_semantic_validation(capsys):
    assert _r.main(["--manifest", str(_EXAMPLE), "--check"]) == 0
    assert "canonical toolbox" in capsys.readouterr().out


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


def test_main_rejects_schema_invalid_design(tmp_path, capsys):
    invalid = _valid()
    del invalid["owner"]
    path = tmp_path / "schema-invalid.routine.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    assert _r.main(["--manifest", str(path), "--check"]) == 1
    assert "owner" in capsys.readouterr().err


def test_main_rejects_non_object_and_non_object_steps_without_crashing(
    tmp_path, capsys
):
    for name, value in (
        ("root", []),
        ("step", {**_valid(), "steps": [1]}),
    ):
        path = tmp_path / f"{name}.routine.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        assert _r.main(["--manifest", str(path), "--check"]) == 1
        assert "is not" in capsys.readouterr().err


def test_main_rejects_schema_invalid_toolbox_without_crashing(tmp_path, capsys):
    toolbox = tmp_path / "bad.toolbox.json"
    toolbox.write_text("[]", encoding="utf-8")
    assert (
        _r.main(
            [
                "--manifest",
                str(_EXAMPLE),
                "--toolbox-manifest",
                str(toolbox),
                "--check",
            ]
        )
        == 1
    )
    assert "toolbox" in capsys.readouterr().err
