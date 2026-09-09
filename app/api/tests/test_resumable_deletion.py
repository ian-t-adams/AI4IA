from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ServiceRequestError
from azure.cosmos.exceptions import CosmosBatchOperationError

from ai4ia_api.agents.approvals import draft_for_call, mint_pending_approval
from ai4ia_api.agents.tools import ToolRisk, ToolSpec
from ai4ia_api.documents.ephemeral_store import BlobNotFoundError, EphemeralAttachmentStore
from ai4ia_api.library.blob_store import InMemoryBlobStore
from ai4ia_api.sessions.deletion_models import (
    CLEANUP_ITEMS,
    CONTROL_PARTITION,
    FENCE_ID,
    DeletionDisabledError,
    DeletionIntegrityError,
    DeletionMigrationRequiredError,
    DeletionUnavailableError,
    now_utc,
)
from ai4ia_api.sessions.deletion_service import ConversationDeletionService
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Document, Message, MessageRole, Session
from ai4ia_api.sessions.repository import SessionNotFoundError
from tests.cosmos_deletion_fake import CosmosState


@pytest.fixture(params=["memory", "cosmos"])
def repo(request):
    if request.param == "memory":
        return InMemorySessionRepository(deletion_enabled=True)
    return CosmosState().repo()


def message(sid, *, item_id="message", **kwargs):
    return Message(
        id=item_id, sessionId=sid, userId="u1", role=MessageRole.assistant, **kwargs
    )


async def verified(service, uid, sid):
    for _ in range(12):
        result = await service.reconcile(uid, sid)
        if result.state == "cleanup_verified":
            return result
    pytest.fail(f"Cleanup did not complete: {result.model_dump()}")


async def seed(repo):
    session = await repo.create_session(Session(id="s1", userId="u1", title="private title"))
    approval, _ = mint_pending_approval(draft_for_call(
        ToolSpec(name="external", description="tool", risk=ToolRisk.external),
        tool="external", label="external", arguments={},
    ))
    pending = message(
        session.id, workflowRunStatus="pending", workflowRunFingerprint="a" * 64,
        pendingApprovals=[approval],
    )
    await repo.add_message("u1", pending)
    doc = Document(id="d1", sessionId=session.id, userId="u1", filename="private.txt", text="private")
    await repo.add_document("u1", doc)
    return session, pending, doc, approval


WRITERS = [
    "add", "upsert_new", "upsert_existing", "summary_reply", "claim", "checkpoint",
    "approval", "document", "clear", "delete_document",
]


async def exercise(repo, name, session, pending, doc, approval):
    uid, sid = "u1", session.id
    if name == "add":
        return await repo.add_message(uid, message(sid, item_id="new"))
    if name == "upsert_new":
        return await repo.upsert_message(uid, message(sid, item_id="upsert"))
    if name == "upsert_existing":
        return await repo.upsert_message(uid, pending.model_copy(update={"content": "update"}))
    if name == "summary_reply":
        assert await repo.add_message_if_summary_version(
            uid, message(sid, item_id="summary"), expected_version=0
        )
    elif name == "claim":
        assert await repo.claim_workflow_run_if_absent(
            uid, message(sid, item_id="claim-user"), message(sid, item_id="claim-assistant")
        )
    elif name == "checkpoint":
        assert await repo.replace_message_if_workflow_status(
            uid, pending.model_copy(update={"content": "checkpoint"}),
            expected_status="pending", expected_lease_token=None, expected_message=pending,
        )
    elif name == "approval":
        assert await repo.consume_tool_approval(uid, sid, pending.id, approval.id)
    elif name == "document":
        return await repo.add_document(uid, doc.model_copy(update={"id": "d2"}))
    elif name == "clear":
        return await repo.clear_messages(uid, sid)
    elif name == "delete_document":
        return await repo.delete_document(uid, sid, doc.id)


@pytest.mark.parametrize("writer", WRITERS)
async def test_every_child_entrypoint_allows_active_and_denies_tombstone(repo, writer):
    args = await seed(repo)
    await exercise(repo, writer, *args)
    await repo.begin_deletion("u1", "s1")
    with pytest.raises(SessionNotFoundError):
        await exercise(repo, writer, *args)


