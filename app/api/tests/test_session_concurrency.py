import asyncio
from pathlib import Path

import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceNotFoundError,
)

from ai4ia_api.agents.agent_catalog import load_agent_catalog
from ai4ia_api.agents.command_service import execute_command
from ai4ia_api.agents.commands import parse_input
from ai4ia_api.agents.summarization import SummarizationService
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.catalog import load_catalog
from ai4ia_api.sessions.cosmos_repo import CosmosSessionRepository
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Document, Message, MessageRole, Session, ToolOverrides
from ai4ia_api.sessions.repository import SessionConflictError, SessionNotFoundError


async def test_model_and_document_updates_survive_concurrency():
    repo = InMemorySessionRepository()
    session = await repo.create_session(
        Session(userId="u1", model="old", libraryDocumentIds=[])
    )
    await asyncio.gather(
        repo.patch_session("u1", session.id, {"model": "new"}),
        repo.mutate_library_document_ids(
            "u1", session.id, "doc-a", add=True
        ),
    )
    final = await repo.get_session("u1", session.id)
    assert final.model == "new"
    assert final.libraryDocumentIds == ["doc-a"]


async def test_tools_and_document_removal_survive_concurrency():
    repo = InMemorySessionRepository()
    session = await repo.create_session(
        Session(userId="u1", libraryDocumentIds=["doc-a", "doc-b"])
    )
    await asyncio.gather(
        repo.patch_session(
            "u1",
            session.id,
            {"toolOverrides": ToolOverrides(added=["calculator"], removed=[])},
        ),
        repo.mutate_library_document_ids(
            "u1", session.id, "doc-a", add=False
        ),
    )
    final = await repo.get_session("u1", session.id)
    assert final.toolOverrides.added == ["calculator"]
    assert final.libraryDocumentIds == ["doc-b"]


async def test_mapping_tool_patch_normalizes_at_repository_boundary():
    repo = InMemorySessionRepository()
    session = await repo.create_session(Session(userId="u1"))
    saved = await repo.patch_session(
        "u1",
        session.id,
        {
            "toolOverrides": {
                "added": [" calculator ", "calculator"],
                "removed": [],
            }
        },
    )
    assert isinstance(saved.toolOverrides, ToolOverrides)
    assert saved.toolOverrides.added == ["calculator"]

    with pytest.raises(ValueError, match="both added and removed"):
        await repo.patch_session(
            "u1",
            session.id,
            {
                "toolOverrides": {
                    "added": ["calculator"],
                    "removed": ["calculator"],
                }
            },
        )


async def test_manual_title_wins_concurrent_generated_title():
    repo = InMemorySessionRepository()
    session = await repo.create_session(Session(userId="u1"))
    generated, renamed = await asyncio.gather(
        repo.set_generated_title_if_eligible("u1", session.id, "Generated"),
        repo.patch_session("u1", session.id, {"title": "Manual"}),
    )
    final = await repo.get_session("u1", session.id)
    assert generated in (True, False)
    assert renamed.title == "Manual"
    assert final.title == "Manual"
    assert final.titleSource == "manual"


async def test_two_document_associations_and_removals_are_atomic():
    repo = InMemorySessionRepository()
    session = await repo.create_session(
        Session(userId="u1", libraryDocumentIds=[])
    )
    await asyncio.gather(
        repo.mutate_library_document_ids("u1", session.id, "doc-a", add=True),
        repo.mutate_library_document_ids("u1", session.id, "doc-b", add=True),
    )
    assert set(
        (await repo.get_session("u1", session.id)).libraryDocumentIds or []
    ) == {"doc-a", "doc-b"}
    await asyncio.gather(
        repo.mutate_library_document_ids("u1", session.id, "doc-a", add=False),
        repo.mutate_library_document_ids("u1", session.id, "doc-b", add=False),
    )
    assert (await repo.get_session("u1", session.id)).libraryDocumentIds == []


class _FakeSessions:
    def __init__(self, session: Session) -> None:
        self.item = {**session.model_dump(mode="json"), "_etag": "e1"}
        self.conflicts = 1
        self.etags: list[str | None] = []

    async def read_item(self, *, item, partition_key):
        return dict(self.item)

    async def patch_item(
        self,
        *,
        item,
        partition_key,
        patch_operations,
        etag=None,
        match_condition=None,
    ):
        self.etags.append(etag)
        if etag is not None and self.conflicts:
            self.conflicts -= 1
            self.item["libraryDocumentIds"] = ["doc-a"]
            self.item["_etag"] = "e2"
            raise CosmosAccessConditionFailedError(message="etag")
        for operation in patch_operations:
            self.item[operation["path"].lstrip("/")] = operation["value"]
        self.item["_etag"] = "e3"
        return dict(self.item)


async def test_cosmos_document_list_cas_retries_and_merges():
    session = Session(userId="u1", libraryDocumentIds=[])
    repo = object.__new__(CosmosSessionRepository)
    fake = _FakeSessions(session)
    repo._sessions = fake
    saved = await repo.mutate_library_document_ids(
        "u1", session.id, "doc-b", add=True
    )
    assert fake.etags == ["e1", "e2"]
    assert saved.libraryDocumentIds == ["doc-a", "doc-b"]


class _SummarySessions:
    def __init__(self, session: Session) -> None:
        self.item = {**session.model_dump(mode="json"), "_etag": "s1"}
        self.conflict_once = False
        self.etags: list[str | None] = []

    async def read_item(self, *, item, partition_key):
        return dict(self.item)

    async def patch_item(
        self,
        *,
        item,
        partition_key,
        patch_operations,
        etag=None,
        match_condition=None,
    ):
        self.etags.append(etag)
        if self.conflict_once:
            self.conflict_once = False
            self.item["model"] = "concurrent-model"
            self.item["_etag"] = "s2"
            raise CosmosAccessConditionFailedError(message="etag")
        for operation in patch_operations:
            self.item[operation["path"].lstrip("/")] = operation["value"]
        self.item["_etag"] = "s3"
        return dict(self.item)


