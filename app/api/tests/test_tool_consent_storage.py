"""Consent and cancellation writes remain ownership-scoped under CAS races."""
from __future__ import annotations

import asyncio
import copy

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosAccessConditionFailedError, CosmosResourceNotFoundError

from ai4ia_api.agents.consent import ConsentSnapshot, contract_hash, mint_consent
from ai4ia_api.receipts import ReceiptToolCall, build_receipt, json_payload
from ai4ia_api.sessions.cosmos_repo import CosmosSessionRepository
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, MessageStatus, Session
from ai4ia_api.sessions.repository import SessionConflictError, SessionNotFoundError
from ai4ia_api.workflows.persistence import persist_run_message


def _consent(session):
    return mint_consent(
        ConsentSnapshot(contract_hash("selection"), contract_hash("environment"), {"calculator": "a" * 64}),
        user_id=session.userId, session_id=session.id,
    )


async def test_memory_concurrent_grants_have_one_winner_and_revocation_cannot_be_lost():
    repo = InMemorySessionRepository()
    session = await repo.create_session(Session(userId="owner"))
    consent = _consent(session)
    results = await asyncio.gather(
        repo.set_tool_consent("owner", session.id, consent, expected_version=0),
        repo.set_tool_consent("owner", session.id, _consent(session), expected_version=0),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Session) for result in results) == 1
    assert sum(isinstance(result, SessionConflictError) for result in results) == 1
    await repo.set_tool_consent("owner", session.id, None)
    with pytest.raises(SessionConflictError):
        await repo.set_tool_consent("owner", session.id, consent, expected_version=1)
    assert (await repo.get_session("owner", session.id)).toolConsent is None
    with pytest.raises(SessionNotFoundError):
        await repo.set_tool_consent("other", session.id, None)
    with pytest.raises(ValueError):
        await repo.patch_session("owner", session.id, {"toolConsentState": consent})


class _SessionItem:
    def __init__(self, session, race):
        self.item = {**CosmosSessionRepository._to_doc(session), "_etag": "one"}
        self.race = race
        self.etags = []

    async def read_item(self, *, item, partition_key):
        if self.item["id"] != item or self.item["userId"] != partition_key:
            raise CosmosResourceNotFoundError(message="missing")
        return copy.deepcopy(self.item)

    async def patch_item(
        self, *, item, partition_key, patch_operations, etag=None, match_condition=None,
    ):
        self.etags.append(etag)
        if self.race:
            self.race = False
            self.item.update({
                "toolConsent": None, "toolConsentState": None,
                "toolConsentVersion": self.item["toolConsentVersion"] + 1,
                "_etag": "revoked",
            })
        if match_condition == MatchConditions.IfNotModified and etag != self.item["_etag"]:
            raise CosmosAccessConditionFailedError(message="stale")
        for operation in patch_operations:
            self.item[operation["path"].lstrip("/")] = copy.deepcopy(operation["value"])
        self.item["_etag"] = "written"


@pytest.mark.parametrize("race", [False, True])
async def test_cosmos_grant_cas_respects_a_concurrent_revocation(race):
    session = Session(userId="owner")
    consent = _consent(session)
    repo = object.__new__(CosmosSessionRepository)
    store = _SessionItem(session, race)
    repo._sessions = store
    if race:
        with pytest.raises(SessionConflictError):
            await repo.set_tool_consent("owner", session.id, consent, expected_version=0)
        assert store.item["toolConsentState"] is None
    else:
        result = await repo.set_tool_consent("owner", session.id, consent, expected_version=0)
        assert result.toolConsentState == consent
        assert store.item["toolConsentState"]["grant"]["id"] == consent.grant.id
        assert "toolConsentState" not in result.model_dump(mode="json")
    assert store.etags == ["one"]


