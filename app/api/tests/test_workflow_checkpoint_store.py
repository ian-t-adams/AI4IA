import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest
from azure.cosmos._base import _format_batch_operations

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.sessions.deletion_models import DeletionIntegrityError, DeletionMigrationRequiredError
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, MessageStatus, Session
from ai4ia_api.sessions.repository import SessionNotFoundError
from ai4ia_api.workflows.automation_common import ExecutionLimits
from ai4ia_api.workflows.automation_models import FrozenWorkflow, WorkflowCheckpoint
from ai4ia_api.workflows.models import Workflow, WorkflowStep
from tests.cosmos_deletion_fake import CosmosState


def bundle(owner="owner"):
    return FrozenWorkflow(
        executionOwnerId=owner,
        workflow=Workflow(id="flow", userId=owner, name="flow", displayName="Flow", steps=[
            WorkflowStep(agent="analyst", instruction="{input}"),
        ]),
        agents=AgentCatalog(agents=[AgentSpec(
            name="analyst", displayName="Analyst", description="Analyse",
            systemPrompt="Analyse.", tools=["calculator"],
        )]),
        source={"owner": owner, "name": "flow", "revision": 0},
        modelId="model", deployment=DeploymentOption(
            deploymentName="model", region="eastus2", sku="GlobalStandard", dataZone="US",
        ), api="chat", bundleDigest="a" * 64, approvedBundleDigest="a" * 64,
        environmentDigest="b" * 64, stepContracts=[{}], toolDestinations={},
        toolContracts={}, selectedDocuments=[], memoryStamp=None,
        resourceStamps={}, safeOnly=True, nonce="fence",
    )


async def seed(repo, *, protocol=True):
    session = await repo.create_session(Session(id="session", userId="owner"))
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    state = WorkflowCheckpoint(
        id="wf-state-one", userId="owner", sessionId=session.id,
        deletionEpoch=session.deletionEpoch or "legacy", ownerEpoch="owner-generation", runId="owner:run-one",
        fingerprint="c" * 64, revision=0, status="pending", reason=None,
        createdAt=now, deadline=now + timedelta(minutes=30), leaseId=None, leaseExpiresAt=None,
        bundle=bundle(), input="Calculate.", limits=ExecutionLimits(spendMode="no_hard_dollar_cap"),
        allowTools=True, allowAutomaticMemory=True,
        step=0, previous="", completedSteps=[], currentResult=None, currentUsage=None,
        memoryContext=None,
        turn=None, draft=None, approvalHistory=[],
        operationId=None, operationState="idle", scheduleId=None, scheduleGeneration=None,
        wakeRevision=0,
    )
    user = Message(
        id="wf-user", userId="owner", sessionId=session.id, role=MessageRole.user,
        content="Calculate.", workflowRunId=state.runId, workflowRunFingerprint=state.fingerprint,
    )
    assistant = Message(
        id="wf-assistant", userId="owner", sessionId=session.id, role=MessageRole.assistant,
        status=MessageStatus.streaming, workflowRunId=state.runId,
        workflowRunStatus=state.status, workflowRunFingerprint=state.fingerprint,
    )
    if protocol:
        assert await repo.claim_workflow_checkpoint("owner", user, assistant, state)
    return session, state, user, assistant


@pytest.fixture(params=["memory", "cosmos"])
def repo(request):
    return InMemorySessionRepository(deletion_enabled=True) if request.param == "memory" else CosmosState().repo()


async def test_state_is_private_owned_and_create_is_atomic(repo):
    session, state, user, assistant = await seed(repo)
    assert not await repo.claim_workflow_checkpoint("owner", user, assistant, state)
    assert await repo.read_workflow_checkpoint("owner", session.id, state.id) == state
    assert len(await repo.list_messages("owner", session.id)) == 2
    with pytest.raises(SessionNotFoundError):
        await repo.read_workflow_checkpoint("other", session.id, state.id)