@pytest.mark.parametrize("operation", [
    "get", "list_messages", "list_documents", "get_document", "patch", "title",
    "touch", "consent", "library", "invalidate", "commit",
])
async def test_every_parent_path_denies_after_intent_with_active_control(repo, operation):
    session, _, doc, _ = await seed(repo)
    if operation == "title":
        await repo.patch_session("u1", session.id, {"title": "New chat", "titleSource": "auto"})
    if operation == "library":
        await repo.patch_session("u1", session.id, {"libraryDocumentIds": []})

    async def act():
        if operation == "get":
            return await repo.get_session("u1", session.id)
        if operation == "list_messages":
            return await repo.list_messages("u1", session.id)
        if operation == "list_documents":
            return await repo.list_documents("u1", session.id)
        if operation == "get_document":
            return await repo.get_document("u1", session.id, doc.id)
        if operation == "patch":
            return await repo.patch_session("u1", session.id, {"model": "changed"})
        if operation == "title":
            return await repo.set_generated_title_if_eligible("u1", session.id, "generated")
        if operation == "touch":
            return await repo.touch_session("u1", session.id)
        if operation == "consent":
            return await repo.set_tool_consent("u1", session.id, None)
        if operation == "library":
            return await repo.mutate_library_document_ids("u1", session.id, "library-id", add=True)
        if operation == "invalidate":
            return await repo.invalidate_summary("u1", session.id)
        if operation == "commit":
            return await repo.commit_summary_if_version(
                "u1", session.id, expected_version=0, summary="summary",
                summarized_through_message_id="message",
            )

    await act()
    assert any(s.id == "s1" for s in await repo.list_sessions("u1"))
    await repo.begin_deletion("u1", session.id)
    with pytest.raises(SessionNotFoundError):
        await act()
    assert await repo.list_sessions("u1") == []
    assert (await repo.get_deletion_status("u1", session.id)).state == "pending"


@pytest.mark.parametrize("writer", WRITERS)
async def test_stale_fence_snapshot_rolls_back_whole_write_after_cleanup(writer):
    state = CosmosState()
    first, second = state.repo(), state.repo()
    args = await seed(first)
    # Same fixture and path first succeeds without closure.
    initial = state.snapshot()
    await exercise(first, writer, *args)
    state.sessions.items = initial["sessions"]
    state.messages.items = initial["messages"]
    state.documents.items = initial["documents"]
    paused = asyncio.Event()
    resume = asyncio.Event()
    container = state.documents if writer in {"document", "delete_document"} else state.messages

    async def before_batch(operations, partition):
        container.before_batch = None
        paused.set()
        await resume.wait()

    container.before_batch = before_batch
    task = asyncio.create_task(exercise(first, writer, *args))
    await asyncio.wait_for(paused.wait(), 3)
    await second.begin_deletion("u1", "s1")
    await verified(ConversationDeletionService(second, None), "u1", "s1")
    resume.set()
    with pytest.raises(SessionNotFoundError):
        await task
    assert set(state.messages.items) == {("s1", FENCE_ID)}
    assert set(state.documents.items) == {("s1", FENCE_ID)}


async def test_fence_batch_rollback_preserves_workflow_claim_atomicity():
    state = CosmosState()
    repo = state.repo()
    await repo.create_session(Session(id="s1", userId="u1"))
    await repo.add_message("u1", message("s1", item_id="second"))
    before = copy.deepcopy(state.messages.items)
    assert not await repo.claim_workflow_run_if_absent(
        "u1", message("s1", item_id="first"), message("s1", item_id="second")
    )
    assert state.messages.items == before
    assert await repo.claim_workflow_run_if_absent(
        "u1", message("s1", item_id="first"), message("s1", item_id="third")
    )


async def test_checkpoint_still_binds_expected_message_status_and_lease():
    state = CosmosState()
    repo = state.repo()
    session, pending, _, _ = await seed(repo)
    next_checkpoint = pending.model_copy(update={"content": "new evidence"})
    assert await repo.replace_message_if_workflow_status(
        "u1", next_checkpoint, expected_status="pending",
        expected_lease_token=None, expected_message=pending,
    )
    assert not await repo.replace_message_if_workflow_status(
        "u1", pending.model_copy(update={"content": "stale overwrite"}),
        expected_status="pending", expected_lease_token=None, expected_message=pending,
    )
    saved = await repo.list_messages("u1", session.id)
    assert saved[0].content == "new evidence"
    assert not await repo.replace_message_if_workflow_status(
        "u1", next_checkpoint, expected_status="pending", expected_lease_token="wrong",
        expected_message=next_checkpoint,
    )
    assert not await repo.replace_message_if_workflow_status(
        "u1", next_checkpoint, expected_status="completed", expected_lease_token=None,
        expected_message=next_checkpoint,
    )


