"""TokenUsage aggregation + summarize_records honesty semantics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai4ia_api.usage.models import (
    TokenUsage,
    UsageRecord,
    summarize_records,
)


def test_parse_known_usage():
    tu = TokenUsage.parse({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert tu.known is True
    assert tu.complete is True
    assert (tu.prompt, tu.completion, tu.total, tu.calls) == (10, 5, 15, 1)


def test_parse_missing_usage_is_unknown_single_call():
    tu = TokenUsage.parse(None)
    assert tu.known is False
    assert tu.complete is False
    assert tu.calls == 1
    assert tu.total == 0


def test_parse_empty_dict_is_unknown():
    assert TokenUsage.parse({}).known is False
    # An object with only nulls is still unknown, not zero.
    assert TokenUsage.parse({"prompt_tokens": None}).known is False


def test_parse_malformed_numeric_fields_never_raises():
    # A provider returning non-numeric usage must degrade to unknown, not raise:
    # metering can never break the chat turn that produced the usage.
    for bad in ({"prompt_tokens": "lots"}, {"completion_tokens": object()}, {"total_tokens": [1]}):
        tu = TokenUsage.parse(bad)
        assert tu.known is False
        assert tu.complete is False
        assert tu.calls == 1


def test_parse_derives_total_when_absent():
    tu = TokenUsage.parse({"prompt_tokens": 7, "completion_tokens": 3})
    assert tu.total == 10 and tu.known is True


def test_add_all_known_stays_complete():
    agg = TokenUsage.empty()
    agg = agg.add(TokenUsage.parse({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}))
    agg = agg.add(TokenUsage.parse({"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}))
    assert agg.known is True
    assert agg.complete is True
    assert agg.calls == 2
    assert (agg.prompt, agg.completion, agg.total) == (3, 3, 6)


def test_add_one_unknown_call_makes_aggregate_incomplete():
    agg = TokenUsage.empty()
    agg = agg.add(TokenUsage.parse({"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}))
    agg = agg.add(TokenUsage.parse(None))  # a call that did not report usage
    assert agg.known is True  # we did see some usage
    assert agg.complete is False  # but not for every call
    assert agg.calls == 2
    assert agg.total == 10  # the unknown call contributed 0 tokens


def test_empty_aggregate_is_known_false():
    agg = TokenUsage.empty()
    assert agg.known is False
    assert agg.calls == 0
    # An empty aggregate is vacuously complete (no call failed to report).
    assert agg.complete is True


def _rec(**kw) -> UsageRecord:
    base = dict(userId="u1", sessionId="s1", provider="azure_openai", model="gpt-5.2", deployment="dep")
    base.update(kw)
    return UsageRecord(**base)


def test_usage_record_defaults_provider_and_nullable_deployment():
    record = UsageRecord.model_validate(
        {"userId": "u1", "sessionId": "s1", "model": "gpt-5.2"}
    )
    assert record.provider == "azure_openai"
    assert record.deployment is None
    assert record.target is None


def test_usage_record_serializes_new_provider_and_target_fields():
    record = _rec(provider="speech_voice_live", deployment=None, target="managed_voice_live")
    payload = record.model_dump(mode="json")
    assert payload["provider"] == "speech_voice_live"
    assert payload["deployment"] is None
    assert payload["target"] == "managed_voice_live"
    assert UsageRecord.model_validate(payload) == record


def test_summarize_counts_and_costs():
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=30)
    records = [
        _rec(
            status="complete",
            billable=True,
            usageKnown=True,
            usageComplete=True,
            promptTokens=100,
            completionTokens=50,
            totalTokens=150,
            costKnown=True,
            estCostMicroUsd=2000,
            createdAt=now,
        ),
        _rec(
            status="complete",
            billable=True,
            usageKnown=True,
            usageComplete=True,
            promptTokens=10,
            completionTokens=10,
            totalTokens=20,
            costKnown=False,  # billable but no price -> cost-unknown
            createdAt=now,
        ),
        _rec(status="cancelled", usageKnown=False, createdAt=now),
        _rec(status="error", usageKnown=False, createdAt=now),
    ]
    s = summarize_records("u1", records, since_days=30, from_time=since, to_time=now)
    assert s.totalRequests == 4
    assert s.billableRequests == 2
    assert s.cancelledRequests == 1
    assert s.erroredRequests == 1
    assert s.unknownUsageRequests == 2
    assert s.totalTokens == 170
    assert s.totalCostMicroUsd == 2000
    assert s.costUnknownRequests == 1  # the billable-but-priceless turn
    # Per-model bucket marks cost as not fully known.
    bucket = next(b for b in s.byModel if b.model == "gpt-5.2")
    assert bucket.requests == 4
    assert bucket.costKnown is False


def test_summarize_per_model_and_per_day_ignore_unknown_usage_tokens():
    """A record with usageKnown=False must not contribute tokens to the
    byModel/byDay breakdowns, mirroring the top-level total. Real records never
    carry stale token fields alongside usageKnown=False (build_record nulls
    them out), but the aggregator must not rely on that invariant holding for
    every past or future record it is handed (e.g. legacy Cosmos rows)."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=30)
    records = [
        _rec(
            status="complete",
            billable=True,
            usageKnown=True,
            promptTokens=100,
            completionTokens=50,
            totalTokens=150,
            createdAt=now,
        ),
        # Stale/unexpected shape: usage explicitly unknown, but token fields
        # are still populated. Must be excluded from every breakdown.
        _rec(
            status="error",
            billable=False,
            usageKnown=False,
            promptTokens=999,
            completionTokens=999,
            totalTokens=1998,
            createdAt=now,
        ),
    ]
    s = summarize_records("u1", records, since_days=30, from_time=since, to_time=now)
    assert s.totalTokens == 150  # top-level total already guarded this
    bucket = next(b for b in s.byModel if b.model == "gpt-5.2")
    assert bucket.totalTokens == 150
    assert bucket.promptTokens == 100
    assert bucket.completionTokens == 50
    day_bucket = s.byDay[0]
    assert day_bucket.totalTokens == 150