async def test_two_checkpoint_writers_have_one_snapshot_cas_winner(repo):
    _, state, _, assistant = await seed(repo)
    updated = state.model_copy(update={"revision": 1, "status": "running", "leaseId": "worker"})
    message = assistant.model_copy(update={"workflowRunStatus": "running"})
    results = await asyncio.gather(*[
        repo.replace_workflow_checkpoint(
            "owner", updated, message, expected=state, expected_assistant=assistant,
        ) for _ in range(2)
    ])
    assert sorted(results) == [False, True]
    assert await repo.read_workflow_checkpoint("owner", "session", state.id) == updated


async def test_clear_and_deletion_cannot_revive_checkpoint(repo):
    session, state, _, assistant = await seed(repo)
    await repo.clear_messages("owner", session.id)
    cleared = await repo.read_workflow_checkpoint("owner", session.id, state.id)
    assert cleared.status == "cancelled"
    assert cleared.bundle is None and cleared.input == "" and cleared.turn is None
    assert not await repo.replace_workflow_checkpoint(
        "owner", state.model_copy(update={"revision": 1}), assistant,
        expected=state, expected_assistant=assistant,
    )
    await repo.begin_deletion("owner", session.id)
    with pytest.raises(SessionNotFoundError):
        await repo.read_workflow_checkpoint("owner", session.id, state.id)
    with pytest.raises(SessionNotFoundError):
        await repo.replace_workflow_checkpoint(
            "owner", state.model_copy(update={"revision": 1}), assistant,
            expected=state, expected_assistant=assistant,
        )


async def test_legacy_conversation_is_not_enrolled():
    repo = InMemorySessionRepository()
    _, state, user, assistant = await seed(repo, protocol=False)
    with pytest.raises(DeletionMigrationRequiredError):
        await repo.claim_workflow_checkpoint("owner", user, assistant, state)
    assert await repo.list_messages("owner", "session") == []


async def test_terminal_state_and_frozen_limits_cannot_be_rewound(repo):
    _, state, _, assistant = await seed(repo)
    cancelled = state.model_copy(update={"revision": 1, "status": "cancelled"})
    message = assistant.model_copy(update={
        "status": MessageStatus.cancelled, "workflowRunStatus": "cancelled", "workflowConsentRevoked": True,
    })
    assert await repo.replace_workflow_checkpoint(
        "owner", cancelled, message, expected=state, expected_assistant=assistant,
    )
    with pytest.raises(DeletionIntegrityError):
        await repo.replace_workflow_checkpoint(
            "owner", state.model_copy(update={"revision": 2}), assistant,
            expected=cancelled, expected_assistant=message,
        )


async def test_real_sdk_fence_retry_preserves_both_checkpoint_and_message_etags():
    state = CosmosState()
    repo = state.repo()
    _, original, _, assistant = await seed(repo)
    execute = state.messages.execute_item_batch
    guards = []

    async def serialized(*, batch_operations, partition_key):
        wire = copy.deepcopy(batch_operations)
        formatted = _format_batch_operations(batch_operations)
        guards.append([item.get("ifMatch") for item in formatted])
        return await execute(batch_operations=wire, partition_key=partition_key)

    state.messages.execute_item_batch = serialized
    updated = original.model_copy(update={"revision": 1, "status": "running"})
    message = assistant.model_copy(update={"workflowRunStatus": "running"})

    async def winner(operations, partition):
        state.messages.before_batch = None
        assert await state.repo().replace_workflow_checkpoint(
            "owner", updated, message, expected=original, expected_assistant=assistant,
        )

    state.messages.before_batch = winner
    assert not await repo.replace_workflow_checkpoint(
        "owner", updated, message, expected=original, expected_assistant=assistant,
    )
    assert len(guards) == 3
    assert guards[-1][0] != guards[0][0]
    assert guards[-1][1:] == guards[0][1:]
    assert all(guards[-1])
