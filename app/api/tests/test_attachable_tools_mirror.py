"""The web app's attachable-tool list must not drift from the API's allowlist.

``app/web/src/lib/studio.ts`` hardcodes ``ATTACHABLE_TOOLS`` so the Agent Builder
can render one checkbox per tool. That mirror can drift in two directions, and
only one of them is safe:

* A tool listed in the web but unknown to the API is rejected with a 422 on save.
  Loud, immediate, self-correcting.
* A tool the API allows but the web omits has **no checkbox**. No user can attach
  it, so no agent and no workflow step can ever have it. Nothing errors — the
  model simply reports that it cannot do the thing, and the turn is recorded as a
  success.

That second direction is not hypothetical: ``remember_memory`` shipped in the API
allowlist while this list still had six entries, so agents asked to save a memory
correctly answered that they could not, and there was no way to fix it from the
product.

This test is in pytest rather than vitest because the API is the source of truth
and only Python can import it. ``app-ci`` is path-filtered on ``app/**``, which
covers both halves, so editing either side runs this.

``toolHelp.test.ts`` already checks that every ``ATTACHABLE_TOOLS`` entry has help
copy and vice versa, and it passed throughout — because both sides of that
comparison live in the web. It can only prove the web is self-consistent, which a
web missing a tool entirely is. Closing the loop needs a check that can see the
API, which is this one.

The companion maps are checked too. A tool with a checkbox but no label renders
as a raw snake_case id, and one with no help copy renders with no explanation —
both are shipped-but-unexplained rather than shipped-but-unreachable, so they are
lesser failures, but they are failures.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai4ia_api.agents.tool_exec import (
    SELECTABLE_SYNTHETIC_TOOL_NAMES,
    USER_ATTACHABLE_TOOL_NAMES,
)

_WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"
_STUDIO_TS = _WEB_SRC / "lib" / "studio.ts"
_TOOL_HELP_TS = _WEB_SRC / "lib" / "toolHelp.ts"
_WORKFLOW_CAPS_TS = _WEB_SRC / "components" / "workflowCapabilities.ts"

# The union the API can ever offer. ``attachable_tool_names()`` filters the
# registry half at runtime (a tool must be executable, enabled, allowlisted,
# safe, approval-free and scope-free), so it is a subset of this. The UI must
# cover the whole union: whether a given registry tool passes those predicates
# depends on deployment state, and a checkbox that is occasionally unbacked is
# far better than a capability with no checkbox at all.
_API_ATTACHABLE = USER_ATTACHABLE_TOOL_NAMES | SELECTABLE_SYNTHETIC_TOOL_NAMES


def _extract_block(source: str, opener: str, closer: str) -> str:
    start = source.index(opener)
    end = source.index(closer, start)
    return source[start:end]


def _web_attachable_tools() -> set[str]:
    block = _extract_block(
        _STUDIO_TS.read_text(encoding="utf-8"),
        "export const ATTACHABLE_TOOLS = [",
        "] as const;",
    )
    return set(re.findall(r'"([a-z_]+)"', block))


def _keys_of_record(path: Path, opener: str) -> set[str]:
    block = _extract_block(path.read_text(encoding="utf-8"), opener, "\n};")
    # Record keys are bare identifiers at the start of a line inside the literal.
    return set(re.findall(r"^\s{2}([a-z_]+):", block, flags=re.MULTILINE))


def _members_of_set(path: Path, opener: str) -> set[str]:
    """Quoted members of a `new Set([...])` literal — a different shape to
    `_keys_of_record`, which matches `key:` pairs and would silently return an
    empty set here (and so pass vacuously)."""
    block = _extract_block(path.read_text(encoding="utf-8"), opener, "]);")
    return set(re.findall(r'"([a-z_]+)"', block))


def test_the_api_allowlist_is_not_empty() -> None:
    """Non-vacuity floor.

    Every other assertion here compares against ``_API_ATTACHABLE``. If a
    refactor renamed or emptied those frozensets, the comparisons would all
    succeed against nothing and this file would pass while checking no drift at
    all — the exact false all-clear it exists to prevent.
    """
    assert len(_API_ATTACHABLE) >= 6
    assert "remember_memory" in _API_ATTACHABLE
    # A moved web tree would otherwise surface as a FileNotFoundError raised deep
    # inside a helper, which reads like a broken test rather than a real finding.
    for path in (_STUDIO_TS, _TOOL_HELP_TS, _WORKFLOW_CAPS_TS):
        assert path.is_file(), f"{path} not found — update the paths in this test."


def test_no_api_tool_is_missing_a_checkbox() -> None:
    """The unsafe drift direction: allowed by the API, unreachable in the UI."""
    missing = _API_ATTACHABLE - _web_attachable_tools()
    assert not missing, (
        f"{sorted(missing)} can be attached per the API but have no checkbox in "
        "studio.ts ATTACHABLE_TOOLS, so no user can ever enable them. Add them "
        "there, plus a label in AgentBuilder.tsx and help copy in toolHelp.ts."
    )


def test_no_web_tool_is_unknown_to_the_api() -> None:
    """The safe direction, checked anyway so it fails here instead of at 422."""
    unknown = _web_attachable_tools() - _API_ATTACHABLE
    assert not unknown, (
        f"{sorted(unknown)} are offered as checkboxes but the API would reject "
        "them with a 422 on save."
    )


@pytest.mark.parametrize(
    ("path", "opener", "what"),
    [
        (_TOOL_HELP_TS, "export const TOOL_LABELS: Record<string, string> = {", "a label"),
        (
            _TOOL_HELP_TS,
            "export const BUILT_IN_TOOL_HELP: Record<string, ToolHelpCopy> = {",
            "help copy",
        ),
    ],
)
def test_every_attachable_tool_has_supporting_copy(
    path: Path, opener: str, what: str
) -> None:
    missing = _API_ATTACHABLE - _keys_of_record(path, opener)
    assert not missing, f"{sorted(missing)} have no {what} in {path.name}."


def test_the_step_tool_picker_excludes_only_what_cannot_work_in_a_step() -> None:
    """``STEP_ATTACHABLE_TOOLS`` is derived, and this pins what it derives to.

    The workflow step picker must never offer a tool that a step structurally
    cannot use — a checkbox that saves, validates, and then does nothing is the
    inert-control failure this whole feature exists to remove. It must equally
    not hide one that *does* work, which is how the memory tools became
    unreachable in the first place.
    """
    excluded = _members_of_set(
        _WORKFLOW_CAPS_TS, "export const NOT_IN_WORKFLOW_STEPS = new Set(["
    )
    assert excluded, "NOT_IN_WORKFLOW_STEPS parsed as empty — the extractor is stale."
    offered = _API_ATTACHABLE - excluded

    assert offered == {
        "calculator",
        "get_current_time",
        "recall_memory",
        "remember_memory",
    }, (
        "The set of tools a workflow step can be given changed. If a tool became "
        "usable in a step, remove it from NOT_IN_WORKFLOW_STEPS; if a new tool is "
        "chat-only, add it there. Then update this expectation deliberately."
    )