class _TitleRaceSessions(_SummarySessions):
    async def patch_item(
        self,
        *,
        item,
        partition_key,
        patch_operations,
        etag=None,
        match_condition=None,
    ):
        self.etags.append(etag)
        if self.conflict_once:
            self.conflict_once = False
            self.item["title"] = "Manual"
            self.item["titleSource"] = "manual"
            self.item["_etag"] = "manual-etag"
            raise CosmosAccessConditionFailedError(message="etag")
        for operation in patch_operations:
            self.item[operation["path"].lstrip("/")] = operation["value"]
        return dict(self.item)


async def test_cosmos_generated_title_stops_after_manual_race():
    session = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    fake = _TitleRaceSessions(session)
    fake.conflict_once = True
    repo._sessions = fake

    updated = await repo.set_generated_title_if_eligible(
        "u1", session.id, "Generated"
    )
    assert updated is False
    assert fake.item["title"] == "Manual"
    assert fake.item["titleSource"] == "manual"
    assert fake.etags == ["s1"]


class _SummaryMessages:
    def __init__(self, sessions: _SummarySessions) -> None:
        self.sessions = sessions
        self.items: dict[str, dict] = {}
        self.advance_after_create = False

    async def create_item(self, body):
        self.items[body["id"]] = dict(body)
        if self.advance_after_create:
            self.advance_after_create = False
            self.sessions.item["summaryVersion"] += 1
            self.sessions.item["_etag"] = "s-race"
            self.items.pop(body["id"], None)
        return body

    async def delete_item(self, *, item, partition_key):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        if item not in self.items:
            raise CosmosResourceNotFoundError(message="already deleted")
        self.items.pop(item)

    async def query_items(self, *, query, parameters=None, partition_key=None):
        version = next(
            parameter["value"]
            for parameter in parameters or []
            if parameter["name"] == "@version"
        )
        for item in list(self.items.values()):
            if item.get("summaryVersion") is not None and item["summaryVersion"] < version:
                yield {"id": item["id"]}


async def test_cosmos_summary_version_is_backward_compatible_and_conditional():
    session = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    fake = _SummarySessions(session)
    fake.item.pop("summaryVersion")
    repo._sessions = fake
    repo._messages = _SummaryMessages(fake)

    cleared = await repo.invalidate_summary("u1", session.id)
    assert cleared.summaryVersion == 1
    fake.conflict_once = True
    committed = await repo.commit_summary_if_version(
        "u1",
        session.id,
        expected_version=1,
        summary="summary",
        summarized_through_message_id="m1",
    )
    assert committed is not None
    assert committed.summary == "summary"
    assert committed.summaryVersion == 2
    assert committed.model == "concurrent-model"
    assert fake.etags == ["s1", "s3", "s2"]
    stale = await repo.commit_summary_if_version(
        "u1",
        session.id,
        expected_version=1,
        summary="stale",
        summarized_through_message_id="m0",
    )
    assert stale is None
    assert (await repo.get_session("u1", session.id)).summary == "summary"


async def test_cosmos_summary_reply_rechecks_and_compensates_cross_container_race():
    session = Session(userId="u1", summary="summary", summaryVersion=1)
    repo = object.__new__(CosmosSessionRepository)
    sessions = _SummarySessions(session)
    messages = _SummaryMessages(sessions)
    repo._sessions = sessions
    repo._messages = messages
    raced = Message(
        sessionId=session.id,
        userId="u1",
        role=MessageRole.assistant,
        content="stale",
    )
    messages.advance_after_create = True
    assert (
        await repo.add_message_if_summary_version(
            "u1", raced, expected_version=1
        )
        is False
    )
    assert messages.items == {}

    current = Message(
        sessionId=session.id,
        userId="u1",
        role=MessageRole.assistant,
        content="current",
    )
    assert await repo.add_message_if_summary_version(
        "u1", current, expected_version=2
    )
    assert current.id in messages.items
    committed = await repo.commit_summary_if_version(
        "u1",
        session.id,
        expected_version=2,
        summary="newer",
        summarized_through_message_id="m2",
    )
    assert committed is not None and committed.summaryVersion == 3
    assert messages.items == {}


async def test_stale_command_writer_preserves_model_tools_and_documents():
    repo = InMemorySessionRepository()
    created = await repo.create_session(
        Session(userId="u1", model="old", libraryDocumentIds=[])
    )
    stale = await repo.get_session("u1", created.id)
    await repo.patch_session(
        "u1",
        created.id,
        {
            "model": "gpt-5.2",
            "toolOverrides": ToolOverrides(added=["calculator"], removed=[]),
        },
    )
    await repo.mutate_library_document_ids(
        "u1", created.id, "doc-a", add=True
    )
    await execute_command(
        parsed=parse_input("/system concise"),
        session=stale,
        user=AuthenticatedUser(
            internal_user_id="u1", subject="sub", issuer="iss", provider="dev"
        ),
        repo=repo,
        catalog=load_catalog(),
        agents=load_agent_catalog(),
    )
    final = await repo.get_session("u1", created.id)
    assert final.systemPrompt == "concise"
    assert final.model == "gpt-5.2"
    assert final.toolOverrides.added == ["calculator"]
    assert final.libraryDocumentIds == ["doc-a"]


