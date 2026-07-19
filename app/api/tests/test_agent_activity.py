"""Tests for the redacted activity-step serialization used by the UI trace."""
from __future__ import annotations

from ai4ia_api.agents.activity import persisted_trace, serialize_step
from ai4ia_api.agents.runtime import AgentStep


def test_tool_start_never_leaks_argument_content():
    view = serialize_step(
        AgentStep(kind="tool_start", tool="web_search", arguments={"query": "microsoft build 2025"})
    )
    assert view is not None
    assert view.kind == "tool_start"
    assert view.tool == "web_search"
    assert view.label == "Searching the web"
    # tool_start never sets AgentStep.detail, so no argument-derived text leaks.
    assert view.detail is None


def test_tool_result_never_leaks_arguments_or_raw_result():
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
    # Neither the argument value nor the raw result are ever surfaced.
    assert view.detail is None
    dumped = view.model_dump_json()
    assert "1+1" not in dumped
    assert "TOKEN" not in dumped


def test_final_marker_is_dropped():
    assert serialize_step(AgentStep(kind="final")) is None
    assert serialize_step(AgentStep(kind="final", detail="max_iters")) is None


def test_denied_and_error_have_distinct_labels():
    denied = serialize_step(AgentStep(kind="tool_denied", tool="web_search", detail="missing_scope"))
    assert denied is not None and denied.label.startswith("Blocked")
    # The bounded reason category set directly by the runtime IS preserved.
    assert denied.detail == "missing_scope"
    errored = serialize_step(AgentStep(kind="tool_error", tool="run_code", detail="execution_error"))
    assert errored is not None and "didn't complete" in errored.label
    assert errored.detail == "execution_error"


def test_detail_is_single_line_and_length_capped():
    # A step's fixed detail string is bounded even if some future call site
    # hands it something unexpectedly long or multi-line (defense in depth) --
    # arguments are never a source of detail regardless of length.
    view = serialize_step(AgentStep(kind="tool_error", tool="web_search", detail="a\nb " * 200))
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


def test_generate_video_prompt_never_appears_in_activity_or_persisted_trace():
    """Regression: a HIGH privacy finding -- generate_video's free-text prompt
    (which may carry sensitive or model-expanded content) was persisted via the
    activity trace's argument-derived ``detail``. It must never appear, live or
    persisted."""
    prompt = "a confidential unreleased product mockup, photorealistic"
    step = AgentStep(kind="tool_result", tool="generate_video", arguments={"prompt": prompt})
    view = serialize_step(step)
    assert view is not None
    assert view.label == "Generated a video"
    assert view.detail is None
    assert prompt not in view.model_dump_json()

    trace = persisted_trace([step])
    assert len(trace) == 1
    assert prompt not in trace[0].model_dump_json()


def test_web_search_query_and_browse_url_never_appear_in_activity():
    """Regression: query/url were previously treated as a 'safe' detail source,
    but any free-text argument value must not be persisted or streamed."""
    query_step = serialize_step(
        AgentStep(kind="tool_start", tool="web_search", arguments={"query": "my medical diagnosis"})
    )
    assert query_step is not None
    assert query_step.detail is None
    assert "medical" not in query_step.model_dump_json()

    url_step = serialize_step(
        AgentStep(
            kind="tool_result",
            tool="browse_url",
            arguments={"url": "https://example.com/patient/12345?token=abc"},
        )
    )
    assert url_step is not None
    assert url_step.detail is None
    assert "example.com" not in url_step.model_dump_json()
    assert "token=abc" not in url_step.model_dump_json()


def test_credentials_and_arbitrary_tool_args_never_appear_in_activity():
    """Regression: arbitrary argument/result content (credentials, file paths,
    delegate targets) must never surface, regardless of key name."""
    step = AgentStep(
        kind="delegate",
        tool="delegate_to_agent",
        arguments={
            "agent": "research-agent",
            "path": "/etc/passwd",
            "filename": "quarterly-earnings.docx",
            "language": "fr",
            "authorization": "Bearer super-secret-token",
        },
        result={"api_key": "SECRET-VALUE-123"},
    )
    view = serialize_step(step)
    assert view is not None
    dumped = view.model_dump_json()
    for leaked in (
        "research-agent",
        "/etc/passwd",
        "quarterly-earnings.docx",
        "super-secret-token",
        "SECRET-VALUE-123",
    ):
        assert leaked not in dumped
