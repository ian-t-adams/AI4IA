"""A consumed v1 fresh-turn slot survives races, lost acknowledgements, and clears."""

from __future__ import annotations

import asyncio
import copy

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.main import create_app
from ai4ia_api.sessions.deletion_models import DeletionUnavailableError
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Document, Message, MessageRole, Session
from ai4ia_api.sessions.repository import SessionNotFoundError
from tests.conftest import make_settings
from tests.cosmos_deletion_fake import CosmosState
from tests.test_chat_memory_api import CapturingGateway


@pytest.fixture(params=["cosmos", "memory"])
def repository(request):
    state = CosmosState() if request.param == "cosmos" else None
    repo = state.repo() if state else InMemorySessionRepository(deletion_enabled=True)
    return repo, state


async def fresh(repo, user="owner"):
    created = await repo.create_session(Session(
        userId=user, title="Synthetic fixture", model="test-model", libraryDocumentIds=[],
    ))
    return await repo.get_session(user, created.id)


async def test_one_winner_and_permanent_marker_in_both_canonical_stores(repository):
    repo, state = repository
    session = await fresh(repo)
    if state:
        arrived = 0
        gate = asyncio.Event()

        async def compete(_item, body):
            nonlocal arrived
            if body.get("freshTurnClaimed"):
                arrived += 1
                if arrived == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), timeout=2)

        state.sessions.before_replace = compete
    results = await asyncio.gather(
        repo.claim_fresh_session("owner", session.model_copy(deep=True)),
        repo.claim_fresh_session("owner", session.model_copy(deep=True)),
    )
    assert sum(result is not None for result in results) == 1
    if state:
        state.sessions.before_replace = None
    saved = await repo.get_session("owner", session.id)
    assert saved.freshTurnClaimed
    assert "freshTurnClaimed" not in saved.model_dump()
    await repo.patch_session("owner", session.id, {"title": "Changed title"})
    await repo.touch_session("owner", session.id)
    await repo.clear_messages("owner", session.id)
    await repo.invalidate_summary("owner", session.id)
    saved = await repo.get_session("owner", session.id)
    assert saved.freshTurnClaimed
    assert await repo.claim_fresh_session("owner", saved) is None
    with pytest.raises(ValueError, match="server-owned"):
        await repo.patch_session("owner", session.id, {"freshTurnClaimed": False})
    if state:
        raw = repo._to_doc(saved)
        assert raw["freshTurnClaimed"] is True
        assert Session.model_validate(raw).freshTurnClaimed is True
        assert "freshTurnClaimed" not in Session.model_validate(raw).model_dump(mode="json")
        # Exercise a full persisted serialization, not only the patch path.
        await state.sessions.upsert_item(raw)
        assert (await state.repo().get_session("owner", session.id)).freshTurnClaimed


async def test_stale_snapshot_wrong_owner_and_closed_session_cannot_claim(repository):
    repo, _ = repository
    session = await fresh(repo)
    with pytest.raises(SessionNotFoundError):
        await repo.claim_fresh_session("other", session)
    await repo.patch_session("owner", session.id, {"systemPrompt": "Changed while preparing"})
    assert await repo.claim_fresh_session("owner", session) is None
    assert not (await repo.get_session("owner", session.id)).freshTurnClaimed
    await repo.begin_deletion("owner", session.id)
    with pytest.raises(SessionNotFoundError):
        await repo.claim_fresh_session("owner", session)


@pytest.mark.parametrize("marker", [False, "false", None, 0, {}, True])
async def test_any_persisted_marker_presence_is_consumed_not_a_reset(marker):
    state = CosmosState()
    repo = state.repo()
    session = await fresh(repo)
    state.sessions.items[("owner", session.id)]["freshTurnClaimed"] = marker
    assert await repo.claim_fresh_session("owner", session) is None
    assert state.sessions.items[("owner", session.id)]["freshTurnClaimed"] == marker