@pytest.mark.parametrize("cancel_during_write", [False, True])
async def test_late_workflow_result_keeps_receipt_without_undoing_cancellation(cancel_during_write):
    class RacingRepo(InMemorySessionRepository):
        raced = False

        async def replace_message_if_workflow_status(self, uid, message, **kwargs):
            if cancel_during_write and not self.raced:
                self.raced = True
                current = self._messages[message.sessionId][0].model_copy(update={
                    "workflowConsentRevoked": True,
                    "workflowRunStatus": "cancelled", "status": MessageStatus.cancelled,
                })
                await self.upsert_message(uid, current)
            return await super().replace_message_if_workflow_status(uid, message, **kwargs)

    repo = RacingRepo()
    session = await repo.create_session(Session(userId="owner"))
    pending = Message(
        sessionId=session.id, userId="owner", role=MessageRole.assistant,
        status=MessageStatus.streaming, workflowRunId="owner:run",
        workflowRunStatus="running", workflowRunFingerprint="f" * 64,
    )
    await repo.add_message("owner", pending)
    finished = pending.model_copy(update={
        "status": MessageStatus.complete, "workflowRunStatus": "completed",
        "executionReceipt": build_receipt(calls=[ReceiptToolCall(
            tool="send", outcome="result", approval="run", consentId="a" * 32,
            arguments=json_payload({"to": "owner"}), result=json_payload({"sent": True}),
        )]),
    })
    saved = await persist_run_message(repo, "owner", finished)
    assert saved.executionReceipt.toolCallCount == 1
    assert saved.executionReceipt.toolCalls[0].result.text
    assert saved.workflowConsentRevoked is cancel_during_write
    assert saved.status is (
        MessageStatus.cancelled if cancel_during_write else MessageStatus.complete
    )


def _checkpoint(session, count):
    receipts = [
        build_receipt(calls=[ReceiptToolCall(
            tool="calculator", outcome="result",
            arguments=json_payload({"expression": f"{index}+1"}),
            result=json_payload({"value": index + 1}),
        )])
        for index in range(count)
    ]
    return Message(
        id="run-assistant", sessionId=session.id, userId=session.userId,
        role=MessageRole.assistant, status=MessageStatus.streaming,
        workflowRunId=f"{session.userId}:run", workflowRunStatus="running",
        workflowRunFingerprint="f" * 64, workflowStepReceipts=receipts,
        executionReceipt=build_receipt(calls=[call for receipt in receipts for call in receipt.toolCalls]),
    )


@pytest.mark.parametrize("race", [False, True])
async def test_checkpoint_replacement_cannot_overwrite_newer_same_status_evidence(race):
    class RacingRepo(InMemorySessionRepository):
        raced = False

        async def replace_message_if_workflow_status(self, uid, message, **kwargs):
            if race and not self.raced:
                self.raced = True
                await self.upsert_message(uid, newer)
            return await super().replace_message_if_workflow_status(uid, message, **kwargs)

    repo = RacingRepo()
    session = await repo.create_session(Session(userId="owner"))
    original = _checkpoint(session, 1)
    newer = original.model_copy(update={
        "workflowStepReceipts": _checkpoint(session, 2).workflowStepReceipts,
        "executionReceipt": _checkpoint(session, 2).executionReceipt,
    }, deep=True)
    await repo.add_message("owner", original)
    saved = await persist_run_message(repo, "owner", original.model_copy(deep=True))
    assert len(saved.workflowStepReceipts) == (2 if race else 1)
    assert saved.executionReceipt.toolCallCount == (2 if race else 1)


@pytest.mark.parametrize("race", ["none", "before_read", "during_replace"])
async def test_cosmos_checkpoint_snapshot_and_etag_both_protect_evidence(race):
    session = Session(userId="owner")
    original = _checkpoint(session, 1)
    newer = _checkpoint(session, 2).model_copy(update={"createdAt": original.createdAt})
    cancelled = original.model_copy(update={
        "status": MessageStatus.cancelled, "workflowRunStatus": "cancelled",
        "workflowConsentRevoked": True,
    }, deep=True)

    class Messages:
        def __init__(self):
            initial = newer if race == "before_read" else original
            self.item = {**CosmosSessionRepository._to_doc(initial), "_etag": "one"}
            self.replaces = 0

        async def read_item(self, **_kwargs):
            return copy.deepcopy(self.item)

        async def replace_item(self, *, item, body, etag, match_condition):
            self.replaces += 1
            if race == "during_replace":
                self.item = {**CosmosSessionRepository._to_doc(newer), "_etag": "two"}
            if match_condition == MatchConditions.IfNotModified and etag != self.item["_etag"]:
                raise CosmosAccessConditionFailedError(message="stale")
            self.item = {**copy.deepcopy(body), "_etag": "written"}

    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _SessionItem(session, False)
    store = Messages()
    repo._messages = store
    replaced = await repo.replace_message_if_workflow_status(
        "owner", cancelled, expected_status="running", expected_lease_token=None,
        expected_message=original,
    )
    assert replaced is (race == "none")
    assert store.item["executionReceipt"]["toolCallCount"] == (1 if race == "none" else 2)
    assert store.replaces == (0 if race == "before_read" else 1)
