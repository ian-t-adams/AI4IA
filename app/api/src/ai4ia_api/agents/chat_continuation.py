"""Interactive exact-call checkpoints using the conversation's existing store."""
from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ..sessions.chat_continuation import (
    CHAT_APPROVAL_SECONDS, ChatContinuation, ChatContinuationError, ChatContinuationPayload,
    exact_chat_arguments,
)
from ..sessions.models import Message, MessageStatus
from ..sessions.repository import SessionRepository
from .approvals import (
    MAX_APPROVAL_REQUESTS_PER_TURN, ApprovalDraft, ApprovalOutcome, PendingToolApproval,
    arguments_digest, consume_grant, grant_hash, mint_pending_approval,
)
from .tools import ToolSpec
from .turn_checkpoint import TurnCheckpoint
from .consent import contract_hash
from ..memory.context_refs import MemoryContextBinding, MemoryReference
from ..memory.preferences import MemoryPreference

_GRANT_PREFIX = "chat-v1:"


def chat_grant_hash(grant: str) -> str:
    return _GRANT_PREFIX + grant_hash(grant)


def consume_chat_grant(record: PendingToolApproval, grant: str) -> ApprovalOutcome:
    if not record.grantHash.startswith(_GRANT_PREFIX):
        return consume_grant(None, grant)
    return consume_grant(record.model_copy(update={
        "grantHash": record.grantHash[len(_GRANT_PREFIX):],
    }), grant)


def validate_pending_call(message: Message, pending: PendingToolApproval) -> None:
    state = message.chatContinuation
    if state is None or state.payload is None:
        raise ChatContinuationError("The saved call is unavailable.")
    matches = [
        turn for turn in [state.payload.turn, *state.payload.children.values()]
        if turn is not None and turn.phase == "tools"
        and f"{turn.activityScope}:{turn.iterations}:{turn.nextToolIndex}" == pending.operationId
    ]
    if len(matches) != 1:
        raise ChatContinuationError("The saved call no longer matches its approval. Send a fresh request.")
    turn = matches[0]
    if turn.response is None or turn.nextToolIndex >= len(turn.response.toolCalls):
        raise ChatContinuationError("The saved call is incomplete. Send a fresh request.")
    function = turn.response.toolCalls[turn.nextToolIndex].get("function") or {}
    arguments = exact_chat_arguments(function.get("arguments") or "{}")
    if (
        pending.messageId != message.id or function.get("name") != pending.tool
        or pending.tool not in turn.contracts or arguments_digest(arguments) != pending.argumentsDigest
    ):
        raise ChatContinuationError("The saved call no longer matches its approval. Send a fresh request.")


