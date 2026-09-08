from __future__ import annotations

import copy

import pytest
from azure.cosmos.exceptions import CosmosHttpResponseError
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ai4ia_api.auth.dependencies import get_current_user
from ai4ia_api.main import create_app
from ai4ia_api.memory.cosmos_service import CosmosMemoryService
from ai4ia_api.memory.cosmos_store import CosmosMemoryStore
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.models import MemoryRecord
from ai4ia_api.memory.planner import MemoryPlan
from ai4ia_api.memory.preferences import (
    MemoryPreference,
    MemoryPreferenceConflict,
    MemoryPreferenceUnavailable,
)
from ai4ia_api.memory.service import MemoryService
from tests.conftest import make_settings
from tests.test_cosmos_memory import FakeCosmosContainer


class RecordingEmbedder:
    def __init__(self):
        self.calls = []

    async def embed_one(self, text):
        self.calls.append(text)
        return [1.0, 0.0]

    async def embed(self, inputs):
        return [await self.embed_one(text) for text in inputs]


class RecordingPlanner:
    def __init__(self):
        self.calls = []
        self.result = None

    async def plan(self, text, candidates):
        self.calls.append((text, list(candidates)))
        return self.result or MemoryPlan(action="add", text=text)


def cosmos_memory():
    container = FakeCosmosContainer()
    store = CosmosMemoryStore(container=container, expected_dim=2)
    embedder = RecordingEmbedder()
    planner = RecordingPlanner()
    service = CosmosMemoryService(
        store=store, embedder=embedder, planner=planner, embedding_model="test-embedding"
    )
    return service, store, embedder, planner, container


async def set_automatic(memory, user_id, enabled):
    current = await memory.get_preference(user_id)
    return await memory.set_preference(user_id, enabled, expected_etag=current.etag)


@pytest.fixture(params=["cosmos", "local"])
def backend(request):
    if request.param == "cosmos":
        service, store, embedder, _planner, _container = cosmos_memory()
    else:
        store = InMemoryVectorStore()
        embedder = RecordingEmbedder()
        service = MemoryService(store=store, embedder=embedder)
    return service, store, embedder


async def test_default_on_off_reenable_are_owner_scoped_and_non_destructive(backend):
    memory, store, embedder = backend
    assert await memory.get_preference("alice") == MemoryPreference()
    assert await memory.remember("alice", "a", "Alice prefers concise answers") == "saved"
    assert await memory.remember("bob", "b", "Bob prefers detailed answers") == "saved"
    assert [record.text for record in await memory.recall("alice", "preferences")] == [
        "Alice prefers concise answers"
    ]

    disabled = await set_automatic(memory, "alice", False)
    calls = len(embedder.calls)
    assert await memory.recall("alice", "preferences") == []
    assert await memory.remember("alice", "a", "Do not automatically save this") == "disabled"
    assert len(embedder.calls) == calls
    assert (await memory.get_preference("bob")).automatic_enabled
    assert [record.text for record in await memory.recall("bob", "preferences")] == [
        "Bob prefers detailed answers"
    ]
    assert [record.text for record in await store.search("alice", [1.0, 0.0], 5)] == [
        "Alice prefers concise answers"
    ]

    enabled = await set_automatic(memory, "alice", True)
    assert enabled.version > disabled.version
    assert await memory.recall("alice", "preferences")
    assert await memory.remember("alice", "a", "Alice also prefers Python examples") == "saved"


async def test_preference_cas_survives_ordinary_writes_but_rejects_stale_toggles(backend):
    memory, _store, _embedder = backend
    original = await memory.get_preference("alice")
    await memory.remember("alice", "s", "A durable fact that changes the state ETag")
    disabled = await memory.set_preference("alice", False, expected_etag=original.etag)
    with pytest.raises(MemoryPreferenceConflict):
        await memory.set_preference("alice", True, expected_etag=original.etag)
    assert await memory.get_preference("alice") == disabled


async def test_historical_state_reads_default_on_without_rewriting_records():
    memory, store, _embedder, _planner, container = cosmos_memory()
    await store.capture_state("alice")
    before = copy.deepcopy(container.items)
    assert "automaticMemoryEnabled" not in before[("alice", "state")]
    assert await memory.get_preference("alice") == MemoryPreference()
    assert container.items == before


async def test_explicit_crud_document_save_and_forget_preserve_disabled_preference():
    memory, _store, _embedder, _planner, _container = cosmos_memory()
    disabled = await set_automatic(memory, "alice", False)
    original = await memory.create_memory("alice", "Keep this explicit owner record")
    updated = await memory.update_memory(
        "alice", original.id, "The owner edited this record", expected_etag=original.etag
    )
    assert updated.locked and updated.origin == "user"
    assert [record.id for record in await memory.list_memories("alice")] == [original.id]
    assert await memory.delete_memory("alice", updated.id, expected_etag=updated.etag)
    assert await memory.remember_document(
        "alice", items=["An explicitly saved document"], session_id="s", document_id="d"
    ) == 1
    assert await memory.forget_document("alice", "d") == 1
    assert await memory.remember_document("alice", items=["An explicit session note"], session_id="s") == 1
    assert await memory.forget_session("alice", "s") == 1
    await memory.create_memory("alice", "Another explicit owner record")
    await memory.create_memory("bob", "Another user's untouched record")
    assert await memory.forget_user("alice") == 1
    assert await memory.get_preference("alice") == disabled
    assert len(await memory.list_memories("bob")) == 1


