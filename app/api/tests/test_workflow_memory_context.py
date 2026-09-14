import copy

import pytest

from ai4ia_api.memory.context_refs import MemoryReference
from ai4ia_api.memory.cosmos_store import CosmosMemoryStore
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.models import MemoryRecord
from ai4ia_api.memory.preferences import MemoryPreferenceConflict, MemoryPreferenceUnavailable
from ai4ia_api.memory.service import MemoryService
from ai4ia_api.workflows.automation_common import AutomationError
from ai4ia_api.workflows.dispatch_scope import workflow_execution_scope
from tests.cosmos_deletion_fake import Container


async def seed():
    container = Container("userId")
    store = CosmosMemoryStore(container=container, expected_dim=2, embedding_model="test")
    state = await store.capture_state("owner")
    record = await store.commit_create(
        state, MemoryRecord(id="memory-one", user_id="owner", text="A previously read fact."),
        [1.0, 0.0],
    )
    return store, container, record, await store.get_preference("owner")


async def test_unchanged_memory_proves_current_without_changing_content_version():
    store, container, record, preference = await seed()
    reference = MemoryReference.from_record(record)
    await store.validate_context_references("owner", preference, [reference])
    after = await store.get_memory("owner", record.id)
    assert MemoryReference.from_record(after) == reference
    assert after.etag != record.etag
    assert any(write[0] == "batch" for write in container.writes)


@pytest.mark.parametrize("change", ["delete", "preference", "forget"])
async def test_revoked_memory_context_cannot_be_used_again(change):
    store, container, record, preference = await seed()
    reference = MemoryReference.from_record(record)
    await store.validate_context_references("owner", preference, [reference])
    if change == "delete":
        await container.delete_item(item=record.id, partition_key="owner")
    elif change == "preference":
        await store.set_preference("owner", False, expected_etag=preference.etag)
    else:
        state = container.items[("owner", "state")]
        container._put({**state, "epoch": 1, "cutoffs": [{
            "key": "user", "scope": "user", "scopeId": None, "beforeEpoch": 1,
            "startedAt": "2026-09-10T00:00:00Z",
        }]})
    with pytest.raises((MemoryPreferenceConflict, MemoryPreferenceUnavailable)):
        await store.validate_context_references("owner", preference, [reference])


async def test_stale_memory_read_cannot_overwrite_a_changed_record_during_proof():
    store, container, record, preference = await seed()
    key = ("owner", record.id)
    stale = copy.deepcopy(container.items[key])
    updated = {**stale, "text": "Changed private fact.", "version": stale["version"] + 1}
    container._put(updated)
    container.stale_reads[key] = stale
    with pytest.raises(MemoryPreferenceUnavailable):
        await store.validate_context_references("owner", preference, [MemoryReference.from_record(record)])
    assert container.items[key]["text"] == "Changed private fact."


class Embedder:
    async def embed_one(self, text):
        return [1.0, 0.0]


class EffectScope:
    owner_id = "owner"

    def __init__(self, safe):
        self.safe = safe

    async def before_effect(self, effect):
        if self.safe:
            raise AutomationError("unsafe_effect", "Safe-only mutation refused.")


@pytest.mark.parametrize("safe", [False, True])
async def test_actual_ambient_memory_write_is_blocked_only_in_safe_scope(safe):
    store = InMemoryVectorStore(expected_dim=2)
    memory = MemoryService(store=store, embedder=Embedder())
    with workflow_execution_scope(EffectScope(safe)):
        if safe:
            with pytest.raises(AutomationError, match="Safe-only"):
                await memory.remember("owner", "session", "This is a durable user fact.")
        else:
            assert await memory.remember("owner", "session", "This is a durable user fact.") == "saved"
    assert len(await store.search("owner", [1.0, 0.0], 10)) == (0 if safe else 1)
