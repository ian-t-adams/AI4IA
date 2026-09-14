from datetime import datetime, timezone

import pytest
from azure.cosmos.exceptions import CosmosResourceExistsError

from ai4ia_api.usage.cosmos_repo import CosmosUsageRepository
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.models import UsageRecord, UsageRollupRow, cost_bearing_attempt
from ai4ia_api.usage.pricing import PricingBook
from ai4ia_api.usage.repository import UsageRecordConflict
from ai4ia_api.usage.service import UsageService
from tests.cosmos_deletion_fake import Container


def record():
    return UsageRecord(
        id="wf-operation-1", userId="owner", sessionId="session", model="m",
        status="complete", providerCompleted=True, usageKnown=True, usageComplete=True,
        promptTokens=5, completionTokens=7, totalTokens=12, calls=1,
        costKnown=True, estCostMicroUsd=42, priceVersion="original",
        createdAt=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )


@pytest.fixture(params=["memory", "cosmos"])
def repo(request):
    if request.param == "memory":
        return InMemoryUsageRepository()
    result = object.__new__(CosmosUsageRepository)
    result._usage = Container("userId")
    return result


async def test_exact_duplicate_is_confirmed_without_double_usage(repo):
    frozen = record()
    assert await repo.record_once(frozen) is True
    assert await repo.record_once(frozen.model_copy(deep=True)) is False
    with pytest.raises(UsageRecordConflict):
        await repo.record_once(frozen.model_copy(update={"totalTokens": 13}))
    rows = await repo.list_for_session("owner", "session", limit=10)
    assert len(rows) == 1
    assert rows[0].estCostMicroUsd == 42
    other = frozen.model_copy(update={"userId": "other"})
    assert await repo.record_once(other) is True


async def test_frozen_delivery_does_not_consult_current_prices(repo):
    service = UsageService(repo, PricingBook({}, currency="USD", version="new"))
    assert await service.record_frozen(record()) is True
    assert await service.record_frozen(record()) is False
    rows = await repo.list_for_session("owner", "session", limit=10)
    assert rows[0].priceVersion == "original"
    assert rows[0].estCostMicroUsd == 42


async def test_strict_delivery_propagates_write_failure():
    class Broken(InMemoryUsageRepository):
        async def record_once(self, record):
            raise RuntimeError("write failed")

    service = UsageService(Broken(), PricingBook({}, currency="USD", version=None))
    with pytest.raises(RuntimeError, match="write failed"):
        await service.record_frozen(record())
    service = UsageService(
        InMemoryUsageRepository(), PricingBook({}, currency="USD", version=None), enabled=False,
    )
    with pytest.raises(RuntimeError, match="disabled"):
        await service.record_frozen(record())


async def test_lost_create_ack_recovers_the_same_cosmos_row():
    repo = object.__new__(CosmosUsageRepository)
    repo._usage = Container("userId")
    create = repo._usage.create_item

    async def lost_ack(body):
        await create(body)
        raise ConnectionError("ack lost")

    repo._usage.create_item = lost_ack
    with pytest.raises(ConnectionError):
        await repo.record_once(record())
    repo._usage.create_item = create
    assert await repo.record_once(record()) is False
    assert len(repo._usage.items) == 1


async def test_missing_accounting_fields_cannot_default_into_a_match():
    repo = object.__new__(CosmosUsageRepository)
    repo._usage = Container("userId")
    body = record().model_dump(mode="json")
    del body["usageComplete"]
    await repo._usage.create_item(body)
    with pytest.raises(UsageRecordConflict):
        await repo.record_once(record())
    with pytest.raises(CosmosResourceExistsError):
        await repo._usage.create_item(body)


def test_uncertain_workflow_attempt_stays_cost_unknown_in_projected_rollups():
    uncertain = record().model_copy(update={
        "status": "error", "providerCompleted": False, "billable": False,
        "costKnown": False, "estCostMicroUsd": None, "workflowDispatchClaimed": True,
    })
    projected = UsageRollupRow.from_document(uncertain.model_dump(mode="json"))
    assert cost_bearing_attempt(uncertain)
    assert cost_bearing_attempt(projected)
    not_dispatched = uncertain.model_copy(update={"workflowDispatchClaimed": False})
    assert not cost_bearing_attempt(not_dispatched)
    assert not cost_bearing_attempt(UsageRollupRow.from_record(not_dispatched))