async def test_independent_clients_resume_preserving_other_owners_and_scopes():
    state = CosmosState()
    repo = state.repo()
    await seed(repo)
    for owner, sid in (("u1", "sibling"), ("u2", "other")):
        await repo.create_session(Session(id=sid, userId=owner))
        await repo.add_message(owner, message(sid, item_id="kept", content="unaffected"))
        await repo.add_document(owner, Document(
            id="kept", sessionId=sid, userId=owner, filename="kept"
        ))
    before = state.snapshot()
    blobs = InMemoryBlobStore()
    await blobs.put("u1/processed/id.md", b"processed")
    await blobs.put("u1/library-id/original", b"library")
    await blobs.put("u1/generated/id.png", b"generated")
    await blobs.put("u2/s1/d1", b"other-owner")
    await blobs.put("u1/sibling/d1", b"other-session")
    blob_before = copy.deepcopy(blobs._data)
    await repo.begin_deletion("u1", "s1")
    # The parent is content-free before any child deletion.
    parent = state.sessions.items[("u1", "s1")]
    assert "title" not in parent and "summary" not in parent and parent["ttl"] == -1
    assert ("s1", "message") in state.messages.items
    with pytest.raises(SessionNotFoundError):
        await state.repo().begin_deletion("u2", "s1")
    with pytest.raises(SessionNotFoundError):
        await state.repo().get_deletion_status("u2", "s1")
    assert (await state.repo().list_deletions("u2")).items == []
    result = await verified(
        ConversationDeletionService(state.repo(), EphemeralAttachmentStore(blobs)), "u1", "s1"
    )
    assert result.lastVerifiedAt and not result.backupsErased
    for name in ("sessions", "messages", "documents"):
        actual = state.snapshot()[name]
        for key, value in before[name].items():
            if (name == "sessions" and key[1] != "s1") or (name != "sessions" and key[0] != "s1"):
                assert actual[key] == value
    assert blobs._data == blob_before
    assert (await state.repo().begin_deletion("u1", "s1")).state == "cleanup_verified"


async def test_stale_parent_reads_cannot_grant_access_or_patch():
    state = CosmosState()
    repo = state.repo()
    await seed(repo)
    assert (await repo.get_session("u1", "s1")).id == "s1"
    snapshot = copy.deepcopy(state.sessions.items[("u1", "s1")])
    await state.repo().begin_deletion("u1", "s1")
    state.sessions.stale_reads[("u1", "s1")] = snapshot
    with pytest.raises(DeletionUnavailableError):
        await repo.get_session("u1", "s1")
    with pytest.raises(DeletionUnavailableError):
        await repo.patch_session("u1", "s1", {"title": "resurrect"})
    assert "title" not in state.sessions.items[("u1", "s1")]


async def test_empty_eventually_consistent_scan_is_not_completion_evidence():
    state = CosmosState()
    repo = state.repo()
    await seed(repo)
    await repo.begin_deletion("u1", "s1")
    state.messages.stale_queries = True
    state.documents.stale_queries = True
    # No token would make this fake return an empty stale snapshot.
    assert [r async for r in state.messages.query_items(query="SELECT * FROM c")] == []
    result = await ConversationDeletionService(state.repo(), None).reconcile("u1", "s1")
    assert result.state == "pending"
    assert ("s1", "message") not in state.messages.items
    assert ("s1", "d1") not in state.documents.items
    assert all(
        query["session_token"] for container in (state.messages, state.documents)
        for query in container.queries if "TOP" in query["query"]
    )
    await verified(ConversationDeletionService(state.repo(), None), "u1", "s1")


