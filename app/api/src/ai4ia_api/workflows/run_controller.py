"""One fenced durable turn: approval, logical effects and actual egress."""
from __future__ import annotations

import json
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Literal, TYPE_CHECKING

from ..agents.approvals import (
    ApprovalDraft, ApprovalPolicy, approval_key, arguments_digest, requires_invocation_approval,
)
from ..agents.consent_service import execution_tools_for_state
from ..agents.synthetic_governance import synthetic_spec
from ..agents.tool_exec import ToolContext, ToolExecutor, validate_args
from ..agents.tools import ToolRegistry, ToolRisk, ToolSpec
from ..agents.turn_checkpoint import TurnCheckpoint
from ..auth.base import AuthenticatedUser
from ..gateway.attempts import current_attempt_envelope
from ..hard_quota.coverage import reservation_bounds
from ..hard_quota.models import Surface
from ..memory.context_refs import MemoryContextBinding, MemoryReference
from ..memory.preferences import MemoryPreference, MemoryPreferenceConflict, MemoryPreferenceUnavailable
from ..policy.models import PolicyRequest
from ..sessions.models import Message
from ..usage.models import TokenUsage, UsageRecord, UsageTarget
from ..usage.pricing import PriceRate, PricingBook
from .automation_common import (
    APPROVAL_SECONDS, MAX_APPROVALS_PER_STEP, AutomationError, digest, exact_arguments, stable_id, utc,
)
from .automation_models import AutomationOwner, EffectIntent, InvocationDraft, WorkflowCheckpoint
from .monetary_ledger import compact_money, reserve_money, settle_money
from .monetary_quotes import operation_impact, quote_for_call, require_quote_current, run_account, same_prices

if TYPE_CHECKING:
    from .automation_service import WorkflowAutomationService