class _SummaryGateway:
    async def complete(self, **_kwargs):
        return {"choices": [{"message": {"content": "summary"}}]}


async def test_stale_summarizer_preserves_workspace_policy():
    repo = InMemorySessionRepository()
    created = await repo.create_session(
        Session(userId="u1", model="old", libraryDocumentIds=[])
    )
    stale = await repo.get_session("u1", created.id)
    prior = [
        Message(sessionId=created.id, userId="u1", role=MessageRole.user, content="a" * 40),
        Message(
            sessionId=created.id,
            userId="u1",
            role=MessageRole.assistant,
            content="b" * 40,
        ),
    ]
    await repo.patch_session(
        "u1",
        created.id,
        {
            "model": "gpt-5.2",
            "toolOverrides": ToolOverrides(added=["calculator"], removed=[]),
        },
    )
    await repo.mutate_library_document_ids(
        "u1", created.id, "doc-a", add=True
    )
    service = SummarizationService(
        enabled=True,
        recent_turns=1,
        threshold_ratio=0.1,
        fallback_threshold_chars=1,
    )
    await service.apply(
        gateway=_SummaryGateway(),
        repo=repo,
        session=stale,
        user_id="u1",
        deployment="dep",
        prior=prior,
        system_prompt=None,
        context_window=None,
    )
    final = await repo.get_session("u1", created.id)
    assert final.summary == "summary"
    assert final.model == "gpt-5.2"
    assert final.toolOverrides.added == ["calculator"]
    assert final.libraryDocumentIds == ["doc-a"]


async def test_unversioned_full_replacement_is_rejected():
    repo = InMemorySessionRepository()
    created = await repo.create_session(Session(userId="u1"))
    with pytest.raises(SessionConflictError):
        await repo.update_session(created)


def test_application_has_no_unversioned_full_session_writers():
    source = Path(__file__).resolve().parents[1] / "src" / "ai4ia_api"
    offenders = []
    for path in source.rglob("*.py"):
        if path.parts[-2] == "sessions":
            continue
        if ".update_session(" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(source)))
    assert offenders == []


class _ConcurrentlyDeletedSessions:
    """Fake ``sessions`` container simulating a delete that lands in the gap
    between a method's existence check and its own write.

    ``read_item`` still succeeds (the check-before-write step), but every
    ``patch_item``/``delete_item`` call raises ``CosmosResourceNotFoundError``,
    mimicking another concurrent request deleting the session first.
    """

    def __init__(self, session: Session) -> None:
        self.item = {**session.model_dump(mode="json"), "_etag": "e1"}

    async def read_item(self, *, item, partition_key):
        return dict(self.item)

    async def patch_item(
        self,
        *,
        item,
        partition_key,
        patch_operations,
        etag=None,
        match_condition=None,
    ):
        raise CosmosResourceNotFoundError(message="deleted concurrently")

    async def delete_item(self, *, item, partition_key):
        raise CosmosResourceNotFoundError(message="deleted concurrently")


class _EmptyQueryContainer:
    """Fake ``messages``/``documents`` container with no rows, so
    ``delete_session``'s cascade loops are no-ops before it reaches the
    final (concurrently-raced) ``sessions.delete_item`` call."""

    async def query_items(self, *, query, parameters=None, partition_key=None):
        return
        yield  # pragma: no cover - makes this an async generator


@pytest.mark.parametrize(
    "method_name, call_kwargs",
    [
        ("patch_session", {"changes": {"model": "gpt-5.2"}}),
        ("touch_session", {}),
        (
            "invalidate_summary",
            {},
        ),
    ],
)
async def test_cosmos_patch_paths_translate_concurrent_delete_to_not_found(
    method_name, call_kwargs
):
    session = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _ConcurrentlyDeletedSessions(session)
    method = getattr(repo, method_name)
    with pytest.raises(SessionNotFoundError):
        await method("u1", session.id, **call_kwargs)


async def test_cosmos_mutate_library_document_ids_translates_concurrent_delete():
    session = Session(userId="u1", libraryDocumentIds=[])
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _ConcurrentlyDeletedSessions(session)
    with pytest.raises(SessionNotFoundError):
        await repo.mutate_library_document_ids("u1", session.id, "doc-a", add=True)


async def test_cosmos_generated_title_translates_concurrent_delete_to_not_found():
    session = Session(userId="u1")  # title="New chat", titleSource="auto" (eligible)
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _ConcurrentlyDeletedSessions(session)
    with pytest.raises(SessionNotFoundError):
        await repo.set_generated_title_if_eligible("u1", session.id, "Generated")


async def test_cosmos_delete_session_translates_concurrent_delete_to_not_found():
    session = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _ConcurrentlyDeletedSessions(session)
    repo._messages = _EmptyQueryContainer()
    repo._documents = _EmptyQueryContainer()
    with pytest.raises(SessionNotFoundError):
        await repo.delete_session("u1", session.id)


class _SucceedingSessions:
    """Fake ``sessions`` container where reads, the tombstone CAS ``patch``,
    and the final delete all succeed normally -- isolates the child-container
    race (below) from the session's own concurrent-delete race, which is
    already covered by ``_ConcurrentlyDeletedSessions`` above.

    ``deleting_at`` optionally pre-seeds an existing tombstone timestamp (a
    session already marked deleting by an earlier call) so tests can prove
    the grace-period-elapsed finalize path without sleeping: a far-past
    value simulates "this session was tombstoned long ago", letting
    ``delete_session`` hard-delete on this same call."""

    def __init__(self, session: Session, *, deleting_at: str | None = None) -> None:
        self.item = {**session.model_dump(mode="json"), "_etag": "e1"}
        if deleting_at is not None:
            self.item["deletingAt"] = deleting_at
        self.deleted = False

    async def read_item(self, *, item, partition_key):
        return dict(self.item)

    async def patch_item(
        self, *, item, partition_key, patch_operations, etag=None, match_condition=None
    ):
        for op in patch_operations:
            self.item[op["path"].lstrip("/")] = op["value"]
        self.item["_etag"] = "e2"

    async def delete_item(self, *, item, partition_key):
        self.deleted = True