@pytest.mark.parametrize("crash_phase", ["fences", "messages", "documents", "attachments"])
async def test_crash_restart_resumes_without_ephemeral_job_state(crash_phase):
    class Crash(BaseException):
        pass

    state = CosmosState()
    repo = state.repo()
    await seed(repo)
    await repo.begin_deletion("u1", "s1")

    async def crash(item, body):
        if body.get("status", {}).get("phase") == crash_phase and body.get("leaseToken"):
            state.sessions.before_replace = None
            raise Crash()

    state.sessions.before_replace = crash
    with pytest.raises(Crash):
        await ConversationDeletionService(repo, None).reconcile("u1", "s1")
    raw = state.sessions.items[("u1", "s1")]
    if raw.get("leaseToken"):
        raw["leaseExpiresAt"] = (now_utc() - timedelta(seconds=1)).isoformat()
        state.sessions._put(raw)
    result = await verified(ConversationDeletionService(state.repo(), None), "u1", "s1")
    assert result.state == "cleanup_verified"


async def test_lease_takeover_rejects_stale_checkpoint_with_same_fixture_control(repo):
    await seed(repo)
    await repo.begin_deletion("u1", "s1")
    first = await repo.claim_deletion("u1", "s1")
    assert first is not None
    assert await repo.claim_deletion("u1", "s1") is None
    current = await repo.checkpoint_deletion(first, first.record.status, release=False)
    assert current.etag != first.etag
    with pytest.raises(DeletionUnavailableError):
        await repo.checkpoint_deletion(first, first.record.status, release=True)
    await repo.checkpoint_deletion(current, current.record.status, release=True)
    replacement = await repo.claim_deletion("u1", "s1")
    assert replacement is not None and replacement.token != current.token
    with pytest.raises(DeletionUnavailableError):
        await repo.checkpoint_deletion(current, current.record.status, release=True)


async def test_concurrent_delete_is_idempotent_and_owned(repo):
    await seed(repo)
    statuses = await asyncio.gather(
        repo.begin_deletion("u1", "s1"), repo.begin_deletion("u1", "s1")
    )
    assert statuses[0].requestedAt == statuses[1].requestedAt
    await verified(ConversationDeletionService(repo, None), "u1", "s1")
    assert (await repo.begin_deletion("u1", "s1")).state == "cleanup_verified"


async def test_two_clients_collide_on_the_same_parent_delete_etag():
    state = CosmosState()
    await seed(state.repo())
    arrived = asyncio.Event()
    count = 0

    async def collide(item, body):
        nonlocal count
        if body.get("kind") != "session_tombstone_v1":
            return
        count += 1
        if count == 2:
            arrived.set()
        await arrived.wait()

    state.sessions.before_replace = collide
    results = await asyncio.wait_for(asyncio.gather(
        state.repo().begin_deletion("u1", "s1"),
        state.repo().begin_deletion("u1", "s1"),
    ), 3)
    assert count == 2 and results[0].requestedAt == results[1].requestedAt
    state.sessions.before_replace = None
    await verified(ConversationDeletionService(state.repo(), None), "u1", "s1")


async def test_expired_lease_takeover_fences_the_old_worker_checkpoint(monkeypatch):
    state = CosmosState()
    first, second = state.repo(), state.repo()
    await seed(first)
    await first.begin_deletion("u1", "s1")
    old = await first.claim_deletion("u1", "s1")
    assert old is not None and old.record.leaseExpiresAt is not None
    assert await second.claim_deletion("u1", "s1") is None
    future = old.record.leaseExpiresAt + timedelta(seconds=1)
    monkeypatch.setattr("ai4ia_api.sessions.cosmos_deletion.now_utc", lambda: future)
    replacement = await second.claim_deletion("u1", "s1")
    assert replacement is not None and replacement.token != old.token
    with pytest.raises(DeletionUnavailableError):
        await first.checkpoint_deletion(old, old.record.status, release=True)
    await second.checkpoint_deletion(replacement, replacement.record.status, release=True)
    await verified(ConversationDeletionService(second, None), "u1", "s1")


async def test_owner_status_pages_are_bounded_and_do_not_lose_retained_ids(repo):
    for index in range(52):
        sid = f"s{index:03}"
        await repo.create_session(Session(id=sid, userId="u1"))
        await repo.begin_deletion("u1", sid)
    first = await repo.list_deletions("u1")
    assert len(first.items) == 50 and first.hasMore
    second = await repo.list_deletions("u1", first.nextCursor)
    assert len(second.items) == 2 and not second.hasMore
    assert len({item.sessionId for item in first.items + second.items}) == 52
    assert (await repo.list_deletions("u2")).items == []