class ChatContinuationController:
    def __init__(
        self, repo: SessionRepository, assistant: Message, *, request: dict[str, Any],
        turn_id: str, seed: dict[str, Any], recheck: Callable[[dict[str, Any]], Awaitable[None]],
        restored: bool = False,
    ) -> None:
        self.repo = repo
        self.assistant = assistant
        self._expected = assistant.model_copy(deep=True) if restored else None
        self._recheck = recheck
        self.approval_events: list[dict[str, Any]] = []
        self._has_held = restored
        self._seed = copy.deepcopy(seed)
        self._volatile_turn: TurnCheckpoint | None = None
        self._volatile_children: dict[str, TurnCheckpoint] = {}
        if not restored:
            assistant.chatContinuation = ChatContinuation(
                turnId=turn_id, revision=0, status="running",
                expiresAt=datetime.now(timezone.utc) + timedelta(seconds=CHAT_APPROVAL_SECONDS),
                payload=ChatContinuationPayload(
                    request=request, seed={"sessionBinding": seed["sessionBinding"], "memoryContext": seed["memoryContext"]},
                ),
            )
        state = assistant.chatContinuation
        if state is None or state.payload is None:
            raise ChatContinuationError("This turn no longer has an executable pending call.")
        self._restored = state.payload.turn.model_copy(deep=True) if state.payload.turn else None
        if restored:
            self._volatile_turn = self._restored
            self._volatile_children = copy.deepcopy(state.payload.children)
        self.previous_content = "".join(self._restored.completedText) if self._restored is not None else ""

    @property
    def state(self) -> ChatContinuation:
        state = self.assistant.chatContinuation
        if state is None:
            raise ChatContinuationError("The pending call is unavailable.")
        return state

    @property
    def restored(self) -> TurnCheckpoint | None:
        return self._restored

    @property
    def visible_resource_ids(self) -> frozenset[str]:
        return frozenset()

    async def initialize(self) -> None:
        if self._expected is None:
            await self._save(self.state)

    async def _save(self, state: ChatContinuation) -> None:
        self.assistant.chatContinuation = state
        self.assistant.continuationStatus = state.status
        state.bounded_body()
        if not await self.repo.save_chat_assistant(
            self.assistant.userId, self.assistant, expected=self._expected, turn_id=state.turnId,
        ):
            raise ChatContinuationError("This conversation or pending call changed. Refresh before continuing.")
        self._expected = self.assistant.model_copy(deep=True)

    async def _check(self) -> None:
        if self.state.status != "running":
            raise ChatContinuationError("This operation has no new dispatch authority.")
        if self.restored is not None:
            if self.state.payload is None:
                raise ChatContinuationError("The pending call was cleared.")
            await self._recheck(self.state.payload.seed)

    async def capture_memory_context(self, preference: MemoryPreference, references: list[MemoryReference]) -> None:
        if not references:
            return
        state = self.state.model_copy(deep=True)
        if state.payload is None:
            raise ChatContinuationError("The pending call was cleared.")
        existing = state.payload.seed.get("memoryContext")
        binding = MemoryContextBinding.model_validate(existing) if existing is not None else None
        if binding is not None and binding.preference != preference:
            raise ChatContinuationError("Previously supplied memory changed.")
        retained = {item.id: item for item in binding.references} if binding is not None else {}
        retained.update({item.id: item for item in references})
        state.payload.seed["memoryContext"] = MemoryContextBinding(
            preference=preference, references=list(retained.values()),
        ).model_dump(mode="json")
        state.revision += 1
        await self._save(state)

    async def _checkpoint(self, turn: TurnCheckpoint, *, dispatched: bool, child: str | None = None) -> None:
        state = self.state.model_copy(deep=True)
        if state.payload is None:
            raise ChatContinuationError("The pending call was cleared.")
        if child is None:
            self._volatile_turn = turn.model_copy(deep=True)
            if self._has_held:
                state.payload.turn = self._volatile_turn
        else:
            self._volatile_children[child] = turn.model_copy(deep=True)
            if self._has_held:
                state.payload.children[child] = self._volatile_children[child]
        state.revision += 1
        state.status = "dispatched" if dispatched else "running"
        await self._save(state)

    async def before_model(self, state: TurnCheckpoint, params: dict[str, Any]) -> None:
        await self._check()
        self.assistant.chatContinuation = self.state.model_copy(update={"operationId": None})
        await self._checkpoint(state, dispatched=True)

    async def begin_fallback(self) -> None:
        if (
            not self._has_held and self.state.status == "dispatched"
            and self._volatile_turn is not None and self._volatile_turn.phase == "model"
            and self._volatile_turn.completedModelCalls == 0 and not self._volatile_turn.steps
        ):
            await self._save(self.state.model_copy(update={
                "revision": self.state.revision + 1, "status": "running",
            }, deep=True))
        await self._check()
        await self._save(self.state.model_copy(update={
            "revision": self.state.revision + 1, "status": "dispatched", "operationId": None,
        }, deep=True))

    async def finish_fallback(self) -> None:
        await self._save(self.state.model_copy(update={
            "revision": self.state.revision + 1, "status": "running",
        }, deep=True))

    async def model_completed(self, state: TurnCheckpoint) -> None:
        if self._has_held and state.response is not None:
            for call in state.response.toolCalls:
                exact_chat_arguments((call.get("function") or {}).get("arguments") or "{}")
        await self._checkpoint(state, dispatched=False)

    async def before_tool(
        self, state: TurnCheckpoint, *, tool: str, arguments: dict[str, Any], contract: str,
        child: str | None = None,
    ) -> None:
        await self._check()
        operation = f"{state.activityScope}:{state.iterations}:{state.nextToolIndex}"
        pending = next((
            record for record in self.assistant.pendingApprovals or []
            if record.operationId == operation
        ), None)
        if pending is not None and (
            not pending.consumed or pending.is_expired() or pending.tool != tool
            or pending.argumentsDigest != arguments_digest(arguments)
        ):
            raise ChatContinuationError("No current approval authorizes this exact call.")
        self.assistant.chatContinuation = self.state.model_copy(update={"operationId": operation})
        await self._checkpoint(state, dispatched=True, child=child)

    async def tool_completed(self, state: TurnCheckpoint, *, outcome: str, child: str | None = None) -> None:
        detail = state.steps[-1].get("detail") if state.steps else None
        if outcome == "tool_error" and self.state.status == "dispatched" and detail in {"outcome_unknown", "cancelled"}:
            await self._save(self.state.cleared("unknown", "tool_outcome_unknown"))
            if detail == "cancelled":
                return
            raise ChatContinuationError("The tool did not return a confirmed result. It will not be replayed.")
        await self._checkpoint(state, dispatched=False, child=child)

    async def hold(
        self, state: TurnCheckpoint, *, spec: ToolSpec, draft: ApprovalDraft,
        arguments: dict[str, Any], contract: str,
        child: str | None = None,
    ) -> None:
        await self._check()
        if len(self.assistant.pendingApprovals or []) >= MAX_APPROVAL_REQUESTS_PER_TURN:
            raise ChatContinuationError("This turn reached its approval limit. Send a fresh request.")
        record, grant = mint_pending_approval(draft)
        record.grantHash = chat_grant_hash(grant)
        record.messageId = self.assistant.id
        record.operationId = f"{state.activityScope}:{state.iterations}:{state.nextToolIndex}"
        updated = self.state.model_copy(deep=True)
        if updated.payload is None:
            raise ChatContinuationError("The pending call was cleared.")
        if not self._has_held:
            self._seed["memoryContext"] = updated.payload.seed.get("memoryContext")
            updated.payload.seed = self._seed
            updated.payload.turn = copy.deepcopy(self._volatile_turn)
            updated.payload.children = copy.deepcopy(self._volatile_children)
        if child is None:
            updated.payload.turn = copy.deepcopy(state)
        else:
            updated.payload.children[child] = copy.deepcopy(state)
        for saved in [updated.payload.turn, *updated.payload.children.values()]:
            if saved is None:
                continue
            calls = [call for message in saved.conversation for call in message.get("tool_calls", [])]
            if saved.response is not None:
                calls.extend(saved.response.toolCalls)
            for call in calls:
                exact_chat_arguments((call.get("function") or {}).get("arguments") or "{}")
        updated.status = "waiting"
        updated.operationId = record.operationId
        updated.expiresAt = record.expiresAt
        updated.revision += 1
        self.assistant.pendingApprovals = [*(self.assistant.pendingApprovals or []), record]
        await self._save(updated)
        self._has_held = True
        self.approval_events = [{**record.model_dump(mode="json"), "grant": grant}]

    def approvals_for(self, state: TurnCheckpoint | None) -> frozenset[str]:
        if state is None:
            return frozenset()
        operation = f"{state.activityScope}:{state.iterations}:{state.nextToolIndex}"
        return frozenset(
            item.key for item in self.assistant.pendingApprovals or []
            if item.operationId == operation and item.consumed and not item.is_expired()
        )

    async def child_checkpoint(self, binding: dict[str, Any]):
        state = self.state.model_copy(deep=True)
        operation = state.operationId
        if state.payload is None or operation is None or state.status != "dispatched":
            raise ChatContinuationError("The linked agent has no active parent operation.")
        previous = state.payload.childBindings.get(operation)
        digest = contract_hash(binding)
        if previous is not None and previous != digest:
            raise ChatContinuationError("The linked agent changed. Send a fresh request.")
        if previous is None and len(state.payload.childBindings) >= 2:
            raise ChatContinuationError("This turn reached its linked-agent limit.")
        state.payload.childBindings[operation] = digest
        state.status = "running"
        state.revision += 1
        await self._save(state)
        child = ChildChatCheckpoint(self, operation)
        return child, self.approvals_for(child.restored)

    async def persist_result(self) -> bool:
        state = self.state
        if state.payload is None or (state.status == "waiting" and self.assistant.status is MessageStatus.complete):
            updated = state.model_copy(update={"revision": state.revision + 1}, deep=True)
        else:
            status = (
                "unknown" if state.status in {"dispatched", "unknown"} else
                "complete" if self.assistant.status is MessageStatus.complete else "cancelled"
            )
            updated = state.cleared(status, "turn_finished")
        await self._save(updated)
        return True


