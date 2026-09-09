"""Exercise the real SDK's destructive formatting before transactional replay."""
from __future__ import annotations

import copy

import pytest
from azure.cosmos._base import _format_batch_operations

from tests.cosmos_deletion_fake import CosmosState
from tests.test_resumable_deletion import seed


@pytest.mark.parametrize("kind", ["checkpoint", "approval"])
@pytest.mark.parametrize("sdk_serialization", [False, True])
async def test_fence_retry_preserves_child_cas_through_real_sdk_formatting(kind, sdk_serialization):
    state = CosmosState()
    repo = state.repo()
    _, pending, _, approval = await seed(repo)
    execute = state.messages.execute_item_batch
    guards = []

    if sdk_serialization:
        async def serialized_batch(*, batch_operations, partition_key):
            # Capture this submission's conditions before the SDK pops options.
            wire = copy.deepcopy(batch_operations)
            formatted = _format_batch_operations(batch_operations)
            serialized_guards = [operation.get("ifMatch") for operation in formatted]
            assert serialized_guards == [
                options.get("if_match_etag") for _, _, options in wire
            ]
            guards.append(serialized_guards)
            return await execute(batch_operations=wire, partition_key=partition_key)

        state.messages.execute_item_batch = serialized_batch

    winner = None

    async def competing_writer(operations, partition):
        nonlocal winner
        state.messages.before_batch = None
        if kind == "checkpoint":
            winner = await state.repo().replace_message_if_workflow_status(
                "u1", pending.model_copy(update={"content": "new checkpoint evidence"}),
                expected_status="pending", expected_lease_token=None, expected_message=pending,
            )
        else:
            winner = await state.repo().consume_tool_approval(
                "u1", "s1", pending.id, approval.id
            )

    state.messages.before_batch = competing_writer
    if kind == "checkpoint":
        loser = await repo.replace_message_if_workflow_status(
            "u1", pending.model_copy(update={"content": "stale overwrite"}),
            expected_status="pending", expected_lease_token=None, expected_message=pending,
        )
    else:
        loser = await repo.consume_tool_approval("u1", "s1", pending.id, approval.id)

    assert winner is True
    assert loser is False
    saved = (await repo.list_messages("u1", "s1"))[0]
    if kind == "checkpoint":
        assert saved.content == "new checkpoint evidence"
    else:
        assert saved.pendingApprovals[0].consumed
    if sdk_serialization:
        assert len(guards) == 3
        assert guards[-1][0] != guards[0][0]
        assert guards[-1][1] == guards[0][1] is not None
    await repo.close()