async def test_unresolved_ticket_sample_is_bounded_and_truthfully_truncated(repo):
    await repo.create_session(Session(id="s1", userId="u1"))
    for index in range(CLEANUP_ITEMS + 1):
        await repo.reserve_attachment_upload("u1", "s1", f"d{index}", storage_id="local")
    await repo.begin_deletion("u1", "s1")
    result = await ConversationDeletionService(
        repo, EphemeralAttachmentStore(InMemoryBlobStore())
    ).reconcile("u1", "s1")
    assert len(result.pendingUploads) == CLEANUP_ITEMS and result.pendingUploadsTruncated
    assert result.state == "pending" and result.lastVerifiedAt is None


async def test_initializer_cannot_reopen_fence_closed_while_parent_initializes():
    state = CosmosState()
    creator, deleter = state.repo(), state.repo()

    async def delete_before_fences(body):
        state.sessions.after_create = None
        await deleter.begin_deletion("u1", "s1")
        await verified(ConversationDeletionService(deleter, None), "u1", "s1")

    state.sessions.after_create = delete_before_fences
    with pytest.raises(DeletionIntegrityError):
        await creator.create_session(Session(id="s1", userId="u1"))
    assert state.sessions.items[("u1", "s1")]["kind"] == "session_tombstone_v1"
    assert state.messages.items[("s1", FENCE_ID)]["closed"] is True
    assert state.documents.items[("s1", FENCE_ID)]["closed"] is True
    assert (await creator.create_session(Session(id="control", userId="u1"))).id == "control"


async def test_pending_blob_put_after_empty_scan_blocks_verified_until_ack(repo):
    await repo.create_session(Session(id="s1", userId="u1"))
    blob = InMemoryBlobStore()
    store = EphemeralAttachmentStore(blob)
    intent = await repo.reserve_attachment_upload("u1", "s1", "d1", storage_id=store.storage_id)
    assert intent is not None
    await repo.begin_deletion("u1", "s1")
    service = ConversationDeletionService(repo, store)
    first = await service.reconcile("u1", "s1")
    assert first.state == "pending" and first.retryReason == "uploads_unresolved"
    assert not first.attachmentsVerified and first.lastVerifiedAt is None
    assert first.pendingUploads[0].id == intent.id
    # PUT was authorized/reserved earlier and can land after an empty purge.
    await store.put("u1", "s1", "d1", b"late")
    again = await service.reconcile("u1", "s1")
    assert again.state == "pending" and not again.attachmentsVerified
    assert blob._data == {}
    await repo.settle_attachment_upload(intent)
    result = await verified(service, "u1", "s1")
    assert result.pendingUploads == [] and result.attachmentsVerified
    with pytest.raises(SessionNotFoundError):
        await repo.reserve_attachment_upload("u1", "s1", "late", storage_id=store.storage_id)


async def test_unresolved_upload_never_expires_into_success(repo):
    await repo.create_session(Session(id="s1", userId="u1"))
    intent = await repo.reserve_attachment_upload("u1", "s1", "d1", storage_id="local")
    assert intent is not None
    await repo.begin_deletion("u1", "s1")
    service = ConversationDeletionService(repo, EphemeralAttachmentStore(InMemoryBlobStore()))
    for _ in range(4):
        status = await service.reconcile("u1", "s1")
        assert status.state == "pending" and status.pendingUploads[0].id == intent.id
        assert status.lastVerifiedAt is None


async def test_cached_attachment_reader_denied_before_bytes_are_physically_removed(repo):
    await repo.create_session(Session(id="s1", userId="u1"))
    blob = InMemoryBlobStore()
    store = EphemeralAttachmentStore(blob, session_repo=repo)
    path = await store.put("u1", "s1", "d1", b"retained")
    await repo.add_document("u1", Document(
        id="d1", sessionId="s1", userId="u1", filename="d.txt", rawRef=path
    ))
    assert await store.get("u1", "s1", "d1") == b"retained"
    await repo.begin_deletion("u1", "s1")
    with pytest.raises(BlobNotFoundError):
        await store.get("u1", "s1", "d1")
    assert blob._data[path] == b"retained"


