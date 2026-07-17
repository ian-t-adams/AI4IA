import asyncio

from azure.cosmos.exceptions import CosmosAccessConditionFailedError

from ai4ia_api.sessions.cosmos_repo import CosmosSessionRepository
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Session, ToolOverrides


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
