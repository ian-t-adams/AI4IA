"""Tests for the redacted activity-step serialization used by the UI trace."""
from __future__ import annotations

from ai4ia_api.agents.activity import persisted_trace, serialize_step
from ai4ia_api.agents.runtime import AgentStep


def test_tool_start_uses_present_tense_with_a_safe_detail():
    view = serialize_step(
        AgentStep(kind="tool_start", tool="web_search", arguments={"query": "microsoft build 2025"})
    )
    assert view is not None
    assert view.kind == "tool_start"
    assert view.tool == "web_search"
    assert view.label == "Searching the web"
    assert view.detail == "microsoft build 2025"


def test_tool_result_uses_past_tense_and_never_leaks_raw_result():
    view = serialize_step(
        AgentStep(
            kind="tool_result",
            tool="run_code",
            arguments={"expression": "1+1"},
            result={"secret": "TOKEN-should-not-appear"},
        )
    )
    assert view is not None
    assert view.label == "Ran code"
    assert view.detail == "1+1"
    # The raw result is never surfaced in the redacted view.
    assert "TOKEN" not in (view.detail or "")


def test_final_marker_is_dropped():
    assert serialize_step(AgentStep(kind="final")) is None
    assert serialize_step(AgentStep(kind="final", detail="max_iters")) is None


def test_denied_and_error_have_distinct_labels():
    denied = serialize_step(AgentStep(kind="tool_denied", tool="web_search", detail="missing_scope"))
    assert denied is not None and denied.label.startswith("Blocked")
    errored = serialize_step(AgentStep(kind="tool_error", tool="run_code", detail="execution_error"))
    assert errored is not None and "didn't complete" in errored.label


def test_detail_is_single_line_and_length_capped():
    view = serialize_step(
        AgentStep(kind="tool_start", tool="web_search", arguments={"query": "a\nb " * 200})
    )
    assert view is not None and view.detail is not None
    assert len(view.detail) <= 80
    assert "\n" not in view.detail


def test_unknown_tool_falls_back_to_a_humanized_label():
    view = serialize_step(AgentStep(kind="tool_result", tool="mystery_tool"))
    assert view is not None
    assert view.label == "Ran mystery tool"
    assert view.tool == "mystery_tool"


def test_persisted_trace_keeps_actions_and_drops_final():
    steps = [
        AgentStep(kind="tool_result", tool="web_search", arguments={"query": "x"}),
        AgentStep(kind="tool_denied", tool="run_code", detail="denied"),
        AgentStep(kind="final"),
    ]
    trace = persisted_trace(steps)
    assert [t.kind for t in trace] == ["tool_result", "tool_denied"]
