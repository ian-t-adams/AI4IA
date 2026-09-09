from __future__ import annotations

import copy

import pytest
from azure.cosmos.exceptions import CosmosResourceExistsError
from fastapi.testclient import TestClient

from ai4ia_api.main import create_app
from ai4ia_api.sessions.deletion_models import (
    FENCE_ID,
    DeletionDisabledError,
    DeletionIntegrityError,
    DeletionUnavailableError,
    InitializationCursorError,
    InitializationRecord,
    initialization_cursor,
    now_utc,
)
from ai4ia_api.sessions.deletion_service import ConversationDeletionService
from ai4ia_api.sessions.models import Session
from ai4ia_api.sessions.repository import SessionNotFoundError
from tests.conftest import make_settings
from tests.cosmos_deletion_fake import CosmosState
from tests.test_resumable_deletion import verified


class Crash(BaseException):
    pass


def private_session(sid="s1"):
    return Session(
        id=sid, userId="u1", title="PRIVATE TITLE", systemPrompt="PRIVATE INSTRUCTIONS",
        summary="PRIVATE SUMMARY", libraryDocumentIds=["PRIVATE DOC ID"],
    )


@pytest.mark.parametrize("point", ["reservation", "message_fence", "document_fence"])
async def test_each_prepublication_crash_is_content_free_visible_and_explicitly_recoverable(point):
    state = CosmosState()
    creator = state.repo()
    target = {
        "reservation": state.sessions,
        "message_fence": state.messages,
        "document_fence": state.documents,
    }[point]

    async def crash(body):
        target.after_create = None
        raise Crash()

    target.after_create = crash
    with pytest.raises(Crash):
        await creator.create_session(private_session())
    raw = state.sessions.items[("u1", "s1")]
    assert raw["kind"] == "session_initializing_v1"
    assert "PRIVATE" not in repr(state.snapshot())
    assert not {"title", "summary", "systemPrompt", "libraryDocumentIds", "toolConsent"} & raw.keys()
    assert len(state.messages.items) == (point != "reservation")
    assert len(state.documents.items) == (point == "document_fence")
    before = state.snapshot()
    recovered = state.repo()
    page = await recovered.list_initializations("u1")
    assert [item.sessionId for item in page.items] == ["s1"]
    assert page.observation == "not_completion_evidence"
    assert state.snapshot() == before
    assert await recovered.list_sessions("u1") == []
    assert (await recovered.list_initializations("u2")).items == []
    with pytest.raises(SessionNotFoundError):
        await recovered.get_session("u1", "s1")
    with pytest.raises(SessionNotFoundError):
        await recovered.patch_session("u1", "s1", {"title": "resurrect"})
    with pytest.raises(DeletionDisabledError):
        await state.repo(enabled=False).delete_session("u1", "s1")
    with pytest.raises(SessionNotFoundError):
        await recovered.begin_deletion("u2", "s1")
    # No GET closes a fence; only an explicit owner discard begins deletion.
    assert state.snapshot() == before
    await recovered.begin_deletion("u1", "s1")
    await verified(ConversationDeletionService(state.repo(), None), "u1", "s1")
    assert state.messages.items[("s1", FENCE_ID)]["closed"] is True
    assert state.documents.items[("s1", FENCE_ID)]["closed"] is True
    assert (await recovered.list_initializations("u1")).items == []


@pytest.mark.parametrize("discard_first", [False, True])
async def test_publication_and_discard_have_both_orderings_without_resurrection(discard_first):
    state = CosmosState()
    creator, deleter = state.repo(), state.repo()

    async def before_publish(item, body):
        if body.get("kind") == "session_v1" and discard_first:
            state.sessions.before_replace = None
            await deleter.begin_deletion("u1", "s1")
            await verified(ConversationDeletionService(deleter, None), "u1", "s1")

    state.sessions.before_replace = before_publish
    if discard_first:
        with pytest.raises(SessionNotFoundError):
            await creator.create_session(private_session())
        assert "PRIVATE" not in repr(state.snapshot())
    else:
        published = await creator.create_session(private_session())
        assert published.title == "PRIVATE TITLE"
        assert (await creator.get_session("u1", "s1")).systemPrompt == "PRIVATE INSTRUCTIONS"
        await deleter.begin_deletion("u1", "s1")
        await verified(ConversationDeletionService(deleter, None), "u1", "s1")
    with pytest.raises(SessionNotFoundError):
        await creator.get_session("u1", "s1")
    assert "PRIVATE" not in repr(state.snapshot())
    assert (await creator.get_deletion_status("u1", "s1")).state == "cleanup_verified"


