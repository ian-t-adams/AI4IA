"""Repository document methods + ownership + delete-session cascade.

Exercises the in-memory repository (behaviorally identical to Cosmos) for
add/list/get/delete_document, per-user ownership enforcement, deterministic
ordering, and that deleting a session removes its documents.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Document, Session
from ai4ia_api.sessions.repository import SessionNotFoundError

ALICE = "alice"
BOB = "bob"


async def _session(repo: InMemorySessionRepository, user_id: str = ALICE) -> Session:
    session = Session(userId=user_id, title="Chat")
    return await repo.create_session(session)


def _doc(session_id: str, name: str, *, at: datetime) -> Document:
    return Document(
        sessionId=session_id,
        userId="placeholder",
        filename=name,
        contentType="text/plain",
        size=10,
        charCount=5,
        text="hello",
        createdAt=at,
    )


async def test_add_and_get_document():
    repo = InMemorySessionRepository()
    s = await _session(repo)
    now = datetime.now(timezone.utc)
    doc = await repo.add_document(ALICE, _doc(s.id, "a.txt", at=now))

    assert doc.userId == ALICE  # denormalized owner stamped on write
    fetched = await repo.get_document(ALICE, s.id, doc.id)
    assert fetched is not None
    assert fetched.filename == "a.txt"


async def test_list_documents_oldest_first():
    repo = InMemorySessionRepository()
    s = await _session(repo)
    base = datetime.now(timezone.utc)
    await repo.add_document(ALICE, _doc(s.id, "second.txt", at=base + timedelta(seconds=2)))
    await repo.add_document(ALICE, _doc(s.id, "first.txt", at=base))

    docs = await repo.list_documents(ALICE, s.id)
    assert [d.filename for d in docs] == ["first.txt", "second.txt"]


async def test_get_missing_document_returns_none():
    repo = InMemorySessionRepository()
    s = await _session(repo)
    assert await repo.get_document(ALICE, s.id, "nope") is None


async def test_delete_document_removes_it():
    repo = InMemorySessionRepository()
    s = await _session(repo)
    now = datetime.now(timezone.utc)
    doc = await repo.add_document(ALICE, _doc(s.id, "a.txt", at=now))

    await repo.delete_document(ALICE, s.id, doc.id)
    assert await repo.get_document(ALICE, s.id, doc.id) is None
    assert await repo.list_documents(ALICE, s.id) == []


async def test_documents_are_owner_scoped():
    repo = InMemorySessionRepository()
    s = await _session(repo, ALICE)
    now = datetime.now(timezone.utc)
    await repo.add_document(ALICE, _doc(s.id, "a.txt", at=now))

    # Bob cannot see or touch Alice's session documents.
    with pytest.raises(SessionNotFoundError):
        await repo.list_documents(BOB, s.id)
    with pytest.raises(SessionNotFoundError):
        await repo.add_document(BOB, _doc(s.id, "evil.txt", at=now))


async def test_delete_session_cascades_documents():
    repo = InMemorySessionRepository()
    s = await _session(repo)
    now = datetime.now(timezone.utc)
    await repo.add_document(ALICE, _doc(s.id, "a.txt", at=now))
    await repo.add_document(ALICE, _doc(s.id, "b.txt", at=now))

    await repo.delete_session(ALICE, s.id)

    # The session is gone, so any document access now fails ownership.
    with pytest.raises(SessionNotFoundError):
        await repo.list_documents(ALICE, s.id)
    # Recreating a session with the same id surfaces no stale documents.
    again = Session(id=s.id, userId=ALICE, title="Chat")
    await repo.create_session(again)
    assert await repo.list_documents(ALICE, s.id) == []