class _RacedChildContainer:
    """Fake ``messages``/``documents`` container whose listed rows are each
    already gone by the time this container's own ``delete_item`` runs --
    every delete raises ``CosmosResourceNotFoundError``, exactly as Cosmos
    does for a real concurrent double-delete (e.g. a duplicate request, or a
    racing single-item delete) landing in the query-then-delete gap. Deleted
    (albeit 404ing) ids stop being returned by later queries, matching a real
    container so delete_session's bounded repeated sweep converges instead of
    re-attempting the same already-gone rows every pass."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = list(ids)
        self.delete_attempts: list[str] = []

    async def query_items(self, *, query, parameters=None, partition_key=None):
        for item_id in list(self._ids):
            yield {"id": item_id}

    async def delete_item(self, *, item, partition_key):
        self.delete_attempts.append(item)
        if item in self._ids:
            self._ids.remove(item)
        raise CosmosResourceNotFoundError(message="deleted concurrently")


async def test_cosmos_delete_session_tolerates_concurrently_deleted_children():
    """Production-shape regression: delete_session's cascade previously let a
    raw CosmosResourceNotFoundError from the *messages* or *documents*
    container escape uncaught when a concurrent request/cascade removed a
    child row between this call's own query and its delete_item. That 404 is
    the cascade's already-achieved goal state for that row, so it must be
    swallowed and the cascade must continue -- and the still-existing session
    must still be deleted, not misreported as SessionNotFoundError.

    Pre-seeds an already-old tombstone so this call's grace period has
    elapsed and hard-delete happens immediately -- isolating the
    concurrent-children-tolerance behavior under test from the separate
    grace-period-timing behavior covered by the tests below."""
    session = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _SucceedingSessions(session, deleting_at="2020-01-01T00:00:00Z")
    messages = _RacedChildContainer(["m1", "m2"])
    documents = _RacedChildContainer(["d1"])
    repo._messages = messages
    repo._documents = documents

    await repo.delete_session("u1", session.id)  # must not raise

    assert messages.delete_attempts == ["m1", "m2"]
    assert documents.delete_attempts == ["d1"]
    assert repo._sessions.deleted is True


async def test_cosmos_clear_messages_tolerates_concurrently_deleted_messages():
    """Same idempotent-child-404 reasoning as delete_session's cascade,
    applied to the standalone clear-messages path (identical query-then-
    delete-per-row shape, same race)."""
    session = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _SucceedingSessions(session)
    messages = _RacedChildContainer(["m1"])
    repo._messages = messages

    await repo.clear_messages("u1", session.id)  # must not raise

    assert messages.delete_attempts == ["m1"]


async def test_cosmos_add_message_rejected_once_session_tombstoned_before_hard_delete():
    """HIGH production-shape finding: a hard-delete-parent-first fence has no
    durability -- if delete_session is interrupted after removing the parent
    but before finishing the cascade, a retry can never resume (ownership
    fails against an already-gone parent), and there is no fence at all
    between the tombstone-equivalent moment and the parent's actual removal.
    The fix CASes a durable ``deletingAt`` tombstone before any child
    cleanup starts. This proves the fence is durable, not just a post-write
    safety net: add_message's *pre-write* ownership check must already
    reject a tombstoned-but-not-yet-hard-deleted session, so the write is
    never even attempted."""
    session = Session(userId="u1")
    tombstoned = {
        **session.model_dump(mode="json"),
        "_etag": "e1",
        "deletingAt": "2024-01-01T00:00:00Z",
    }

    class _TombstonedSessions:
        async def read_item(self, *, item, partition_key):
            return dict(tombstoned)

    class _RecordingMessages:
        def __init__(self) -> None:
            self.items: dict[str, dict] = {}

        async def create_item(self, body):
            self.items[body["id"]] = body

    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _TombstonedSessions()
    messages = _RecordingMessages()
    repo._messages = messages

    message = Message(sessionId=session.id, userId="u1", role=MessageRole.user, content="hi")
    with pytest.raises(SessionNotFoundError):
        await repo.add_message("u1", message)

    assert messages.items == {}  # the write must never even be attempted


async def test_cosmos_add_message_pre_write_check_races_tombstone_visibility():
    """Cross-replica/stale-read shape: add_message's *pre-write* ownership
    check can observe a stale replica that hasn't yet caught up to
    delete_session's tombstone CAS, letting the write proceed even though
    the session is already (durably) being deleted elsewhere. The
    post-write recheck must still catch this once it observes the tombstone
    (a later, consistent read) and self-compensate -- proving the fence
    holds even when the pre-write check itself is raced by eventual-
    consistency staleness, not only by literal deletion order."""
    session = Session(userId="u1")

    class _EventuallyConsistentSessions:
        """First read (the pre-write check) returns a stale, not-yet-
        tombstoned view; every read after that reflects the tombstone --
        the minimal shape of a single stale-replica read followed by full
        consistency."""

        def __init__(self, session: Session) -> None:
            self.stale_item = {**session.model_dump(mode="json"), "_etag": "e1"}
            self.fresh_item = {**self.stale_item, "deletingAt": "2024-01-01T00:00:00Z"}
            self.reads = 0

        async def read_item(self, *, item, partition_key):
            self.reads += 1
            return dict(self.stale_item) if self.reads == 1 else dict(self.fresh_item)

    class _RecordingMessages:
        def __init__(self) -> None:
            self.items: dict[str, dict] = {}
            self.delete_attempts: list[str] = []

        async def create_item(self, body):
            self.items[body["id"]] = body

        async def delete_item(self, *, item, partition_key):
            self.delete_attempts.append(item)
            if item not in self.items:
                raise CosmosResourceNotFoundError(message="not found")
            del self.items[item]

    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _EventuallyConsistentSessions(session)
    messages = _RecordingMessages()
    repo._messages = messages

    message = Message(sessionId=session.id, userId="u1", role=MessageRole.user, content="hi")
    with pytest.raises(SessionNotFoundError):
        await repo.add_message("u1", message)

    assert message.id not in messages.items
    assert messages.delete_attempts == [message.id]


async def test_cosmos_delete_session_resumes_after_interrupted_sweep():
    """HIGH production-shape finding: a hard-delete-parent-first design has
    no resumability -- once a genuine (non-404) error interrupts the
    children sweep, the parent is already gone, so a retry of delete_session
    immediately fails SessionNotFoundError and can never finish the cascade,
    leaving orphans behind forever. The tombstone fence fixes this: the
    parent row is left untouched (only tombstoned) until the sweep finishes,
    so an interrupted call's retry re-reads the still-present, already-
    tombstoned session -- ownership still checkable, no re-CAS needed -- and
    resumes the sweep to completion without changing the partition key or
    ownership model."""
    session = Session(userId="u1")

    class _ToggleFailSessions:
        def __init__(self, session: Session) -> None:
            self.item = {**session.model_dump(mode="json"), "_etag": "e1"}
            self.deleted = False

        async def read_item(self, *, item, partition_key):
            if self.deleted:
                raise CosmosResourceNotFoundError(message="deleted concurrently")
            return dict(self.item)

        async def patch_item(
            self, *, item, partition_key, patch_operations, etag=None, match_condition=None
        ):
            for op in patch_operations:
                self.item[op["path"].lstrip("/")] = op["value"]
            self.item["_etag"] = "e2"

        async def delete_item(self, *, item, partition_key):
            self.deleted = True

    class _FlakyMessages:
        """Fails the first delete attempt with a genuine transient error
        (a dropped connection/timeout, not a 404 -- the "interrupted sweep"
        shape), then behaves normally on any later call."""

        def __init__(self, ids: list[str]) -> None:
            self.items = {item_id: {"id": item_id} for item_id in ids}
            self.fail_next = True

        async def query_items(self, *, query, parameters=None, partition_key=None):
            for item_id in list(self.items.keys()):
                yield {"id": item_id}

        async def delete_item(self, *, item, partition_key):
            if self.fail_next:
                self.fail_next = False
                raise TimeoutError("transient network failure")
            self.items.pop(item, None)

    class _EmptyDocuments:
        async def query_items(self, *, query, parameters=None, partition_key=None):
            return
            yield  # pragma: no cover - makes this an async generator

    sessions_fake = _ToggleFailSessions(session)
    messages = _FlakyMessages(["m1"])
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = sessions_fake
    repo._messages = messages
    repo._documents = _EmptyDocuments()

    with pytest.raises(TimeoutError):
        await repo.delete_session("u1", session.id)

    # The session must still exist (tombstoned, not hard-deleted) so a retry
    # can resume -- unlike the old parent-deleted-first design, where this
    # would now be gone and any retry would fail SessionNotFoundError.
    assert sessions_fake.deleted is False
    assert sessions_fake.item.get("deletingAt") is not None

    # Backdate the tombstone to simulate a retry happening well after the
    # grace period (e.g. a client retry, or a later ops/reconciliation pass)
    # rather than an immediate one -- isolating the resumability behavior
    # under test here from the separate grace-period-timing behavior covered
    # by the tests below.
    sessions_fake.item["deletingAt"] = "2020-01-01T00:00:00Z"

    await repo.delete_session("u1", session.id)  # retry resumes and completes

    assert sessions_fake.deleted is True
    assert messages.items == {}


async def test_cosmos_delete_session_sweep_catches_child_appearing_on_later_pass():
    """Idempotent/retriable-sweep requirement: a child row that only becomes
    visible to the query on a later pass (e.g. replication lag, or a
    child-writer's write landing between this call's first and second
    query) must still be caught within the same delete_session call, not
    left behind because the sweep only looked once."""
    session = Session(userId="u1")

    class _StragglerMessages:
        """``m1`` isn't visible to query_items until the second pass --
        simulating a child whose write is still landing/propagating when
        the sweep's first pass runs."""

        def __init__(self) -> None:
            self.visible_ids: list[str] = []
            self.delete_attempts: list[str] = []
            self._queries = 0

        async def query_items(self, *, query, parameters=None, partition_key=None):
            self._queries += 1
            if self._queries == 2:
                self.visible_ids = ["m1"]
            for item_id in list(self.visible_ids):
                yield {"id": item_id}

        async def delete_item(self, *, item, partition_key):
            self.delete_attempts.append(item)
            self.visible_ids.remove(item)

    class _EmptyDocuments:
        async def query_items(self, *, query, parameters=None, partition_key=None):
            return
            yield  # pragma: no cover - makes this an async generator

    session_fake = _SucceedingSessions(session, deleting_at="2020-01-01T00:00:00Z")
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = session_fake
    messages = _StragglerMessages()
    repo._messages = messages
    repo._documents = _EmptyDocuments()

    await repo.delete_session("u1", session.id)

    assert messages.delete_attempts == ["m1"]
    assert session_fake.deleted is True


