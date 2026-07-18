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
from ai4ia_api.sessions.models import Message, MessageRole, Session, ToolOverrides
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
