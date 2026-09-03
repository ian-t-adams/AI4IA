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
from ai4ia_api.safety import (
    MAX_SEVERITY_LEVEL,
    SafetyStatus,
    merge_safety,
    parse_safety,
    provider_for_api,
    safety_assessment,
    severity_level,
    unavailable_safety,
)
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


def test_responses_api_content_filters_are_preserved():
    safety = parse_safety(
        {
            "content_filters": [
                {
                    "blocked": False,
                    "source_type": "prompt",
                    "content_filter_results": {
                        "self_harm": {
                            "filtered": False,
                            "severity": "medium",
                        },
                        "indirect_attack": {
                            "filtered": False,
                            "detected": True,
                        },
                    },
                },
                {
                    "blocked": False,
                    "source_type": "completion",
                    "content_filter_results": {
                        "violence": {
                            "filtered": False,
                            "severity": "low",
                        }
                    },
                },
            ]
        }
    )

    assert safety is not None
    assert _find(safety, "self_harm", "prompt").severityLevel == 2
    assert _find(safety, "indirect_attack", "prompt").detected is True
    assert _find(safety, "violence", "completion").severityLevel == 1


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


def test_content_filter_error_is_unavailable_not_clean():
    safety = parse_safety(
        {
            "choices": [
                {
                    "content_filter_results": {
                        "error": {
                            "code": "content_filter_timeout",
                            "message": "provider details",
                        }
                    }
                }
            ]
        }
    )

    assert safety is not None
    assert safety.status is SafetyStatus.unavailable
    assert safety.signals == []
    assert safety.errors == ["content_filter_timeout"]


def test_content_filter_error_with_signals_is_partial():
    safety = parse_safety(
        {
            "choices": [
                {
                    "content_filter_results": {
                        "violence": {
                            "filtered": False,
                            "severity": "low",
                        },
                        "error": {"code": "one_filter_failed"},
                    }
                }
            ]
        }
    )

    assert safety is not None
    assert safety.status is SafetyStatus.partial
    assert safety.flagged is True
    assert safety.errors == ["one_filter_failed"]


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
    assert safety.signalCount == 500
    assert safety.truncated is True


def test_truncation_prioritizes_late_notable_assessment():
    merged = None
    for model_call in range(1, 6):
        content_filter_results = {
            f"safe_{model_call}_{index}": {
                "filtered": False,
                "severity": "safe",
            }
            for index in range(8)
        }
        if model_call == 5:
            content_filter_results["violence"] = {
                "filtered": False,
                "severity": "high",
            }
        current = parse_safety(
            {
                "choices": [
                    {"content_filter_results": content_filter_results}
                ]
            }
        )
        assert current is not None
        for signal in current.signals:
            signal.modelCall = model_call
        merged = merge_safety(merged, current)

    assert merged is not None
    assert merged.truncated is True
    assert merged.flagged is True
    notable = _find(merged, "violence", "completion")
    assert notable.modelCall == 5


def test_filtered_true_is_always_notable():
    """Today nothing is blocked. If the policy is ever flipped to blocking, that
    must show up here rather than changing behaviour silently."""
    safety = parse_safety(
        {"choices": [{"content_filter_results": {"hate": {"filtered": True, "severity": "high"}}}]}
    )
    assert safety is not None and safety.flagged is True


# --- normalized severity -----------------------------------------------------


def test_severity_carries_a_normalized_ordinal_beside_the_raw_value():
    """"medium" means nothing to a reader who does not know the scale has four
    steps. The ordinal supplies the scale; the provider's own string is never
    overwritten by it."""
    safety = parse_safety(AZURE_BODY)
    assert safety is not None

    harm = _find(safety, "violence", "completion")
    assert harm.severity == "low"
    assert harm.severityLevel == 1
    assert MAX_SEVERITY_LEVEL == 3

    clean = _find(safety, "hate", "prompt")
    assert clean.severity == "safe" and clean.severityLevel == 0