async def test_cosmos_delete_session_defers_hard_delete_within_grace_period():
    """HIGH-1 (round 6) regression: a fresh tombstone must not be treated as
    license to hard-delete the parent on the very same call -- a stale
    replica elsewhere could still let a child write land after this call's
    sweep already ran and found nothing. The session must stay tombstoned
    (already invisible to every caller via ``_owned_session``, so the DELETE
    endpoint's contract is unaffected) until a later call -- a retry,
    duplicate request, or future reconciliation pass -- finds the grace
    period has elapsed since the *original* tombstone timestamp."""
    session = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    session_fake = _SucceedingSessions(session)  # fresh tombstone, no pre-seed
    repo._sessions = session_fake
    repo._messages = _EmptyQueryContainer()
    repo._documents = _EmptyQueryContainer()

    await repo.delete_session("u1", session.id)

    assert session_fake.deleted is False
    assert session_fake.item.get("deletingAt") is not None

    # Backdate the stored tombstone to simulate the grace period elapsing
    # (e.g. a client retry, duplicate request, or a later ops/reconciliation
    # pass), then a follow-up call finalizes.
    session_fake.item["deletingAt"] = "2020-01-01T00:00:00Z"
    await repo.delete_session("u1", session.id)

    assert session_fake.deleted is True