@pytest.mark.parametrize("stage", ["embed", "search"])
@pytest.mark.parametrize("disable", [False, True])
async def test_recall_rechecks_after_each_await(backend, monkeypatch, stage, disable):
    memory, store, embedder = backend
    await memory.remember("alice", "s", "A previously saved durable fact")
    target, method = (embedder, "embed_one") if stage == "embed" else (store, "search")
    original = getattr(target, method)
    entered = []

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.append(True)
        if disable:
            await set_automatic(memory, "alice", False)
        return result

    monkeypatch.setattr(target, method, paused)
    recalled = await memory.recall("alice", "find that fact")
    assert entered
    assert bool(recalled) is not disable


@pytest.mark.parametrize("stage", ["candidate_embedding", "candidate_search", "planner", "write_embedding"])
@pytest.mark.parametrize("disable", [False, True])
async def test_planner_rechecks_after_each_await(monkeypatch, stage, disable):
    memory, store, embedder, planner, _container = cosmos_memory()
    target, method = {
        "candidate_embedding": (embedder, "embed_one"),
        "candidate_search": (store, "search"),
        "planner": (planner, "plan"),
        "write_embedding": (embedder, "embed_one"),
    }[stage]
    original = getattr(target, method)
    calls = 0
    entered = False

    async def paused(*args, **kwargs):
        nonlocal calls, entered
        result = await original(*args, **kwargs)
        calls += 1
        if calls == (2 if stage == "write_embedding" else 1):
            entered = True
            if disable:
                await set_automatic(memory, "alice", False)
        return result

    monkeypatch.setattr(target, method, paused)
    outcome = await memory.remember("alice", "s", "A fact proposed by a pending planner")
    assert entered
    assert outcome == ("disabled" if disable else "saved")
    assert bool(await memory.list_memories("alice")) is not disable
    if disable and stage in {"candidate_embedding", "candidate_search"}:
        assert planner.calls == []
    else:
        assert len(planner.calls) == 1


@pytest.mark.parametrize("action", ["add", "update", "delete"])
@pytest.mark.parametrize("transition", ["unchanged", "disable", "disable_reenable"])
async def test_transaction_fence_blocks_automatic_commit_after_disable(monkeypatch, action, transition):
    memory, store, _embedder, planner, container = cosmos_memory()
    target = MemoryRecord(id="m-target", user_id="alice", text="Original mutable fact")
    await store.commit_create(await store.capture_state("alice"), target, [1.0, 0.0])
    planner.result = MemoryPlan(
        action=action,
        memory_id=target.id if action != "add" else None,
        text="A new durable fact" if action != "delete" else None,
    )
    original_batch = container.execute_item_batch
    entered = 0

    async def paused_batch(**kwargs):
        nonlocal entered
        entered += 1
        if entered == 1 and transition != "unchanged":
            await set_automatic(memory, "alice", False)
            if transition == "disable_reenable":
                await set_automatic(memory, "alice", True)
        return await original_batch(**kwargs)

    monkeypatch.setattr(container, "execute_item_batch", paused_batch)
    outcome = await memory.remember("alice", "s", "A pending automatic memory operation")
    assert entered == 1
    records = await memory.list_memories("alice")
    if transition != "unchanged":
        assert outcome == "disabled"
        assert [(record.id, record.text) for record in records] == [(target.id, target.text)]
        assert (await memory.get_preference("alice")).automatic_enabled is (transition == "disable_reenable")
    else:
        assert outcome == ("removed" if action == "delete" else "saved")
        assert len(records) == {"add": 2, "update": 1, "delete": 0}[action]
        if action == "update":
            assert records[0].text == "A new durable fact"


@pytest.mark.parametrize("transition", ["unchanged", "disable", "disable_reenable"])
async def test_atomic_add_does_not_revive_a_pre_disable_write(backend, monkeypatch, transition):
    memory, store, embedder = backend
    original = embedder.embed_one

    async def paused(text):
        vector = await original(text)
        if transition != "unchanged":
            await set_automatic(memory, "alice", False)
            if transition == "disable_reenable":
                await set_automatic(memory, "alice", True)
        return vector

    monkeypatch.setattr(embedder, "embed_one", paused)
    outcome = await memory.remember("alice", "s", "A pre-disable automatic write")
    assert outcome == ("saved" if transition == "unchanged" else "disabled")
    assert bool(await store.search("alice", [1.0, 0.0], 5)) is (transition == "unchanged")