class RunController:
    def __init__(
        self, service: WorkflowAutomationService, state: WorkflowCheckpoint, message: Message, *,
        user: AuthenticatedUser | None,
    ) -> None:
        self.service = service
        self.current = state
        self.message = message
        self.owner_id = state.userId
        self.user = user
        self._lease = state.leaseId
        self._restored = state.turn.model_copy(deep=True) if state.turn else None
        self._registry: ToolRegistry | None = None
        self._executor: ToolExecutor | None = None
        self._operation: str | None = None
        self._number = -1
        self._dispatch_sequence = 0
        self._approved_operation = state.draft.operationId if state.draft and user else None
        self._aliases: dict[str, str] = {}
        self._local_zero: ContextVar[bool] = ContextVar("workflow_local_zero", default=False)

    @property
    def restored(self) -> TurnCheckpoint | None:
        return self._restored

    @property
    def visible_resource_ids(self) -> frozenset[str]:
        return frozenset(self.bundle.selectedDocuments)

    @property
    def bundle(self):
        bundle = self.current.bundle
        if bundle is None:
            raise AutomationError("context_revoked", "The execution context was cleared.")
        return bundle

    def operation(self, state: TurnCheckpoint, kind: str) -> tuple[str, int]:
        ordinal = state.nextToolIndex if kind == "tool" else -1
        return (
            f"{self.current.step}:{state.iterations}:{kind}:{ordinal}",
            self.current.step * 64 + state.iterations * 16 + ordinal + 1,
        )

    async def check_current(self) -> datetime:
        self.service.require_enabled(scheduling=self.current.scheduleId is not None)
        self.service.require_spend_support(self.bundle, self.current.limits)
        owner = await self.service.owner(self.owner_id)
        handle = owner.value.runs[self.current.runId]
        account = run_account(owner.value, self.current)
        if account is not None and account.blocked:
            raise AutomationError("budget_stopped", "The monetary accounting contract no longer admits work.")
        if (
            owner.value.epoch != self.current.ownerEpoch or not handle.active or handle.terminal
            or owner.now >= utc(self.current.deadline)
        ):
            raise AutomationError("execution_revoked", "This run no longer permits another dispatch.")
        current, message = await self.service.load(self.owner_id, self.current.runId)
        if (
            current.revision != self.current.revision or current.leaseId != self._lease
            or message.workflowConsentRevoked or current.status != "running"
        ):
            raise AutomationError("checkpoint_changed", "Execution ownership changed.")
        await self.service.access.recheck(self.bundle, user=self.user)
        await self.service.access.check_context(current)
        return owner.now

    async def save(self, updated: WorkflowCheckpoint) -> None:
        self.current, self.message = await self.service.commit(self.current, self.message, updated)

    def capabilities(self, names: list[str]):
        built = self.service.access.capabilities(
            self.owner_id, self.current.sessionId, names, self.bundle.selectedDocuments,
            nonce=self.bundle.nonce, safe_only=self.bundle.safeOnly,
        )
        return built.tools, built.handlers

    async def tools(
        self, names: Sequence[str], ctx: ToolContext,
    ) -> tuple[ToolRegistry, ToolExecutor, ToolContext]:
        registry, executor, ctx = await execution_tools_for_state(
            self.service.state, user_id=self.owner_id, tool_names=names, ctx=ctx,
        )
        self._registry, self._executor = registry, executor
        self._aliases = dict(ctx.tool_aliases)
        return registry, executor, replace(ctx, capture_memory_context=self.capture_memory)

    def approvals(self) -> frozenset[str]:
        draft = self.current.draft
        if (
            draft is not None and draft.state == "approved"
            and draft.challenge is not None and draft.challenge.consumed
        ):
            return frozenset({approval_key(draft.tool, draft.argumentsDigest)})
        return frozenset()

    async def begin(self, state: TurnCheckpoint, kind: Literal["model", "tool"], payload: Any) -> None:
        now = await self.check_current()
        if state.contracts != self.bundle.stepContracts[self.current.step]:
            raise AutomationError("tool_contract_changed", "The effective tool subset no longer matches.")
        operation, number = self.operation(state, kind)
        if self._approved_operation is not None and operation != self._approved_operation:
            self.user = None
        identity = stable_id(self.owner_id, self.current.runId, operation)
        fingerprint = digest(payload)
        reserved = self.current.model_copy(update={
            "turn": state, "currentResult": None, "currentUsage": None,
            "operationId": operation, "operationState": "reserved",
            "leaseExpiresAt": now + timedelta(seconds=30),
        }, deep=True)
        await self.save(reserved)

        def reserve(owner: AutomationOwner, observed: datetime) -> None:
            handle = owner.runs[self.current.runId]
            if handle.terminal or not handle.active or number <= handle.operationFloor:
                raise AutomationError("operation_replayed", "This operation cannot acquire another attempt.")
            prior = owner.effects.get(identity)
            if prior is not None:
                if prior.payloadDigest != fingerprint or prior.state != "reserved":
                    raise AutomationError("outcome_unknown", "The prior operation is not replayable.")
                return
            if kind == "tool":
                if handle.toolCalls >= self.current.limits.maxToolCalls:
                    raise AutomationError("tool_limit", "The run's tool-call limit was reached.")
                handle.toolCalls += 1
            owner.effects[identity] = EffectIntent(
                id=identity, runId=self.current.runId, sessionId=self.current.sessionId,
                operationId=operation, category=kind, payloadDigest=fingerprint,
                state="reserved", startedAt=observed, resultDigest=None, usage=None, delivered=True,
            )

        await self.service.mutate_owner(self.owner_id, reserve)
        await self.check_current()
        claimed = self.current.model_copy(update={"operationState": "dispatched"}, deep=True)
        if kind == "tool" and claimed.draft is not None:
            claimed.draft.state = "dispatched"
        await self.save(claimed)

        def dispatch(owner: AutomationOwner, observed: datetime) -> None:
            handle = owner.runs[self.current.runId]
            effect = owner.effects[identity]
            if handle.terminal or effect.state != "reserved" or effect.payloadDigest != fingerprint:
                raise AutomationError("outcome_unknown", "The operation dispatch claim is no longer available.")
            effect.state = "dispatched"

        await self.service.mutate_owner(self.owner_id, dispatch)
        self._operation, self._number = operation, number
        self._dispatch_sequence = 0

    async def before_model(self, state: TurnCheckpoint, params: dict[str, Any]) -> None:
        self._local_zero.set(False)
        await self.begin(state, "model", {"messages": state.conversation, "params": params})

    async def before_tool(
        self, state: TurnCheckpoint, *, tool: str, arguments: dict[str, Any], contract: str,
    ) -> None:
        spec = (self._registry.get(tool) if self._registry else None) or synthetic_spec(tool)
        if spec is None or contract != self.bundle.stepContracts[self.current.step].get(tool):
            raise AutomationError("tool_contract_changed", "The tool has no matching current contract.")
        definition = self._executor.get(tool) if self._executor else None
        if definition is not None and validate_args(definition.parameters, arguments):
            raise AutomationError("invalid_arguments", "Tool arguments do not match the approved schema.")
        impact = operation_impact(definition, contract)
        if self.current.limits.maxSpendMicroUsd is not None and impact.coverage != "bounded":
            raise AutomationError("spend_unbounded", "This exact tool has no proven USD application-meter bound.")
        actor = await self.service.access.actor(self.owner_id, self.user)
        canonical = next(
            (name for name, alias in self.current_aliases().items() if alias == tool), tool,
        )
        await self.service.state.policy.require(actor, PolicyRequest(
            "tool.invoke", tool_name=canonical, tool_contract_digest=contract,
            resource_ids=tuple(self.bundle.selectedDocuments),
        ))
        if requires_invocation_approval(
            spec, policy=ApprovalPolicy.always, untrusted_context=state.untrustedContext,
        ):
            draft = self.current.draft
            now = (await self.service.owner(self.owner_id)).now
            operation, _ = self.operation(state, "tool")
            if (
                draft is None or draft.state != "approved" or draft.operationId != operation
                or draft.tool != tool or draft.argumentsDigest != arguments_digest(arguments)
                or draft.contractDigest != contract or draft.bundleDigest != self.bundle.bundleDigest
                or draft.challenge is None or not draft.challenge.consumed or now >= draft.expiresAt
                or draft.destination != self.service.access.destination(self.bundle, tool, arguments)
            ):
                raise AutomationError("grant_rejected", "No current one-time approval authorizes this exact call.")
            require_quote_current(
                self.current, draft, run_account((await self.service.owner(self.owner_id)).value, self.current),
                definition=definition, now=now, challenged=True,
            )
        await self.begin(state, "tool", {"tool": tool, "arguments": arguments, "contract": contract})
        self._local_zero.set(impact.coverage == "bounded" and (
            self.current.limits.maxSpendMicroUsd is not None
            or (self.current.draft is not None and self.current.draft.spend is not None)
        ))

    def current_aliases(self) -> dict[str, str]:
        return self._aliases

    async def complete(self, state: TurnCheckpoint, *, failed: bool = False) -> None:
        if self._operation is None:
            raise AutomationError("operation_missing", "There is no active operation to checkpoint.")
        identity = stable_id(self.owner_id, self.current.runId, self._operation)
        current_owner = await self.service.owner(self.owner_id)
        dispatches = [
            effect for effect in current_owner.value.effects.values()
            if effect.runId == self.current.runId and effect.operationId == self._operation
            and effect.category == "dispatch"
        ]
        if ":model:" in self._operation and not dispatches:
            raise AutomationError("unobserved_dispatch", "The model did not traverse governed dispatch.")
        unknown = any(effect.state in {"dispatched", "unknown"} for effect in dispatches)
        await self.service.flush_usage(self.owner_id, self.current.runId)

        def finish(owner: AutomationOwner, now: datetime) -> None:
            effect = owner.effects.get(identity)
            if effect is None or effect.state != "dispatched":
                raise AutomationError("operation_replayed", "The operation result was already recorded.")
            effect.state = "unknown" if unknown else "complete"
            effect.resultDigest = digest(state.model_dump(mode="json"))

        await self.service.mutate_owner(self.owner_id, finish)
        updated = self.current.model_copy(update={
            "turn": state, "operationState": "unknown" if unknown else "complete",
            "leaseExpiresAt": (await self.service.owner(self.owner_id)).now + timedelta(seconds=30),
        }, deep=True)
        if updated.draft is not None and updated.draft.operationId == self._operation:
            updated.approvalHistory.append(updated.draft)
            updated.draft = None
        if unknown or failed:
            updated.status = "outcome_unknown" if unknown else "failed"
            updated.reason = "dispatch_outcome_unknown" if unknown else "tool_failed"
        await self.save(updated)

        def floor(owner: AutomationOwner, now: datetime) -> None:
            handle = owner.runs[self.current.runId]
            handle.operationFloor = max(handle.operationFloor, self._number)
            # The fenced continuation is now past this operation. Its permanent
            # floor prevents replay; delivered ledger rows retain the accounting.
            for key, effect in list(owner.effects.items()):
                if (
                    effect.runId == self.current.runId and effect.operationId == self._operation
                    and effect.state == "complete" and (effect.usage is None or effect.delivered)
                ):
                    compact_money(owner, effect)
                    del owner.effects[key]

        await self.service.mutate_owner(self.owner_id, floor)
        if self._approved_operation == self._operation:
            self.user = None
            self._approved_operation = None
        if unknown or failed:
            raise AutomationError(updated.reason or "step_failed", "The run cannot safely continue.")

    async def model_completed(self, state: TurnCheckpoint) -> None:
        if state.response is not None and len(state.response.toolCalls) > 8:
            raise AutomationError("tool_limit", "The model emitted too many calls for a resumable batch.")
        await self.complete(state)

    async def tool_completed(self, state: TurnCheckpoint, *, outcome: str) -> None:
        if outcome == "tool_denied":
            raise AutomationError("tool_denied", "The requested tool is not currently authorized.")
        await self.complete(state, failed=outcome == "tool_error")

    async def hold(
        self, state: TurnCheckpoint, *, spec: ToolSpec, draft: ApprovalDraft,
        arguments: dict[str, Any], contract: str,
    ) -> None:
        now = await self.check_current()
        if self.bundle.safeOnly:
            raise AutomationError("not_safe", "A safe schedule cannot expand into an approval-gated operation.")
        operation, _ = self.operation(state, "tool")
        if sum(item.step == self.current.step for item in self.current.approvalHistory) >= MAX_APPROVALS_PER_STEP:
            raise AutomationError("approval_limit", "The step's approval request limit was reached.")
        _, identity, shown = exact_arguments(
            json.dumps(arguments), visible_resource_ids=self.visible_resource_ids,
        )
        destination = self.service.access.destination(self.bundle, draft.tool, arguments)
        if spec.risk is ToolRisk.external and not destination:
            raise AutomationError("arguments_not_reviewable", "The external destination cannot be bound for review.")
        pending = InvocationDraft(
            id=stable_id(self.owner_id, self.current.runId, operation, identity),
            operationId=operation, runId=self.current.runId, step=self.current.step,
            iteration=state.iterations, ordinal=state.nextToolIndex,
            tool=draft.tool,
            canonicalTool=next((name for name, alias in self._aliases.items() if alias == draft.tool), draft.tool),
            label=draft.label,
            purpose=draft.purpose, risk=draft.risk,
            contractDigest=contract, bundleDigest=self.bundle.bundleDigest,
            argumentsDigest=identity, argumentsJson=shown,
            destination=destination,
            createdAt=now, expiresAt=min(self.current.deadline, now + timedelta(seconds=APPROVAL_SECONDS)),
            state="pending", challenge=None, challengeGeneration=0, decidedAt=None,
        )
        pending.spend = quote_for_call(
            self.current, pending,
            run_account((await self.service.owner(self.owner_id)).value, self.current),
            definition=self._executor.get(draft.tool) if self._executor else None, now=now,
        )
        updated = self.current.model_copy(update={
            "turn": state, "draft": pending, "status": "awaiting_approval",
            "operationId": None, "operationState": "idle", "leaseId": None,
            "leaseExpiresAt": None, "wakeRevision": self.current.wakeRevision + 1,
        }, deep=True)
        await self.save(updated)

    async def capture_memory(self, preference: MemoryPreference, references: list[MemoryReference]) -> None:
        prior = self.current.memoryContext
        if prior is not None and prior.preference != preference:
            raise AutomationError("context_revoked", "The memory preference changed.")
        merged = {reference.id: reference for reference in prior.references} if prior else {}
        for reference in references:
            if reference.id in merged and merged[reference.id] != reference:
                raise AutomationError("context_revoked", "A previously read memory changed.")
            merged[reference.id] = reference
        binding = MemoryContextBinding(preference=preference, references=list(merged.values()))
        try:
            await self.service.state.memory.validate_context_references(
                self.owner_id, preference, binding.references,
            )
        except (MemoryPreferenceConflict, MemoryPreferenceUnavailable) as exc:
            raise AutomationError("context_revoked", "Memory context could not be confirmed.") from exc
        await self.save(self.current.model_copy(update={"memoryContext": binding}, deep=True))

    async def before_effect(self, effect: str) -> None:
        await self.check_current()
        if self._local_zero.get() or self.current.limits.maxSpendMicroUsd is not None:
            raise AutomationError("spend_unbounded", "This operation does not admit ambient metered effects.")
        if self.bundle.safeOnly:
            raise AutomationError("unsafe_effect", "Safe scheduled work cannot perform an ambient mutation.")

    async def before_dispatch(
        self, surface: Surface, payload: dict[str, Any], *, deployment: str | None, target: str | None,
    ) -> str:
        await self.check_current()
        if self._local_zero.get():
            raise AutomationError("zero_cost_effect", "An exact local-only operation cannot dispatch a metered request.")
        if self._operation is None or self.current.operationState != "dispatched":
            raise AutomationError("operation_missing", "Egress has no claimed workflow operation.")
        self._dispatch_sequence += 1
        identifier = stable_id(self.owner_id, self.current.runId, self._operation, str(self._dispatch_sequence))
        model_id = self.bundle.modelId
        descriptor = UsageTarget(deployment=deployment, target=deployment or surface)
        for entry in self.service.state.catalog.models:
            option = next((option for option in entry.options if option.deploymentName == deployment), None)
            if option is not None:
                model_id = entry.id
                descriptor = UsageTarget.from_deployment(option)
                break
        prices = self.service.state.usage.pricing.snapshot_token_prices(model_id)
        rate = prices.rate(model_id)
        bound = reservation_bounds(
            surface, payload, deployment=deployment, catalog=self.service.state.catalog,
            pricing=prices, attempts=current_attempt_envelope(
                surface, payload, deployment=deployment, target=target, owner=self.owner_id,
            ),
        ) if self.current.limits.maxSpendMicroUsd is not None else None

        def claim(owner: AutomationOwner, now: datetime) -> None:
            handle = owner.runs[self.current.runId]
            if handle.terminal or not handle.active or identifier in owner.effects:
                raise AutomationError("operation_replayed", "The dispatch cannot be repeated.")
            if bound is not None and any(
                effect.runId == self.current.runId and effect.operationId == self._operation
                and effect.category == "dispatch" for effect in owner.effects.values()
            ):
                raise AutomationError("operation_replayed", "The capped operation already claimed its one dispatch.")
            if handle.dispatches >= self.current.limits.maxApplicationDispatches:
                raise AutomationError("dispatch_limit", "The run's application-dispatch limit was reached.")
            if surface == "chat":
                if handle.modelCalls >= self.current.limits.maxModelCalls:
                    raise AutomationError("model_limit", "The run's model-call limit was reached.")
                output = next((payload[key] for key in (
                    "max_output_tokens", "max_completion_tokens", "max_tokens",
                ) if key in payload), None)
                if type(output) is not int or not 0 < output <= self.current.limits.maxOutputTokens:
                    raise AutomationError("output_limit", "The adapted request does not carry the run's output bound.")
                handle.modelCalls += 1
            handle.dispatches += 1
            money = reserve_money(
                owner, self.current.runId, bound,
                approval_spend_digest=(
                    self.current.draft.spend.quoteDigest
                    if self.current.draft is not None and self.current.draft.spend is not None else None
                ),
            ) if bound is not None else None
            usage = UsageRecord(
                id="wf-use-" + identifier, userId=self.owner_id, sessionId=self.current.sessionId,
                provider=descriptor.provider if surface in {"chat", "embedding"} else surface,
                model=model_id if surface in {"chat", "embedding"} else surface,
                deployment=descriptor.deployment, target=descriptor.target,
                region=descriptor.region, dataZone=descriptor.dataZone,
                agent=f"workflow:{self.bundle.workflow.name}", status="error",
                providerCompleted=False, workflowDispatchClaimed=True, calls=1,
                usageKnown=False, usageComplete=False, createdAt=now,
                priceVersion=prices.version, currency=prices.currency,
                priceInputPer1M=rate.input_per_1m if rate else None,
                priceOutputPer1M=rate.output_per_1m if rate else None,
            )
            owner.effects[identifier] = EffectIntent(
                id=identifier, runId=self.current.runId, sessionId=self.current.sessionId,
                operationId=self._operation or "", category="dispatch",
                payloadDigest=digest({"surface": surface, "payload": payload, "deployment": deployment, "target": target}),
                state="dispatched", startedAt=now, resultDigest=None, usage=usage, delivered=False,
                money=money,
            )

        try:
            await self.service.mutate_owner(self.owner_id, claim)
        except AutomationError as exc:
            if bound is not None and exc.code in {"spend_limit", "spend_unbounded", "budget_stopped"}:
                await self.refuse_undispatched_model(exc.code)
            raise
        return identifier

    async def refuse_undispatched_model(self, reason: str) -> None:
        """Close only a typed admission refusal before any recorded dispatch."""
        operation = self._operation
        if operation is None or ":model:" not in operation:
            return
        identity = stable_id(self.owner_id, self.current.runId, operation)

        def refuse(owner: AutomationOwner, now: datetime) -> None:
            logical = owner.effects.get(identity)
            if logical is None or logical.state != "dispatched":
                raise AutomationError("accounting_changed", "The model's dispatch ownership changed.")
            if any(
                effect.runId == self.current.runId and effect.operationId == operation
                and effect.category == "dispatch" for effect in owner.effects.values()
            ):
                return
            logical.state = "complete"
            logical.resultDigest = digest({"notDispatched": reason})

        updated = await self.service.mutate_owner(self.owner_id, refuse)
        if updated.value.effects[identity].state == "complete":
            await self.save(self.current.model_copy(update={"operationState": "complete"}, deep=True))

    async def authorize_dispatch(self, ticket: str) -> None:
        await self.check_current()
        if self.current.limits.maxSpendMicroUsd is not None:
            owner = await self.service.owner(self.owner_id)
            effect = owner.value.effects.get(ticket)
            if effect is None or effect.money is None or effect.money.phase != "held" or effect.usage is None:
                raise AutomationError("accounting_changed", "The dispatch has no current monetary hold.")
            bound = effect.money.bounds
            if not same_prices(
                self.service.state.usage.pricing, effect.usage.model, version=bound.priceVersion,
                input_rate=bound.inputRate, output_rate=bound.outputRate,
            ):
                raise AutomationError("spend_quote_stale", "The price snapshot changed before dispatch.")

    async def after_dispatch(
        self, ticket: str, *, usage: dict[str, Any] | None, completed: bool, outcome: str,
    ) -> None:
        def finish(owner: AutomationOwner, now: datetime) -> None:
            effect = owner.effects.get(ticket)
            identity = digest({"completed": completed, "outcome": outcome, "usage": usage})
            if effect is None or effect.runId != self.current.runId:
                raise AutomationError("accounting_changed", "The dispatch receipt has no matching intent.")
            if effect.state != "dispatched":
                if effect.resultDigest == identity:
                    return
                raise AutomationError("accounting_changed", "The dispatch outcome is already recorded differently.")
            record = effect.usage
            if record is None:
                raise AutomationError("accounting_changed", "The dispatch lost its price/usage snapshot.")
            tokens = TokenUsage.parse(usage)
            record.status = "complete" if completed else "cancelled" if outcome == "cancelled" else "error"
            record.providerCompleted = completed
            record.usageKnown = tokens.known
            record.usageComplete = tokens.complete
            record.promptTokens = tokens.prompt if tokens.known else None
            record.completionTokens = tokens.completion if tokens.known else None
            record.totalTokens = tokens.total if tokens.known else None
            record.billable = completed and tokens.known and tokens.complete
            if record.billable and record.priceInputPer1M is not None and record.priceOutputPer1M is not None:
                pricing = PricingBook(
                    {record.model: PriceRate(record.priceInputPer1M, record.priceOutputPer1M)},
                    currency=record.currency, version=record.priceVersion,
                )
                estimate = pricing.estimate(
                    record.model, prompt_tokens=tokens.prompt, completion_tokens=tokens.completion,
                )
                record.costKnown = estimate.known
                record.estCostMicroUsd = estimate.micro_usd
                record.pricingBasis = "input_output_tokens"
            if effect.money is not None:
                settle_money(owner, effect, completed=completed, usage=usage, outcome=outcome)
            effect.state = "complete" if completed and (
                effect.money is None or effect.money.phase == "settled"
            ) else "unknown"
            effect.resultDigest = identity

        await self.service.mutate_owner(self.owner_id, finish)

    async def capture_usage(self, record: UsageRecord) -> None:
        if record.userId != self.owner_id or record.sessionId != self.current.sessionId:
            raise AutomationError("accounting_owner_mismatch", "Usage cannot cross workflow owners.")
        current = await self.service.owner(self.owner_id)
        matching = [
            item for item in current.value.effects.values()
            if item.runId == self.current.runId and item.operationId == self._operation
            and item.category == "dispatch"
        ]
        if not matching:
            raise AutomationError("unobserved_dispatch", "Service usage has no recorded application dispatch.")
        if record.usageKnown and (
            sum(item.usage.totalTokens or 0 for item in matching if item.usage) != record.totalTokens
        ):
            raise AutomationError("accounting_changed", "Service usage disagrees with recorded dispatch evidence.")