async def test_successful_publication_lost_ack_is_normal_discoverable_conversation():
    state = CosmosState()
    repo = state.repo()
    real_replace = state.sessions.replace_item

    async def lost_ack(**kwargs):
        response = await real_replace(**kwargs)
        if kwargs["body"].get("kind") == "session_v1":
            raise Crash()
        return response

    state.sessions.replace_item = lost_ack
    with pytest.raises(Crash):
        await repo.create_session(private_session())
    state.sessions.replace_item = real_replace
    assert (await state.repo().list_initializations("u1")).items == []
    assert [session.id for session in await state.repo().list_sessions("u1")] == ["s1"]
    assert (await state.repo().get_session("u1", "s1")).title == "PRIVATE TITLE"


@pytest.mark.parametrize("existing", ["reservation", "active", "tombstone"])
async def test_duplicate_create_never_reuses_owner_reservation_or_terminal_generation(existing):
    state = CosmosState()
    repo = state.repo()
    if existing == "reservation":
        async def crash(body):
            state.sessions.after_create = None
            raise Crash()

        state.sessions.after_create = crash
        with pytest.raises(Crash):
            await repo.create_session(private_session())
    else:
        await repo.create_session(private_session())
        if existing == "tombstone":
            await repo.begin_deletion("u1", "s1")
            await verified(ConversationDeletionService(repo, None), "u1", "s1")
    before = state.snapshot()
    with pytest.raises(CosmosResourceExistsError):
        await state.repo().create_session(private_session())
    assert state.snapshot() == before
    assert (await state.repo().create_session(private_session("fresh"))).id == "fresh"


@pytest.mark.parametrize("protocol", [None, True, 0])
async def test_damaged_v1_marker_cannot_be_interpreted_as_legacy_after_flag_rollback(protocol):
    state = CosmosState()
    await state.repo().create_session(private_session())
    paused = state.repo(enabled=False)
    assert (await paused.get_session("u1", "s1")).title == "PRIVATE TITLE"
    raw = copy.deepcopy(state.sessions.items[("u1", "s1")])
    state.sessions._put(raw | {"deletionProtocol": protocol})
    before = state.snapshot()
    with pytest.raises(DeletionIntegrityError):
        await paused.get_session("u1", "s1")
    with pytest.raises(DeletionDisabledError):
        await paused.delete_session("u1", "s1")
    assert state.snapshot() == before


@pytest.mark.parametrize("bad", ["owner", "generation", "closed"])
async def test_existing_fence_cannot_be_adopted_for_new_publication(bad):
    state = CosmosState()

    async def conflicting_fence(body):
        state.sessions.after_create = None
        fence = {
            "id": FENCE_ID, "sessionId": "s1", "userId": body["userId"],
            "kind": "session_fence_v1", "epoch": body["deletionEpoch"],
            "closed": False, "ttl": -1,
        }
        if bad == "owner":
            fence["userId"] = "u2"
        elif bad == "generation":
            fence["epoch"] = "another-generation"
        else:
            fence["closed"] = True
        await state.messages.create_item(fence)

    state.sessions.after_create = conflicting_fence
    with pytest.raises(DeletionIntegrityError):
        await state.repo().create_session(private_session())
    assert "PRIVATE" not in repr(state.snapshot())
    assert state.sessions.items[("u1", "s1")]["kind"] == "session_initializing_v1"
    assert [item.sessionId for item in (await state.repo().list_initializations("u1")).items] == ["s1"]


