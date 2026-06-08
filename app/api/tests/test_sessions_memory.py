import pytest

from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, Session
from ai4ia_api.sessions.repository import SessionNotFoundError


async def _seed(repo, user="user-a"):
    session = Session(userId=user, title="t")
    await repo.create_session(session)
    return session


async def test_create_and_get_session():
    repo = InMemorySessionRepository()
    session = await _seed(repo)
    got = await repo.get_session("user-a", session.id)
    assert got.id == session.id


async def test_cross_user_access_is_denied():
    repo = InMemorySessionRepository()
    session = await _seed(repo, user="user-a")
    with pytest.raises(SessionNotFoundError):
        await repo.get_session("user-b", session.id)


async def test_list_sessions_scoped_to_user():
    repo = InMemorySessionRepository()
    await _seed(repo, user="user-a")
    await _seed(repo, user="user-b")
    assert len(await repo.list_sessions("user-a")) == 1


async def test_add_message_requires_owned_session():
    repo = InMemorySessionRepository()
    session = await _seed(repo, user="user-a")
    msg = Message(sessionId=session.id, userId="user-a", role=MessageRole.user, content="hi")
    await repo.add_message("user-a", msg)
    # A different user cannot write into the session's message partition.
    intruder = Message(
        sessionId=session.id, userId="user-b", role=MessageRole.user, content="x"
    )
    with pytest.raises(SessionNotFoundError):
        await repo.add_message("user-b", intruder)


async def test_add_message_forces_user_id():
    repo = InMemorySessionRepository()
    session = await _seed(repo, user="user-a")
    # Spoofed userId on the message is overwritten with the authenticated user.
    msg = Message(sessionId=session.id, userId="spoofed", role=MessageRole.user, content="hi")
    saved = await repo.add_message("user-a", msg)
    assert saved.userId == "user-a"


async def test_upsert_message_updates_in_place():
    repo = InMemorySessionRepository()
    session = await _seed(repo, user="user-a")
    msg = Message(sessionId=session.id, userId="user-a", role=MessageRole.assistant, content="")
    await repo.add_message("user-a", msg)
    msg.content = "done"
    await repo.upsert_message("user-a", msg)
    messages = await repo.list_messages("user-a", session.id)
    assert len(messages) == 1 and messages[0].content == "done"


async def test_delete_session_removes_messages():
    repo = InMemorySessionRepository()
    session = await _seed(repo, user="user-a")
    msg = Message(sessionId=session.id, userId="user-a", role=MessageRole.user, content="hi")
    await repo.add_message("user-a", msg)
    await repo.delete_session("user-a", session.id)
    with pytest.raises(SessionNotFoundError):
        await repo.list_messages("user-a", session.id)
