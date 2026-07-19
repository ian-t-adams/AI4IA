#!/usr/bin/env python3
"""Provision Foundry skills from foundry/skills/<name>/SKILL.md (preview).

A *skill* is a ``SKILL.md`` file (Agent Skills spec, https://agentskills.io) with a YAML
front-matter block (``name``, ``description``) and a Markdown body of instructions. This
script discovers every ``foundry/skills/*/SKILL.md``, validates it, and creates a versioned
skill in the Foundry project via ``project.beta.skills.create(...)``.

Skills are then *bound* to the toolbox by listing them under ``skills`` in
foundry/toolbox.manifest.json (see scripts/provision-foundry-toolbox.py), so any MCP client
- including this app, through the official-MCP APIM bridge - discovers them alongside tools
at the single toolbox endpoint. No app runtime code is required.

Default is a dry run (offline, no Azure calls). Pass ``--create`` to write to Foundry
(requires the optional ``foundry`` dependency group: azure-ai-projects + azure-identity). Every
``--create`` call passes ``default=True``, so the version it just created is immediately
activated as the one served -- both for a brand-new skill and for a later version of an
existing one (there is no separate promotion step, unlike the toolbox script).
All of this is public preview; do not use in production without validation.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SKILLS_DIR = REPO_ROOT / "foundry" / "skills"

# Agent Skills name rule (see the Foundry skills doc): lowercase/numbers/hyphens, no
# leading/trailing hyphen, max 64.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$")
_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


# --------------------------------------------------------------------------------------
# Pure helpers (no Azure SDK, no PyYAML; unit-tested offline)
# --------------------------------------------------------------------------------------
def parse_skill_md(text: str) -> dict[str, Any]:
    """Parse a SKILL.md into {name, description, instructions}.

    Front matter is a leading ``--- ... ---`` block of simple ``key: value`` lines; the
    remaining Markdown is the instruction body. A minimal parser (no PyYAML dependency) is
    sufficient for the two fields the Skills API needs.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {"name": None, "description": None, "instructions": text.strip(), "_error": "missing YAML front matter"}
    front, body = match.group(1), match.group(2)
    meta: dict[str, str] = {}
    for line in front.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return {
        "name": meta.get("name"),
        "description": meta.get("description"),
        "instructions": body.strip(),
    }


def validate_skill(skill: dict[str, Any]) -> list[str]:
    """Return validation errors for a parsed skill (empty => valid)."""
    errors: list[str] = []
    name = skill.get("name")
    if not name or not _SKILL_NAME_RE.match(name) or len(name) > 64:
        errors.append(f"name '{name}' must match ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ (max 64).")
    if not skill.get("description"):
        errors.append("`description` front-matter field is required.")
    if not skill.get("instructions"):
        errors.append("instruction body (Markdown after the front matter) is required.")
    return errors


def discover_skills(skills_dir: Path) -> list[dict[str, Any]]:
    """Find every <skills_dir>/*/SKILL.md and parse it. Each entry includes its source path."""
    found: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return found
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        parsed = parse_skill_md(skill_md.read_text(encoding="utf-8"))
        parsed["path"] = skill_md
        found.append(parsed)
    return found


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
# Live path (isolated Azure SDK import)
# --------------------------------------------------------------------------------------
def create_skill(project: Any, skill: dict[str, Any]) -> Any:
    """Create one skill version from inline content and activate it as the default version.

    ``default=True`` is required on every call: ``create()`` is the single, idempotent REST
    operation for both "create the skill for the first time" and "add a later version to an
    already-existing skill" (its own docstring: "Creates a new version of a skill. If the skill
    does not exist, it will be created."). Without an explicit ``default=True``, a later version
    is created but never served -- the skills analogue of the toolbox's
    ``create_version`` + ``update(default_version=...)`` two-call promotion, except skills'
    ``create()`` supports promoting the version it just created in the very same call.
    """
    from azure.ai.projects.models import SkillInlineContent

    return project.beta.skills.create(
        name=skill["name"],
        inline_content=SkillInlineContent(
            description=skill["description"],
            instructions=skill["instructions"],
        ),
        default=True,
    )


def _project_client(endpoint: str) -> Any:  # pragma: no cover - live only
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise SystemExit(
            "azure-ai-projects is not installed. Install the optional provisioning group:\n"
            '  uv pip install -e "app/api[foundry]"   # or: pip install azure-ai-projects==2.3.0 azure-identity'
        ) from exc
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential(), allow_preview=True)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR, help="Directory of <name>/SKILL.md skills.")
    parser.add_argument("--project-endpoint", default=None, help="Foundry project endpoint (else AZURE_FOUNDRY_PROJECT_ENDPOINT).")
    parser.add_argument("--create", action="store_true", help="Actually create the skill versions (needs azure-ai-projects). Default is a dry run.")
    args = parser.parse_args(argv)

    skills = discover_skills(args.skills_dir)
    if not skills:
        print(f"No skills found under {args.skills_dir} (expected <name>/SKILL.md). Nothing to do.")
        return 0

    invalid = False
    for skill in skills:
        errors = validate_skill(skill)
        status = "OK" if not errors else "INVALID"
        skill_path = skill["path"]
        try:
            # Cosmetic only: prefer a repo-relative path when --skills-dir is (as normal) under
            # REPO_ROOT. A custom --skills-dir (e.g. a test's tmp_path, or an operator pointing at
            # a scratch directory) is not an error, so fall back to the absolute path instead of
            # crashing (Path.relative_to() raises ValueError when it is not a subpath).
            skill_path = skill_path.relative_to(REPO_ROOT)
        except ValueError:
            pass
        print(f"[{status}] {skill.get('name') or skill['path'].parent.name}  ({skill_path})")
        for e in errors:
            print(f"    - {e}", file=sys.stderr)
            invalid = True
    if invalid:
        print("\nFix the errors above before provisioning.", file=sys.stderr)
        return 1

    if not args.create:
        print(f"\n(dry run) {len(skills)} skill(s) valid. Re-run with --create to create them in Foundry,")
        print("then list them under `skills` in foundry/toolbox.manifest.json to bind to the toolbox.")
        return 0

    endpoint = resolve_project_endpoint(args.project_endpoint)
    project = _project_client(endpoint)
    for skill in skills:
        created = create_skill(project, skill)
        print(f"Created skill '{skill['name']}' version {getattr(created, 'version', '?')} and activated it as the default version.")
    print("\nNow bind them: add each under `skills` in foundry/toolbox.manifest.json and re-run")
    print("scripts/provision-foundry-toolbox.py --create to publish a toolbox version that references them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