@pytest.mark.parametrize("invalid", [None, "false", 0, 1])
async def test_unknown_preference_never_defaults_to_on(invalid):
    memory, store, embedder, planner, container = cosmos_memory()
    await store.capture_state("alice")
    container.items[("alice", "state")]["automaticMemoryEnabled"] = invalid
    with pytest.raises(MemoryPreferenceUnavailable):
        await memory.get_preference("alice")
    assert await memory.recall("alice", "query") == []
    assert await memory.remember("alice", "s", "Do not save on a corrupt preference") == "unavailable"
    assert embedder.calls == planner.calls == []


@pytest.fixture
def preference_client():
    app = create_app(make_settings())
    with TestClient(app) as client:
        memory, _store, _embedder, _planner, container = cosmos_memory()
        app.state.memory = memory
        yield client, container


def test_owner_get_patch_contract_and_existing_management(preference_client):
    client, _container = preference_client
    alice, bob = {"X-Dev-User": "alice"}, {"X-Dev-User": "bob"}
    initial = client.get("/api/memories/preference", headers=alice)
    assert initial.status_code == 200
    assert initial.json() == {"automaticMemoryEnabled": True, "etag": '"memory-preference-0"'}
    assert initial.headers["cache-control"] == "no-store"
    disabled = client.patch(
        "/api/memories/preference?userId=bob",
        headers={**alice, "If-Match": initial.headers["etag"]},
        json={"automaticMemoryEnabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["automaticMemoryEnabled"] is False
    assert client.get("/api/memories/preference", headers=bob).json()["automaticMemoryEnabled"] is True
    assert client.patch(
        "/api/memories/preference", headers={**alice, "If-Match": initial.headers["etag"]},
        json={"automaticMemoryEnabled": True},
    ).status_code == 409
    created = client.post("/api/memories", headers=alice, json={"text": "An explicit owner memory"})
    assert created.status_code == 201
    memory_id = created.json()["id"]
    assert client.get("/api/memories", headers=alice).json()["items"][0]["id"] == memory_id
    assert client.get("/api/memories", headers=bob).json()["items"] == []
    changed = client.patch(
        f"/api/memories/{memory_id}", headers={**alice, "If-Match": created.headers["etag"]},
        json={"text": "An explicit owner edit"},
    )
    assert changed.status_code == 200
    assert client.delete(f"/api/memories/{memory_id}", headers=bob).status_code == 404
    assert client.delete(f"/api/memories/{memory_id}", headers=alice).status_code == 204
    for tool in client.get("/api/tools", headers=alice).json()["tools"]:
        if tool["name"] in {"recall_memory", "remember_memory"}:
            assert tool["available"] is False
    enabled = client.patch(
        "/api/memories/preference", headers={**alice, "If-Match": disabled.headers["etag"]},
        json={"automaticMemoryEnabled": True},
    )
    assert enabled.status_code == 200 and enabled.json()["automaticMemoryEnabled"] is True


@pytest.mark.parametrize("body", [
    {}, {"automaticMemoryEnabled": "false"}, {"automaticMemoryEnabled": None},
    {"automaticMemoryEnabled": 0}, {"automaticMemoryEnabled": False, "userId": "bob"},
])
def test_patch_requires_a_strict_boolean_and_forbids_owner_override(preference_client, body):
    client, _container = preference_client
    response = client.patch(
        "/api/memories/preference", headers={"If-Match": '"memory-preference-0"'}, json=body,
    )
    assert response.status_code == 422
    assert client.get("/api/memories/preference").json()["automaticMemoryEnabled"] is True


def test_patch_requires_if_match(preference_client):
    client, _container = preference_client
    assert client.patch("/api/memories/preference", json={"automaticMemoryEnabled": False}).status_code == 422


def test_preference_outage_is_503_not_default_enabled(preference_client, monkeypatch):
    client, container = preference_client

    async def unavailable(**kwargs):
        raise CosmosHttpResponseError(status_code=503, message="storage unavailable")

    monkeypatch.setattr(container, "read_item", unavailable)
    assert client.get("/api/memories/preference").status_code == 503
    assert client.patch(
        "/api/memories/preference", headers={"If-Match": '"memory-preference-0"'},
        json={"automaticMemoryEnabled": False},
    ).status_code == 503


def test_preference_routes_require_authentication(preference_client):
    client, _container = preference_client

    async def unauthenticated():
        raise HTTPException(status_code=401, detail="Authentication required")

    client.app.dependency_overrides[get_current_user] = unauthenticated
    assert client.get("/api/memories/preference").status_code == 401
    assert client.patch(
        "/api/memories/preference", headers={"If-Match": '"memory-preference-0"'},
        json={"automaticMemoryEnabled": False},
    ).status_code == 401