async def test_missing_etag_refuses_and_lost_ack_stays_consumed():
    state = CosmosState()
    repo = state.repo()
    session = await fresh(repo)
    raw = state.sessions.items[("owner", session.id)]
    etag = raw.pop("_etag")
    with pytest.raises(DeletionUnavailableError):
        await repo.claim_fresh_session("owner", session)
    assert "freshTurnClaimed" not in raw
    raw["_etag"] = etag
    original = state.sessions.patch_item

    async def lose_ack(**kwargs):
        result = await original(**kwargs)
        if any(patch["path"] == "/freshTurnClaimed" for patch in kwargs["patch_operations"]):
            raise TimeoutError("lost acknowledgement")
        return result

    state.sessions.patch_item = lose_ack
    with pytest.raises(TimeoutError):
        await repo.claim_fresh_session("owner", session)
    state.sessions.patch_item = original
    assert state.sessions.items[("owner", session.id)]["freshTurnClaimed"] is True
    assert await repo.claim_fresh_session("owner", session) is None
    # Same backend, different genuinely fresh record: the positive claim runs.
    other = await fresh(repo)
    assert await repo.claim_fresh_session("owner", other) is not None


async def test_unversioned_or_disabled_repository_never_adopts_existing_record():
    state = CosmosState()
    legacy = state.repo(enabled=False)
    session = await fresh(legacy)
    before = state.snapshot()
    assert await legacy.claim_fresh_session("owner", session) is None
    assert await state.repo(enabled=True).claim_fresh_session("owner", session) is None
    assert state.snapshot() == before
    local = InMemorySessionRepository()
    ordinary = await fresh(local)
    assert await local.claim_fresh_session("owner", ordinary) is None


def test_marker_is_not_browser_settable_and_clear_does_not_reopen_it():
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"freshTurnClaimed": False}).status_code == 422
        session = client.post("/api/sessions", json={
            "title": "Fixture", "model": "gpt-5.2", "libraryDocumentIds": [],
        }).json()
        assert "freshTurnClaimed" not in session and "deletionProtocol" not in session
        assert client.patch(
            f"/api/sessions/{session['id']}", json={"freshTurnClaimed": False},
        ).status_code == 422
        app.state.gateway = gateway = CapturingGateway()
        body = {
            "sessionId": session["id"], "content": "fixed synthetic input",
            "allowTools": False, "allowAutomaticMemory": False,
            "requireFreshSession": True, "stream": False,
        }
        assert client.post("/api/chat", json=body).status_code == 200
        assert gateway.calls == 1
        assert client.post("/api/chat", json={
            "sessionId": session["id"], "content": "/clear", "stream": False,
        }).status_code == 200
        assert client.post("/api/chat", json=body).status_code == 409
        assert gateway.calls == 1


def test_late_documents_and_history_are_not_loaded_into_the_claimed_prompt(monkeypatch):
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        app.state.gateway = gateway = CapturingGateway()
        session = client.post("/api/sessions", json={
            "title": "Fixture", "model": "gpt-5.2", "libraryDocumentIds": [],
        }).json()
        repo = app.state.session_repo
        original = repo.add_message
        inserted = []

        async def late_write(user_id, message):
            result = await original(user_id, message)
            if message.role == MessageRole.user and not inserted:
                inserted.append(True)
                await repo.add_document(user_id, Document(
                    sessionId=message.sessionId, userId=user_id,
                    filename="late.txt", text="LATE PRIVATE DOCUMENT",
                ))
                await original(user_id, Message(
                    sessionId=message.sessionId, userId=user_id,
                    role=MessageRole.user, content="LATE PRIVATE HISTORY",
                ))
            return result

        monkeypatch.setattr(repo, "add_message", late_write)
        result = client.post("/api/chat", json={
            "sessionId": session["id"], "content": "fixed synthetic input",
            "allowTools": False, "allowAutomaticMemory": False,
            "requireFreshSession": True, "stream": False,
        })
        assert result.status_code == 200, result.text
        assert inserted
        assert copy.deepcopy(gateway.last_messages) == [{"role": "user", "content": "fixed synthetic input"}]