class _StaleThenConvergingMessages:
    """Simulates a replica that is arbitrarily stale for an entire call's
    3-pass sweep (every pass sees no children at all -- e.g. the child's
    write is still replicating/propagating), then later "converges" once
    ``converged`` is flipped, exposing the real row. Models the coordinator's
    explicit "arbitrarily stale independent reads" case: a reader that never
    observes a child during one call's whole duration, not just a single
    query."""

    def __init__(self, real_ids: list[str]) -> None:
        self._real_ids = list(real_ids)
        self.converged = False
        self.delete_attempts: list[str] = []

    async def query_items(self, *, query, parameters=None, partition_key=None):
        if not self.converged:
            return
            yield  # pragma: no cover - makes this an async generator
        for item_id in list(self._real_ids):
            yield {"id": item_id}

    async def delete_item(self, *, item, partition_key):
        self.delete_attempts.append(item)
        if item in self._real_ids:
            self._real_ids.remove(item)


async def test_cosmos_delete_session_grace_period_prevents_orphan_from_stale_sweep():
    """HIGH-1 (round 6) regression, the coordinator's explicit "arbitrarily
    stale independent reads" case: a replica that is stale for this call's
    *entire* 3-pass sweep (every pass sees no children -- not just the first
    of several) must not cause the parent to be hard-deleted, because that
    would permanently orphan the child the instant it does land -- nothing
    else ever re-sweeps a session id once its parent row is gone. The grace
    period defers finalization to a later call, whose fresh read/sweep
    catches the straggler before the parent is ever removed. This is the
    exact gap the fixed, no-delay 3-pass sweep alone (round 5) left open:
    that design would have hard-deleted the parent at the end of this same
    first call, orphaning ``m1`` forever the moment it later landed."""
    session = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    session_fake = _SucceedingSessions(session)  # fresh tombstone
    repo._sessions = session_fake
    stale_then_converging = _StaleThenConvergingMessages(["m1"])
    repo._messages = stale_then_converging
    repo._documents = _EmptyQueryContainer()

    # First call: every one of the 3 sweep passes sees an empty (stale)
    # container -- the child is invisible for this call's entire duration.
    await repo.delete_session("u1", session.id)

    assert session_fake.deleted is False  # not hard-deleted despite a "clean" sweep
    assert stale_then_converging.delete_attempts == []
    assert "m1" in stale_then_converging._real_ids  # child untouched, still real

    # Convergence: this reader now sees the real row, and the grace period
    # has elapsed (simulating a retry/duplicate/reconciliation call later).
    stale_then_converging.converged = True
    session_fake.item["deletingAt"] = "2020-01-01T00:00:00Z"

    await repo.delete_session("u1", session.id)

    assert stale_then_converging.delete_attempts == ["m1"]
    assert session_fake.deleted is True  # only now, after catching the straggler