async def test_attachment_configuration_change_does_not_verify_other_empty_store(repo):
    await repo.create_session(Session(id="s1", userId="u1"))
    ticket = await repo.reserve_attachment_upload("u1", "s1", "d1", storage_id="original")
    await repo.settle_attachment_upload(ticket)
    await repo.begin_deletion("u1", "s1")
    wrong = EphemeralAttachmentStore(InMemoryBlobStore(), storage_id="different")
    result = await ConversationDeletionService(repo, wrong).reconcile("u1", "s1")
    assert result.state == "retryable" and result.retryReason == "artifact_store_required"
    right = EphemeralAttachmentStore(InMemoryBlobStore(), storage_id="original")
    assert (await verified(ConversationDeletionService(repo, right), "u1", "s1")).attachmentsVerified


async def test_blob_and_query_errors_are_retryable_not_empty_success(repo):
    class Broken(InMemoryBlobStore):
        fail = True

        async def delete_prefix_page(self, prefix, *, limit):
            if self.fail:
                raise ServiceRequestError("sensitive storage path")
            return await super().delete_prefix_page(prefix, limit=limit)

    await repo.create_session(Session(id="s1", userId="u1"))
    ticket = await repo.reserve_attachment_upload("u1", "s1", "d1", storage_id="local")
    await repo.settle_attachment_upload(ticket)
    await repo.begin_deletion("u1", "s1")
    blob = Broken()
    service = ConversationDeletionService(repo, EphemeralAttachmentStore(blob))
    result = await service.reconcile("u1", "s1")
    assert result.state == "retryable" and result.retryReason == "storage_unavailable"
    assert "sensitive" not in result.model_dump_json()
    blob.fail = False
    await verified(service, "u1", "s1")


async def test_cleanup_bounds_and_reserved_records_are_preserved():
    state = CosmosState()
    repo = state.repo()
    await repo.create_session(Session(id="s1", userId="u1"))
    for index in range(CLEANUP_ITEMS + 3):
        await repo.add_message("u1", message("s1", item_id=f"m{index}"))
    await repo.begin_deletion("u1", "s1")
    status = await ConversationDeletionService(repo, None).reconcile("u1", "s1")
    assert status.state == "pending"
    assert len(state.messages.items) == 4  # three children plus the retained fence
    await verified(ConversationDeletionService(state.repo(), None), "u1", "s1")
    assert set(state.messages.items) == {("s1", FENCE_ID)}


async def test_bad_child_owner_blocks_cleanup_without_deleting_the_row():
    state = CosmosState()
    repo = state.repo()
    await seed(repo)
    raw = copy.deepcopy(state.documents.items[("s1", "d1")])
    state.documents._put(raw | {"userId": "u2"})
    wrong = copy.deepcopy(state.documents.items[("s1", "d1")])
    await repo.begin_deletion("u1", "s1")
    result = await ConversationDeletionService(repo, None).reconcile("u1", "s1")
    assert result.state == "retryable" and result.retryReason == "integrity_mismatch"
    assert state.documents.items[("s1", "d1")] == wrong
    state.documents._put(raw)
    await verified(ConversationDeletionService(state.repo(), None), "u1", "s1")


async def test_legacy_requires_migration_when_enabled_and_v1_never_falls_back():
    state = CosmosState()
    legacy = state.repo(enabled=False)
    await legacy.create_session(Session(id="legacy", userId="u1"))
    with pytest.raises(DeletionMigrationRequiredError):
        await state.repo().begin_deletion("u1", "legacy")
    with pytest.raises(DeletionMigrationRequiredError):
        await state.repo().delete_session("u1", "legacy")
    await legacy.delete_session("u1", "legacy")
    await state.repo().create_session(Session(id="v1", userId="u1"))
    with pytest.raises(DeletionDisabledError):
        await legacy.delete_session("u1", "v1")
    await state.repo().begin_deletion("u1", "v1")
    with pytest.raises(SessionNotFoundError):
        await legacy.get_session("u1", "v1")


async def test_status_reads_have_no_cleanup_or_write_side_effects():
    state = CosmosState()
    repo = state.repo()
    await seed(repo)
    await repo.begin_deletion("u1", "s1")
    before = state.snapshot()
    assert (await repo.get_deletion_status("u1", "s1")).state == "pending"
    assert len((await repo.list_deletions("u1")).items) == 1
    assert state.snapshot() == before


def approved_marker():
    return {
        "id": "reviewed-cutover", "userId": CONTROL_PARTITION,
        "kind": "session_deletion_rollout_v1", "protocol": 1, "state": "approved",
        "scope": "new_sessions_only", "singleWriteRegion": True,
        "noCoordinationExpiry": True, "writerCutoverEvidence": "review/cutover",
        "recoveryReviewEvidence": "review/recovery",
    }


