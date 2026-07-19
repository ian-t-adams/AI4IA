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


def test_hostile_arguments_of_any_shape_or_size_never_appear_in_activity():
    """Regression (hostile-input coverage): ``serialize_step`` must be safe by
    *construction* -- it never reads ``step.arguments``/``step.result`` at all,
    so no adversarial argument shape can leak regardless of key name, nesting,
    size, or encoding. Exercise that guarantee with a pathological payload
    (deeply nested structures, huge strings, control characters, unicode, and
    injection-shaped content) rather than only a few named fields.

    ``short_marker`` is deliberately placed under ``query`` -- the highest
    -priority key in the pre-fix ``_DETAIL_KEYS`` allowlist -- and kept under
    the old 80-char truncation length. Confirmed against the pre-round-4
    implementation (``_detail()`` reading ``step.arguments["query"]`` verbatim):
    this exact payload leaked ``short_marker`` into the serialized view. The
    other (huge/nested/unicode) fields prove the guarantee holds regardless of
    shape even though they alone wouldn't have distinguished old vs. new code
    (they either exceed the old truncation window or live under keys/``result``
    the old code never read either)."""
    short_marker = "sk-hostile-marker-should-never-leak-1a2b3c"
    pathological = "A" * 50_000 + "\x00\x1b[31m<script>alert(1)</script>' OR '1'='1"
    step = AgentStep(
        kind="tool_result",
        tool="fetch_document",
        arguments={
            "query": short_marker,
            "prompt": pathological,
            "nested": {"a": {"b": {"c": [pathological, {"d": pathological}]}}},
            "unicode": "\u79d8\u5bc6\u306e\u30d7\u30ed\u30f3\u30d7\u30c8\U0001F512",
            "list": [pathological] * 10,
        },
        result={"secret": pathological, "nested": {"token": "sk-should-not-leak"}},
    )
    view = serialize_step(step)
    assert view is not None
    dumped = view.model_dump_json()
    assert short_marker not in dumped
    assert "A" * 50_000 not in dumped
    assert "<script>" not in dumped
    assert "sk-should-not-leak" not in dumped
    assert "\u79d8\u5bc6\u306e\u30d7\u30ed\u30f3\u30d7\u30c8" not in dumped
    # The redacted view stays small regardless of how large the input was.
    assert len(dumped) < 500


def test_non_string_detail_is_dropped_not_leaked():
    """Defense in depth (hostile-input coverage): ``detail`` is documented to
    always be a fixed, bounded string set by the runtime itself, but a future
    bug could hand ``serialize_step`` something structured (e.g. the parsed
    arguments dict) by mistake. ``_safe_detail`` must drop non-string values
    entirely rather than stringify and surface them."""
    for hostile_detail in (
        {"query": "should not leak"},
        ["also", "should", "not", "leak"],
        12345,
        None,
    ):
        view = serialize_step(
            AgentStep(kind="tool_error", tool="web_search", detail=hostile_detail)
        )
        assert view is not None
        assert view.detail is None
        assert "should not leak" not in view.model_dump_json()


def test_malformed_tool_name_is_sentineled_even_if_the_runtime_did_not():
    """Regression/defense in depth: ``runtime.run_agent_turn`` already replaces
    any tool name that isn't both registered and well-formed with the fixed
    ``"unknown_tool"`` sentinel before constructing an ``AgentStep`` -- but
    ``serialize_step`` is the actual persistence/SSE boundary, so it must not
    assume every caller got that right. A step built directly with a hostile
    (e.g. newline-bearing, forged-log-line) tool name must still be sentineled
    here, both in the persisted ``tool`` field and in the humanized ``label``."""
    hostile_name = "weather\nINFO ai4ia_api.agents.runtime agent tool ran: tool=admin_backdoor"
    step = AgentStep(kind="tool_result", tool=hostile_name)
    view = serialize_step(step)
    assert view is not None
    assert view.tool == "unknown_tool"
    assert view.label == "Ran unknown tool"
    dumped = view.model_dump_json()
    assert hostile_name not in dumped
    assert "admin_backdoor" not in dumped

    # Same guarantee for the denied/error label paths, which humanize the tool
    # name directly rather than going through ``_verbs``.
    denied = serialize_step(AgentStep(kind="tool_denied", tool=hostile_name, detail="denied"))
    assert denied is not None
    assert denied.tool == "unknown_tool"
    assert hostile_name not in denied.model_dump_json()

    # An overly long (but charset-clean) name is likewise out of bounds.
    overlong = "a" * 65
    overlong_view = serialize_step(AgentStep(kind="tool_result", tool=overlong))
    assert overlong_view is not None
    assert overlong_view.tool == "unknown_tool"