class ChildChatCheckpoint:
    visible_resource_ids: frozenset[str] = frozenset()

    def __init__(self, parent: ChatContinuationController, operation: str) -> None:
        self.parent = parent
        self.operation = operation
        payload = parent.state.payload
        existing = payload.children.get(operation) if payload is not None else None
        self.restored = existing.model_copy(deep=True) if existing is not None else None

    async def before_model(self, state: TurnCheckpoint, params: dict[str, Any]) -> None:
        await self.parent._check()
        self.parent.assistant.chatContinuation = self.parent.state.model_copy(update={"operationId": None})
        await self.parent._checkpoint(state, dispatched=True, child=self.operation)

    async def model_completed(self, state: TurnCheckpoint) -> None:
        if self.parent._has_held and state.response is not None:
            for call in state.response.toolCalls:
                exact_chat_arguments((call.get("function") or {}).get("arguments") or "{}")
        await self.parent._checkpoint(state, dispatched=False, child=self.operation)

    async def before_tool(
        self, state: TurnCheckpoint, *, tool: str, arguments: dict[str, Any], contract: str,
    ) -> None:
        await self.parent.before_tool(state, tool=tool, arguments=arguments, contract=contract, child=self.operation)

    async def tool_completed(self, state: TurnCheckpoint, *, outcome: str) -> None:
        await self.parent.tool_completed(state, outcome=outcome, child=self.operation)

    async def hold(
        self, state: TurnCheckpoint, *, spec: ToolSpec, draft: ApprovalDraft,
        arguments: dict[str, Any], contract: str,
    ) -> None:
        await self.parent.hold(
            state, spec=spec, draft=draft, arguments=arguments, contract=contract, child=self.operation,
        )