@pytest.mark.parametrize("bad", [
    "missing", "multiwrite", "two_regions", "consistency", "ttl", "partition", "cutover",
])
async def test_startup_requires_real_layout_and_referenced_rollout_evidence(bad):
    state = CosmosState()
    repo = state.repo()
    repo._deletion_rollout_id = "reviewed-cutover"
    account = SimpleNamespace(
        WritableLocations=[{"name": "region"}], _EnableMultipleWritableLocations=False,
        ConsistencyPolicy={"defaultConsistencyLevel": "Session"},
    )

    async def read_account():
        return account

    repo._client = SimpleNamespace(client_connection=SimpleNamespace(GetDatabaseAccount=read_account))
    await state.sessions.create_item(approved_marker())
    await repo.check_deletion_ready()
    if bad == "missing":
        state.sessions.items.clear()
    elif bad == "multiwrite":
        account._EnableMultipleWritableLocations = True
    elif bad == "two_regions":
        account.WritableLocations.append({"name": "second"})
    elif bad == "consistency":
        account.ConsistencyPolicy["defaultConsistencyLevel"] = "Eventual"
    elif bad == "ttl":
        state.messages.properties["defaultTtl"] = 3600
    elif bad == "partition":
        state.documents.properties["partitionKey"]["paths"] = ["/userId"]
    elif bad == "cutover":
        marker = approved_marker() | {"writerCutoverEvidence": " "}
        state.sessions._put(marker)
    with pytest.raises(DeletionUnavailableError):
        await repo.check_deletion_ready()


async def test_fake_rejects_stale_cas_and_rolls_back_prior_batch_operations():
    state = CosmosState()
    container = state.messages
    await container.create_item({"id": "fence", "sessionId": "s1"})
    old = await container.read_item(item="fence", partition_key="s1")
    await container.replace_item(item="fence", body=dict(old), etag=old["_etag"])
    with pytest.raises(CosmosBatchOperationError):
        await container.execute_item_batch(
            partition_key="s1",
            batch_operations=[
                ("create", ({"id": "child", "sessionId": "s1"},), {}),
                ("replace", ("fence", dict(old)), {"if_match_etag": old["_etag"]}),
            ],
        )
    assert ("s1", "child") not in container.items


async def test_cleanup_query_failure_and_timeout_persist_retryable_progress(monkeypatch):
    state = CosmosState()
    repo = state.repo()
    await seed(repo)
    await repo.begin_deletion("u1", "s1")

    async def unavailable(query, partition):
        raise ServiceRequestError("unavailable")

    state.messages.before_query = unavailable
    service = ConversationDeletionService(repo, None)
    result = await service.reconcile("u1", "s1")
    assert result.state == "retryable" and result.retryReason == "storage_unavailable"
    assert ("s1", "message") in state.messages.items

    async def blocked(query, partition):
        await asyncio.Event().wait()

    state.messages.before_query = blocked
    monkeypatch.setattr("ai4ia_api.sessions.deletion_service.CLEANUP_TIMEOUT_SECONDS", 0.01)
    result = await service.reconcile("u1", "s1")
    assert result.state == "retryable" and result.retryReason == "cleanup_timeout"
    assert ("s1", "message") in state.messages.items
    state.messages.before_query = None
    await verified(service, "u1", "s1")


async def test_closed_fences_never_expire_and_wrong_generation_cannot_authorize():
    state = CosmosState()
    repo = state.repo()
    await seed(repo)
    raw = copy.deepcopy(state.messages.items[("s1", FENCE_ID)])
    state.messages._put(raw | {"epoch": "another-generation"})
    with pytest.raises(DeletionIntegrityError):
        await repo.add_message("u1", message("s1", item_id="wrong-generation"))
    state.messages._put(raw)
    await repo.add_message("u1", message("s1", item_id="control"))
    state.messages._put(raw | {"ttl": 1})
    await repo.begin_deletion("u1", "s1")
    result = await ConversationDeletionService(repo, None).reconcile("u1", "s1")
    assert result.state == "retryable" and result.retryReason == "integrity_mismatch"
    assert ("s1", "message") in state.messages.items
    state.messages._put(raw)
    await verified(ConversationDeletionService(repo, None), "u1", "s1")
