import asyncio
from pathlib import Path

import pytest
from azure.cosmos.exceptions import CosmosAccessConditionFailedError

from ai4ia_api.agents.agent_catalog import load_agent_catalog
from ai4ia_api.agents.command_service import execute_command
from ai4ia_api.agents.commands import parse_input
from ai4ia_api.agents.summarization import SummarizationService
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.catalog import load_catalog
from ai4ia_api.sessions.cosmos_repo import CosmosSessionRepository
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, Session, ToolOverrides
from ai4ia_api.sessions.repository import SessionConflictError


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