async def test_owner_initialization_query_is_bounded_opaque_and_not_completion_evidence():
    state = CosmosState()
    for index in range(52):
        record = InitializationRecord(
            id=f"s{index:03}", userId="u1", kind="session_initializing_v1",
            deletionEpoch="generation", createdAt=now_utc(),
        )
        await state.sessions.create_item(record.model_dump(mode="json"))
    repo = state.repo()
    before = state.snapshot()
    page = await repo.list_initializations("u1")
    assert len(page.items) == 50 and page.hasMore
    assert page.nextCursor == initialization_cursor("s049") and page.nextCursor != "s049"
    rest = await repo.list_initializations("u1", page.nextCursor)
    assert len(rest.items) == 2 and not rest.hasMore
    assert (await repo.list_initializations("u2", page.nextCursor)).items == []
    assert state.snapshot() == before
    for invalid in ("!", "a", initialization_cursor("../"), " "):
        with pytest.raises(InitializationCursorError):
            await repo.list_initializations("u1", invalid)
    state.sessions.stale_queries = True
    empty_observation = await repo.list_initializations("u1")
    assert empty_observation.items == []
    assert empty_observation.observation == "not_completion_evidence"
    assert state.snapshot() == before


@pytest.mark.parametrize("raw", [
    None, {}, {"id": "s1", "userId": "u1", "deletionEpoch": "g"},
    {
        "id": "s1", "userId": "u2", "deletionEpoch": "g", "kind": "session_initializing_v1",
        "deletionProtocol": 1, "ttl": -1, "createdAt": "2026-09-09T12:00:00Z",
    },
    {
        "id": "s1", "userId": "u1", "deletionEpoch": "g", "kind": "session_initializing_v1",
        "deletionProtocol": 1, "ttl": -1,
    },
])
async def test_malformed_initialization_observation_is_not_empty_success(raw):
    state = CosmosState()

    async def malformed(**kwargs):
        yield raw

    state.sessions.query_items = malformed
    with pytest.raises(DeletionIntegrityError):
        await state.repo().list_initializations("u1")


@pytest.mark.parametrize("container_name", ["sessions", "messages", "documents"])
async def test_missing_initialization_write_ack_never_publishes_or_returns_success(container_name):
    state = CosmosState()
    container = getattr(state, container_name)
    create = container.create_item

    async def empty_ack(body):
        await create(body)
        return {}

    container.create_item = empty_ack
    with pytest.raises((DeletionIntegrityError, DeletionUnavailableError)):
        await state.repo().create_session(private_session())
    assert "PRIVATE" not in repr(state.snapshot())
    assert state.sessions.items[("u1", "s1")]["kind"] == "session_initializing_v1"
    assert [item.sessionId for item in (await state.repo().list_initializations("u1")).items] == ["s1"]


def test_http_losing_initializer_never_returns_201_and_discovery_is_read_only():
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        state = CosmosState()
        repo = state.repo()
        app.state.session_repo = repo

        async def discard_before_publication(item, body):
            if body.get("kind") == "session_v1":
                state.sessions.before_replace = None
                await state.repo().begin_deletion(body["userId"], item)

        state.sessions.before_replace = discard_before_publication
        response = client.post("/api/sessions", json={"title": "PRIVATE TITLE", "systemPrompt": "PRIVATE"})
        assert response.status_code == 404
        assert "PRIVATE" not in repr(state.snapshot())
        assert client.get("/api/sessions").json() == []
        assert len(client.get("/api/sessions/deletions").json()["items"]) == 1


def test_http_initialization_recovery_requires_explicit_owner_discard():
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        owner = client.post("/api/sessions", json={}).json()["userId"]
        state = CosmosState()
        repo = state.repo()
        app.state.session_repo = repo
        record = InitializationRecord(
            id="incomplete", userId=owner, kind="session_initializing_v1",
            deletionEpoch="generation", createdAt=now_utc(),
        )
        state.sessions._put(record.model_dump(mode="json"))
        before = copy.deepcopy(state.snapshot())
        response = client.get("/api/sessions/initializations")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["items"][0]["sessionId"] == "incomplete"
        assert client.get("/api/sessions").json() == []
        assert state.snapshot() == before
        other = {"X-Dev-User": "other-owner"}
        assert client.get("/api/sessions/initializations", headers=other).json()["items"] == []
        assert client.delete("/api/sessions/incomplete", headers=other).status_code == 404
        assert client.get("/api/sessions/initializations?cursor=!").status_code == 400
        assert client.delete("/api/sessions/incomplete").status_code == 202
        assert client.get("/api/sessions/initializations").json()["items"] == []
        response = client.post("/api/sessions/incomplete/deletion/reconcile")
        assert response.status_code == 200 and response.json()["state"] == "cleanup_verified"
