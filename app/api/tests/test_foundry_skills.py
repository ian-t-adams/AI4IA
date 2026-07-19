"""Guard the skills provisioning script (docs/foundry-toolbox.md "Skills" section).

`parse_skill_md`/`validate_skill`/the shipped `citation-discipline` example/
`resolve_project_endpoint` already have coverage in test_foundry_toolbox.py (which loads this
same script as `_sk` for a handful of shared/parametrized checks); this file adds what is
genuinely NOT covered anywhere else:

- `discover_skills()` (directory globbing, sorted order, missing-dir tolerance).
- `main()`'s CLI paths (no skills found, a valid dry run, an invalid skill exits non-zero).
- The round-10 regression: `create_skill()` must always pass `default=True` to
  `project.beta.skills.create(...)`, for BOTH a brand-new skill and a later version of an
  existing one -- `create()` is the same idempotent REST call for both cases (its own docstring:
  "Creates a new version of a skill. If the skill does not exist, it will be created."), so a
  missing `default=True` would silently create a version that is never served.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "provision-foundry-skills.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_sk = _load("provision_foundry_skills_dedicated", _SCRIPT)


def _write_skill(root: Path, dirname: str, name: str, description: str, body: str) -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8")
    return path


# ----------------------------------- discover_skills ------------------------------------
def test_discover_skills_finds_every_skill_md_in_sorted_order(tmp_path):
    _write_skill(tmp_path, "b-skill", "b-skill", "B.", "Do B.")
    _write_skill(tmp_path, "a-skill", "a-skill", "A.", "Do A.")
    # A stray non-SKILL.md file alongside a real skill must not be picked up.
    (tmp_path / "a-skill" / "README.md").write_text("not a skill", encoding="utf-8")

    found = _sk.discover_skills(tmp_path)

    assert [f["name"] for f in found] == ["a-skill", "b-skill"]
    assert all(f["path"].name == "SKILL.md" for f in found)


def test_discover_skills_tolerates_a_missing_directory(tmp_path):
    assert _sk.discover_skills(tmp_path / "does-not-exist") == []


def test_discover_skills_returns_empty_list_for_an_empty_directory(tmp_path):
    assert _sk.discover_skills(tmp_path) == []


# ----------------------------------------- main ------------------------------------------
def test_main_reports_no_skills_found_and_exits_zero(tmp_path, capsys):
    exit_code = _sk.main(["--skills-dir", str(tmp_path)])

    assert exit_code == 0
    assert "No skills found" in capsys.readouterr().out


def test_main_dry_run_reports_valid_skill_and_promotion_hint(tmp_path, capsys):
    _write_skill(tmp_path, "greeting", "greeting", "Say hi.", "Be warm and brief.")

    exit_code = _sk.main(["--skills-dir", str(tmp_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[OK] greeting" in out
    assert "--create" in out


def test_main_reports_invalid_skill_and_exits_nonzero(tmp_path, capsys):
    skill_dir = tmp_path / "bad"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("no front matter here at all", encoding="utf-8")

    exit_code = _sk.main(["--skills-dir", str(tmp_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Fix the errors above" in err


# ------------------------- round 10: create_skill(..., default=True) ---------------------
class _FakeSkillsOps:
    """Records every project.beta.skills.create(...) call, mirroring the real SDK's signature."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, *, name, inline_content, default=None):
        self.calls.append({"name": name, "inline_content": inline_content, "default": default})
        return SimpleNamespace(version=str(len(self.calls)))


class _FakeProject:
    def __init__(self) -> None:
        self.beta = SimpleNamespace(skills=_FakeSkillsOps())


def test_create_skill_always_passes_default_true_on_first_create_and_later_version():
    # Uses the REAL SkillInlineContent model (not a stub) so this proves create_skill() actually
    # constructs the real SDK type, not just a plain dict -- but swaps in a fake `beta.skills` so
    # no network/credential is needed.
    m = pytest.importorskip("azure.ai.projects.models")
    project = _FakeProject()
    skill = {"name": "citation-discipline", "description": "Cite sources.", "instructions": "Do it."}

    first = _sk.create_skill(project, skill)

    assert first.version == "1"
    assert len(project.beta.skills.calls) == 1
    call = project.beta.skills.calls[0]
    assert call["name"] == "citation-discipline"
    assert call["default"] is True  # <- the round-10 regression: must never be omitted/False
    assert isinstance(call["inline_content"], m.SkillInlineContent)
    assert call["inline_content"].description == "Cite sources."
    assert call["inline_content"].instructions == "Do it."

    # Later version of the SAME skill (e.g. edited instructions): create() is the identical
    # idempotent call the SDK uses for "add a new version to an existing skill" -- it must ALSO
    # promote, or the new version would be created but never actually served.
    updated_skill = {**skill, "instructions": "Do it better."}
    second = _sk.create_skill(project, updated_skill)

    assert second.version == "2"
    assert len(project.beta.skills.calls) == 2
    assert project.beta.skills.calls[1]["default"] is True
    assert project.beta.skills.calls[1]["inline_content"].instructions == "Do it better."
