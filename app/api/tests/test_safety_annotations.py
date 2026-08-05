"""Surfacing annotate-only content-safety verdicts.

Every model here runs under a non-blocking Responsible AI policy: the filters
are enabled, but nothing is ever refused. That makes the annotations the *only*
observable output of the safety system -- if they are discarded (as they were),
the system is invisible and unauditable even though it ran on every turn.

These tests use the payload shape Azure OpenAI actually returns, including the
two different verdict styles (severity for harm categories, boolean detection
for jailbreak/protected material).
"""
from __future__ import annotations

from ai4ia_api.gateway.client import parse_sse_line
from ai4ia_api.safety import merge_safety, parse_safety
from ai4ia_api.sessions.models import Message, MessageRole

# A realistic non-streaming body: prompt verdicts alongside completion verdicts.
AZURE_BODY = {
    "choices": [
        {
            "message": {"role": "assistant", "content": "hi"},
            "content_filter_results": {
                "hate": {"filtered": False, "severity": "safe"},
                "violence": {"filtered": False, "severity": "low"},
                "protected_material_text": {"filtered": False, "detected": False},
            },
        }
    ],
    "prompt_filter_results": [
        {
            "prompt_index": 0,
            "content_filter_results": {
                "hate": {"filtered": False, "severity": "safe"},
                "jailbreak": {"filtered": False, "detected": True},
            },
        }
    ],
}


def _find(safety, category, scope):
    return next(
        s for s in safety.signals if s.category == category and s.scope == scope
    )


def test_parses_both_verdict_styles_and_keeps_them_distinct():
    safety = parse_safety(AZURE_BODY)
    assert safety is not None

    harm = _find(safety, "violence", "completion")
    assert harm.severity == "low" and harm.detected is None

    detection = _find(safety, "jailbreak", "prompt")
    assert detection.detected is True and detection.severity is None


def test_prompt_and_completion_scopes_are_not_conflated():
    """'the user asked something flagged' and 'the model produced something
    flagged' are different statements and must stay separable."""
    safety = parse_safety(AZURE_BODY)
    assert safety is not None

    scopes = {(s.category, s.scope) for s in safety.signals}
    assert ("hate", "prompt") in scopes
    assert ("hate", "completion") in scopes


def test_only_non_safe_verdicts_are_notable():
    safety = parse_safety(AZURE_BODY)
    assert safety is not None
    assert safety.flagged is True

    notable = {(s.category, s.scope) for s in safety.notable}
    # low-severity violence and a detected jailbreak are notable...
    assert ("violence", "completion") in notable
    assert ("jailbreak", "prompt") in notable
    # ...while "safe" and "not detected" are not.
    assert ("hate", "prompt") not in notable
    assert ("protected_material_text", "completion") not in notable


def test_all_clear_is_reported_but_not_flagged():
    """An all-safe turn must still produce a record.

    'The filters ran and found nothing' is evidence; 'no annotations at all' is
    the absence of evidence. Collapsing the two would make the annotate-only
    posture unauditable.
    """
    safety = parse_safety(
        {"choices": [{"content_filter_results": {"hate": {"filtered": False, "severity": "safe"}}}]}
    )
    assert safety is not None
    assert safety.signals and safety.flagged is False


def test_absent_annotations_are_none_not_empty():
    assert parse_safety({"choices": [{"message": {"content": "hi"}}]}) is None
    assert parse_safety({}) is None


def test_malformed_payloads_never_raise():
    # A turn must not fail because its annotations were malformed.
    for payload in [
        None,
        "not a dict",
        {"choices": "nope"},
        {"choices": [{"content_filter_results": "nope"}]},
        {"choices": [{"content_filter_results": {"hate": "nope"}}]},
        {"prompt_filter_results": [None, 5]},
        {"choices": [{"content_filter_results": {"hate": {"severity": 5}}}]},
    ]:
        parse_safety(payload)  # must not raise


def test_signal_count_is_bounded():
    huge = {
        "choices": [
            {"content_filter_results": {f"c{i}": {"severity": "safe"} for i in range(500)}}
        ]
    }
    safety = parse_safety(huge)
    assert safety is not None and len(safety.signals) <= 32


def test_filtered_true_is_always_notable():
    """Today nothing is blocked. If the policy is ever flipped to blocking, that
    must show up here rather than changing behaviour silently."""
    safety = parse_safety(
        {"choices": [{"content_filter_results": {"hate": {"filtered": True, "severity": "high"}}}]}
    )
    assert safety is not None and safety.flagged is True


# --- streaming ---------------------------------------------------------------


def test_sse_chunks_carry_annotations():
    line = 'data: {"choices":[{"delta":{"content":"x"},"content_filter_results":{"violence":{"filtered":false,"severity":"medium"}}}]}'
    chunk = parse_sse_line(line)

    assert chunk is not None and chunk.safety is not None
    assert _find(chunk.safety, "violence", "completion").severity == "medium"


def test_merge_combines_prompt_and_completion_across_chunks():
    """Azure reports prompt verdicts early and completion verdicts later, so the
    full picture only exists after merging."""
    first = parse_safety(
        {"prompt_filter_results": [{"content_filter_results": {"jailbreak": {"detected": False}}}]}
    )
    later = parse_safety(
        {"choices": [{"content_filter_results": {"hate": {"severity": "safe"}}}]}
    )

    merged = merge_safety(first, later)
    assert merged is not None
    scopes = {(s.category, s.scope) for s in merged.signals}
    assert scopes == {("jailbreak", "prompt"), ("hate", "completion")}


def test_later_verdict_wins_for_the_same_category_and_scope():
    # Azure refines a completion verdict as more of the answer streams; the last
    # word is the accurate one.
    early = parse_safety({"choices": [{"content_filter_results": {"violence": {"severity": "safe"}}}]})
    late = parse_safety({"choices": [{"content_filter_results": {"violence": {"severity": "high"}}}]})

    merged = merge_safety(early, late)
    assert merged is not None
    assert _find(merged, "violence", "completion").severity == "high"


def test_merge_handles_missing_sides():
    only = parse_safety({"choices": [{"content_filter_results": {"hate": {"severity": "safe"}}}]})
    assert merge_safety(None, only) is only
    assert merge_safety(only, None) is only
    assert merge_safety(None, None) is None


# --- persistence -------------------------------------------------------------


def test_message_round_trips_safety_and_defaults_to_none():
    """Additive optional field: rows written before this feature must still load."""
    legacy = Message.model_validate(
        {"sessionId": "s", "userId": "u", "role": "assistant", "content": "hi"}
    )
    assert legacy.safety is None

    with_safety = Message(
        sessionId="s", userId="u", role=MessageRole.assistant, safety=parse_safety(AZURE_BODY)
    )
    restored = Message.model_validate(with_safety.model_dump(mode="json"))
    assert restored.safety is not None
    assert _find(restored.safety, "jailbreak", "prompt").detected is True