class _ListSessionsContainer:
    """Fake ``sessions`` container for ``list_sessions``. Only excludes
    tombstoned rows when the *actual* query text asks for it (checking for
    the ``deletingAt`` clause, not just replicating the intended filter
    unconditionally) so this test is sensitive to regressions that remove
    the WHERE clause from ``list_sessions`` itself."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    async def query_items(self, *, query, parameters=None, partition_key=None):
        user_id = next(
            (p["value"] for p in (parameters or []) if p["name"] == "@uid"), None
        )
        excludes_tombstoned = "deletingAt" in query
        for item in self._items:
            if item.get("userId") != user_id:
                continue
            if excludes_tombstoned and item.get("deletingAt") is not None:
                continue
            yield item


async def test_cosmos_list_sessions_excludes_tombstoned_sessions():
    """HIGH-1 (round 6) hardening: the grace period intentionally leaves a
    tombstoned session's row in place for up to ``_DELETE_GRACE_PERIOD``
    before the parent is hard-deleted. Without a matching filter here, a
    session the caller just "deleted" would reappear in ``GET /api/sessions``
    (which returns ``list_sessions`` results unfiltered) until the grace
    period elapses -- a user-visible regression the fixed, no-delay round-5
    design never had, since that design almost always hard-deleted within the
    same call."""
    active = Session(userId="u1")
    tombstoned = Session(userId="u1")
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _ListSessionsContainer(
        [
            active.model_dump(mode="json"),
            {
                **tombstoned.model_dump(mode="json"),
                "deletingAt": "2020-01-01T00:00:00Z",
            },
        ]
    )

    sessions = await repo.list_sessions("u1")

    ids = {s.id for s in sessions}
    assert active.id in ids
    assert tombstoned.id not in ids


class _LegacyDocSessions:
    """Cosmos container stand-in serving one already-stored document whose
    ``toolOverrides`` predates/violates the current schema (e.g. written by
    an older code path, or hand-edited)."""

    def __init__(self, item: dict) -> None:
        self.item = item

    async def read_item(self, *, item, partition_key):
        return dict(self.item)

    async def patch_item(
        self, *, item, partition_key, patch_operations, etag=None, match_condition=None
    ):
        for operation in patch_operations:
            self.item[operation["path"].lstrip("/")] = operation["value"]
        self.item["_etag"] = "e2"
        return dict(self.item)


@pytest.mark.parametrize(
    "malformed",
    [None, [], "corrupt", {"add": ["x"]}, {"added": "not-a-list"}],
)
def test_session_model_tolerates_malformed_persisted_tool_overrides(malformed):
    """Regression for a production 500 on PATCH /api/sessions/<id> traced to
    ``sessions.py``'s ``update_session`` -> ``_validate_policy_fields`` reading
    ``session.toolOverrides`` as its unset-field fallback. The root cause was
    upstream: ``Session.model_validate`` raised ``ValidationError`` (not
    caught anywhere in the read path) whenever a persisted document's
    ``toolOverrides`` didn't match the current schema, making every future
    read *and* patch of that one session a permanent 500. A malformed value
    must be tolerated as "no overrides" instead of failing the whole read.
    """
    session = Session(userId="u1", title="Legacy chat")
    doc = {**session.model_dump(mode="json"), "toolOverrides": malformed}
    restored = Session.model_validate(doc)
    assert restored.toolOverrides == ToolOverrides(added=[], removed=[])


async def test_cosmos_get_and_patch_session_survive_corrupted_tool_overrides():
    """End-to-end version of the above through the actual Cosmos repository:
    both ``get_session`` (GET) and ``patch_session`` (PATCH) must succeed
    against a legacy/corrupted document rather than 500ing, and the field
    self-heals to empty overrides on the next write."""
    session = Session(userId="u1", title="Legacy chat")
    doc = {
        **session.model_dump(mode="json"),
        "toolOverrides": None,
        "_etag": "e1",
    }
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _LegacyDocSessions(doc)

    fetched = await repo.get_session("u1", session.id)
    assert fetched.toolOverrides == ToolOverrides(added=[], removed=[])

    patched = await repo.patch_session("u1", session.id, {"title": "Renamed"})
    assert patched.title == "Renamed"
    assert patched.toolOverrides == ToolOverrides(added=[], removed=[])


class _SessionsDeletedOnce:
    """Fake ``sessions`` container whose ``read_item`` succeeds until
    ``delete_item`` is called, after which every read raises 404 -- i.e., a
    consistent "does the session currently exist" view across multiple
    reads, unlike the simpler check-then-write fakes above which only model
    a single race shape. ``patch_item`` supports delete_session's tombstone
    CAS step (setting ``deletingAt`` without removing the item).

    ``deleting_at`` optionally pre-seeds an existing tombstone timestamp, same
    as ``_SucceedingSessions`` above, so a test can prove the grace period
    has already elapsed and isolate a different race from HIGH-1's
    grace-period-timing behavior."""

    def __init__(self, session: Session, *, deleting_at: str | None = None) -> None:
        self.item = {**session.model_dump(mode="json"), "_etag": "e1"}
        if deleting_at is not None:
            self.item["deletingAt"] = deleting_at
        self.deleted = False

    async def read_item(self, *, item, partition_key):
        if self.deleted:
            raise CosmosResourceNotFoundError(message="deleted concurrently")
        return dict(self.item)

    async def patch_item(
        self, *, item, partition_key, patch_operations, etag=None, match_condition=None
    ):
        if self.deleted:
            raise CosmosResourceNotFoundError(message="deleted concurrently")
        for op in patch_operations:
            self.item[op["path"].lstrip("/")] = op["value"]
        self.item["_etag"] = "e2"

    async def delete_item(self, *, item, partition_key):
        self.deleted = True


async def test_cosmos_add_message_self_compensates_when_write_lands_after_sweep():
    """HIGH production-shape finding: delete_session previously deleted
    children BEFORE the parent. That left a permanent-orphan window: a
    concurrent add_message could pass its own ownership check while the
    session still existed, then have its write land strictly AFTER
    delete_session's children-sweep query had already run (and seen
    nothing) but before the session was gone -- so neither side ever
    caught it. The fix (a) tombstones the session up front (round 5) so
    every ownership check -- including add_message's own post-write
    recheck -- rejects it from that instant on, and (b) adds a post-write
    recheck-and-compensate to every child-writer; this test drives the
    exact worst-case interleaving that only the combination of both closes.

    This does not depend on delete_session's own parent-row hard-delete
    completing within this call (round 6's grace period can, and here
    does, defer that to a later call) -- only on the tombstone, which is
    set synchronously up front and is what every ownership check (both
    add_message's pre-write check and its post-write recheck) actually
    keys off.
    """
    session = Session(userId="u1")
    write_gate = asyncio.Event()

    class _GatedMessages:
        def __init__(self) -> None:
            self.items: dict[str, dict] = {}
            self.delete_attempts: list[str] = []

        async def create_item(self, body):
            # Force this write to land strictly after delete_session's
            # children-sweep query below has already run and seen nothing.
            await asyncio.wait_for(write_gate.wait(), timeout=5)
            self.items[body["id"]] = body

        async def query_items(self, *, query, parameters=None, partition_key=None):
            for doc_id in list(self.items.keys()):
                yield {"id": doc_id}

        async def delete_item(self, *, item, partition_key):
            self.delete_attempts.append(item)
            if item not in self.items:
                raise CosmosResourceNotFoundError(message="not found")
            del self.items[item]

    class _SignalingEmptyDocuments:
        """No-row ``documents`` container that opens the write gate once
        delete_session's cascade reaches it -- i.e., once the message sweep
        immediately before has already completed."""

        async def query_items(self, *, query, parameters=None, partition_key=None):
            write_gate.set()
            return
            yield  # pragma: no cover - makes this an async generator

    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _SessionsDeletedOnce(session)  # fresh tombstone, no pre-seed
    messages = _GatedMessages()
    repo._messages = messages
    repo._documents = _SignalingEmptyDocuments()

    message = Message(sessionId=session.id, userId="u1", role=MessageRole.user, content="hi")

    add_result, delete_result = await asyncio.gather(
        repo.add_message("u1", message),
        repo.delete_session("u1", session.id),
        return_exceptions=True,
    )

    assert delete_result is None  # delete_session succeeded (didn't raise)
    # Fresh tombstone: round 6's grace period defers the parent's hard
    # delete to a later call -- the session stays tombstoned (already
    # invisible via _owned_session, which is what matters for this race).
    assert repo._sessions.deleted is False
    assert isinstance(add_result, SessionNotFoundError)
    # The message must not survive the race: add_message's own post-write
    # recheck caught the now-tombstoned parent and self-compensated.
    assert message.id not in messages.items
    assert messages.delete_attempts == [message.id]


async def test_cosmos_add_message_if_summary_version_compensates_when_session_deleted_mid_write():
    """Sub-bug within the same finding: add_message_if_summary_version's own
    post-write summaryVersion recheck could raise SessionNotFoundError
    (parent deleted concurrently) and let it propagate uncaught, skipping
    the method's existing compensating-delete logic and leaving the
    just-written message orphaned even though the caller correctly saw an
    exception. The recheck must trigger the same self-compensation as a
    mismatched summaryVersion does."""
    session = Session(userId="u1", summaryVersion=1)
    sessions_fake = _SessionsDeletedOnce(session)

    class _MessagesRecordingDeletes:
        def __init__(self) -> None:
            self.items: dict[str, dict] = {}
            self.delete_attempts: list[str] = []

        async def create_item(self, body):
            self.items[body["id"]] = body
            # The parent is deleted strictly between this write landing and
            # the method's own recheck immediately below.
            sessions_fake.deleted = True

        async def delete_item(self, *, item, partition_key):
            self.delete_attempts.append(item)
            if item not in self.items:
                raise CosmosResourceNotFoundError(message="not found")
            del self.items[item]

    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = sessions_fake
    messages = _MessagesRecordingDeletes()
    repo._messages = messages
    message = Message(sessionId=session.id, userId="u1", role=MessageRole.user, content="hi")

    with pytest.raises(SessionNotFoundError):
        await repo.add_message_if_summary_version("u1", message, expected_version=1)

    assert message.id not in messages.items
    assert messages.delete_attempts == [message.id]


async def test_cosmos_upsert_message_compensates_when_session_deleted_mid_write():
    """Same recheck-and-compensate fix applied to upsert_message (used for
    both creating and updating a session's messages, e.g. streaming
    assistant replies): a session deleted strictly between the write
    landing and this method's own recheck must not leave the message
    behind."""
    session = Session(userId="u1")
    sessions_fake = _SessionsDeletedOnce(session)

    class _MessagesRecordingDeletes:
        def __init__(self) -> None:
            self.items: dict[str, dict] = {}
            self.delete_attempts: list[str] = []

        async def upsert_item(self, body):
            self.items[body["id"]] = body
            sessions_fake.deleted = True

        async def delete_item(self, *, item, partition_key):
            self.delete_attempts.append(item)
            if item not in self.items:
                raise CosmosResourceNotFoundError(message="not found")
            del self.items[item]

    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = sessions_fake
    messages = _MessagesRecordingDeletes()
    repo._messages = messages
    message = Message(sessionId=session.id, userId="u1", role=MessageRole.assistant, content="hi")

    with pytest.raises(SessionNotFoundError):
        await repo.upsert_message("u1", message)

    assert message.id not in messages.items
    assert messages.delete_attempts == [message.id]


async def test_cosmos_add_document_compensates_when_session_deleted_mid_write():
    """Same recheck-and-compensate fix applied to add_document: an uploaded
    document written strictly before a concurrent session delete must not
    survive as an orphan referencing a gone session."""
    session = Session(userId="u1")
    sessions_fake = _SessionsDeletedOnce(session)

    class _DocumentsRecordingDeletes:
        def __init__(self) -> None:
            self.items: dict[str, dict] = {}
            self.delete_attempts: list[str] = []

        async def create_item(self, body):
            self.items[body["id"]] = body
            sessions_fake.deleted = True

        async def delete_item(self, *, item, partition_key):
            self.delete_attempts.append(item)
            if item not in self.items:
                raise CosmosResourceNotFoundError(message="not found")
            del self.items[item]

    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = sessions_fake
    documents = _DocumentsRecordingDeletes()
    repo._documents = documents
    document = Document(sessionId=session.id, userId="u1", filename="a.txt")

    with pytest.raises(SessionNotFoundError):
        await repo.add_document("u1", document)

    assert document.id not in documents.items
    assert documents.delete_attempts == [document.id]