def test_unknown_severity_stays_unranked_rather_than_being_guessed_at():
    """A provider value outside the known scale is shown as itself. Ranking it
    would assert a position on a scale it may not belong to."""
    safety = parse_safety(
        {"choices": [{"content_filter_results": {"hate": {"severity": "catastrophic"}}}]}
    )
    assert safety is not None
    signal = _find(safety, "hate", "completion")
    assert signal.severity == "catastrophic"
    assert signal.severityLevel is None


def test_detection_filters_have_no_severity_ordinal():
    safety = parse_safety(AZURE_BODY)
    assert safety is not None
    detection = _find(safety, "jailbreak", "prompt")
    assert detection.severityLevel is None


def test_severity_level_covers_the_whole_scale():
    assert [severity_level(s) for s in ("safe", "low", "medium", "high")] == [0, 1, 2, 3]
    assert severity_level(None) is None
    assert severity_level("nonsense") is None


# --- assessment coverage ------------------------------------------------------


def test_reported_assessment_records_which_halves_were_assessed():
    safety = parse_safety(AZURE_BODY)
    assert safety is not None
    assert safety.status is SafetyStatus.reported
    assert safety.coverage == ["prompt", "completion"]
    assert safety.mode == "annotate_only"


def test_coverage_reflects_a_completion_only_assessment():
    """Control for the field: it has to be able to say something else."""
    safety = parse_safety(
        {"choices": [{"content_filter_results": {"hate": {"severity": "safe"}}}]}
    )
    assert safety is not None and safety.coverage == ["completion"]


def test_an_unreturned_assessment_is_explicit_not_missing():
    """Under an annotate-only policy, "nobody assessed this" and "assessed and
    clean" look identical to a reader unless the first one says so."""
    safety = safety_assessment({"choices": [{"message": {"content": "hi"}}]},
                               provider="azure_openai")
    assert safety.status is SafetyStatus.unavailable
    assert safety.signals == [] and safety.coverage == []
    assert safety.provider == "azure_openai"
    assert safety.flagged is False and safety.assessed is False


def test_assessment_does_not_fabricate_a_verdict():
    unavailable = unavailable_safety("azure_openai")
    # No signal at all -- in particular, nothing claiming "safe".
    assert unavailable.signals == []
    assert unavailable.notable == []


def test_assessment_passes_real_annotations_through_untouched():
    """Control: the total wrapper must not flatten a genuine assessment into
    the unavailable case."""
    safety = safety_assessment(AZURE_BODY, provider="azure_openai")
    assert safety.status is SafetyStatus.reported
    assert safety.assessed is True
    assert _find(safety, "jailbreak", "prompt").detected is True


def test_provider_label_distinguishes_the_anthropic_surface():
    assert provider_for_api("chat") == "azure_openai"
    assert provider_for_api("responses") == "azure_openai"
    assert provider_for_api("anthropic") == "azure_foundry_anthropic"


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


def test_a_row_written_before_coverage_existed_still_means_what_it_meant():
    """The coverage fields are additive. An older document carries signals and
    no status, and must keep reading as a reported assessment rather than
    becoming an "unavailable" one."""
    legacy = Message.model_validate(
        {
            "sessionId": "s",
            "userId": "u",
            "role": "assistant",
            "content": "hi",
            "safety": {
                "signals": [
                    {
                        "category": "hate",
                        "scope": "completion",
                        "severity": "low",
                        "filtered": False,
                    }
                ]
            },
        }
    )
    assert legacy.safety is not None
    assert legacy.safety.status is SafetyStatus.reported
    assert legacy.safety.mode == "annotate_only"
    # An older row carries no ordinal; it is absent, not wrong.
    assert legacy.safety.signals[0].severityLevel is None
    assert legacy.safety.flagged is True


def test_unavailable_assessment_round_trips():
    message = Message(
        sessionId="s",
        userId="u",
        role=MessageRole.assistant,
        safety=unavailable_safety("azure_openai"),
    )
    restored = Message.model_validate(message.model_dump(mode="json"))
    assert restored.safety is not None
    assert restored.safety.status is SafetyStatus.unavailable
