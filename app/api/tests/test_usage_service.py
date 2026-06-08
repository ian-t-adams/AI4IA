"""UsageService: billable/cost logic, never-raises durability, bounded summary."""
from __future__ import annotations

import pytest

from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.models import TokenUsage
from ai4ia_api.usage.pricing import PriceRate, PricingBook
from ai4ia_api.usage.service import MAX_SUMMARY_DAYS, UsageService


def _deployment() -> DeploymentOption:
    return DeploymentOption(
        region="eastus2", dataZone=None, sku="GlobalStandard", deploymentName="gpt-x-dep"
    )


def _pricing() -> PricingBook:
    return PricingBook(
        {"gpt-x": PriceRate(input_per_1m=2.0, output_per_1m=8.0)},
        currency="USD",
        version="p-1",
    )


def _service(repo=None, pricing=None, enabled=True) -> UsageService:
    return UsageService(repo or InMemoryUsageRepository(), pricing or _pricing(), enabled=enabled)


def _known_usage() -> TokenUsage:
    return TokenUsage(prompt=1000, completion=500, total=1500, known=True, complete=True, calls=1)


def test_build_record_billable_complete_known_with_price():
    rec = _service().build_record(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=_known_usage(),
        status="complete",
        agent=None,
        correlation_id="cid",
    )
    assert rec.billable is True
    assert rec.usageKnown is True and rec.usageComplete is True
    # 1000*2.0 + 500*8.0 = 2000 + 4000 = 6000 micro-USD.
    assert rec.costKnown is True
    assert rec.estCostMicroUsd == 6000
    assert rec.priceVersion == "p-1"
    assert rec.region == "eastus2"


def test_build_record_unknown_usage_not_billable_no_cost():
    rec = _service().build_record(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=TokenUsage.parse(None),  # unknown
        status="complete",
        agent=None,
        correlation_id=None,
    )
    assert rec.billable is False
    assert rec.costKnown is False
    assert rec.estCostMicroUsd is None
    assert rec.promptTokens is None


def test_build_record_incomplete_usage_not_billable():
    agg = TokenUsage.empty().add(_known_usage()).add(TokenUsage.parse(None))
    rec = _service().build_record(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=agg,
        status="complete",
        agent="researcher",
        correlation_id=None,
    )
    assert rec.usageKnown is True
    assert rec.usageComplete is False
    assert rec.billable is False  # not every call reported -> not billable
    assert rec.costKnown is False


def test_build_record_cancelled_is_not_billable_even_if_known():
    rec = _service().build_record(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=_known_usage(),
        status="cancelled",
        agent=None,
        correlation_id=None,
    )
    assert rec.status == "cancelled"
    assert rec.billable is False
    assert rec.costKnown is False


def test_build_record_billable_but_unknown_price_marks_cost_unknown():
    svc = _service(pricing=PricingBook({}, currency="USD", version=None))
    rec = svc.build_record(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=_known_usage(),
        status="complete",
        agent=None,
        correlation_id=None,
    )
    assert rec.billable is True  # usage is real and complete
    assert rec.costKnown is False  # but no price in the book


async def test_record_completion_persists_and_summarizes():
    repo = InMemoryUsageRepository()
    svc = _service(repo=repo)
    await svc.record_completion(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=_known_usage(),
    )
    summary = await svc.summarize("u1")
    assert summary.totalRequests == 1
    assert summary.billableRequests == 1
    assert summary.totalCostMicroUsd == 6000


async def test_record_completion_disabled_records_nothing():
    repo = InMemoryUsageRepository()
    svc = _service(repo=repo, enabled=False)
    await svc.record_completion(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=_known_usage(),
    )
    summary = await svc.summarize("u1")
    assert summary.totalRequests == 0


class _RaisingRepo:
    async def record(self, record):
        raise RuntimeError("ledger down")

    async def summarize(self, user_id, *, since, since_days, now):  # pragma: no cover
        raise RuntimeError("ledger down")

    async def close(self):
        return None


async def test_record_completion_never_raises_on_repo_failure():
    svc = _service(repo=_RaisingRepo())
    # Must swallow the ledger failure rather than propagate to the chat path.
    await svc.record_completion(
        user_id="u1",
        session_id="s1",
        model_id="gpt-x",
        deployment=_deployment(),
        usage=_known_usage(),
    )


@pytest.mark.parametrize("days,expected", [(0, 1), (1, 1), (45, 45), (1000, MAX_SUMMARY_DAYS)])
async def test_summarize_bounds_window(days, expected):
    svc = _service()
    summary = await svc.summarize("u1", since_days=days)
    assert summary.sinceDays == expected
