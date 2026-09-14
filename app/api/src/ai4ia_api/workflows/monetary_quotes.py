"""Immutable exact-call spend evidence, not an executable tool or price grant."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..agents.consent import tool_contract_hash
from ..agents.tool_exec import ToolDefinition, builtin_tools
from ..usage.pricing import PricingBook
from .automation_common import AutomationError, digest, utc
from .monetary_models import ApprovalSpend, BudgetView, OperationSpend, RunMoney, budget_identity

if TYPE_CHECKING:
    from .automation_models import AutomationOwner, InvocationDraft, WorkflowCheckpoint


def run_account(owner: AutomationOwner, state: WorkflowCheckpoint) -> RunMoney | None:
    handle = owner.runs.get(state.runId)
    if (
        handle is None or owner.userId != state.userId or owner.epoch != state.ownerEpoch
        or handle.fingerprint != state.fingerprint
    ):
        raise AutomationError("budget_changed", "The run's monetary ownership binding changed.")
    account = handle.money
    limit = state.limits.maxSpendMicroUsd
    if limit is None:
        if account is not None:
            raise AutomationError("budget_changed", "An uncapped run cannot acquire a monetary contract.")
        return None
    if (
        state.limits.spendMode != "usd_app_meter" or account is None
        or account.limitMicroUsd != limit
        or account.budgetId != budget_identity(
            state.userId, state.ownerEpoch, state.runId, state.fingerprint, limit,
        )
    ):
        raise AutomationError("budget_changed", "The run's immutable USD application-meter limit changed.")
    return account


def operation_impact(definition: ToolDefinition | None, contract: str) -> OperationSpend:
    if definition is not None:
        for expected in builtin_tools():
            if (
                definition.handler is expected.handler and definition.spec == expected.spec
                and definition.parameters == expected.parameters
                and definition.consent_metadata == expected.consent_metadata
                and contract == tool_contract_hash(
                    expected.spec, expected.parameters, description=expected.spec.description,
                    metadata=expected.consent_metadata,
                )
            ):
                return OperationSpend(
                    coverage="bounded", amountMicroUsd=0, basis="local-no-metered-effects-v1",
                    reason="repository-local-handler", bounds=None, localContractDigest=contract,
                )
    return OperationSpend(
        coverage="unknown", amountMicroUsd=None, basis="unbounded-operation",
        reason="meter-coverage-unknown", bounds=None, localContractDigest=None,
    )


def approval_binding(state: WorkflowCheckpoint, draft: InvocationDraft) -> str:
    bundle = state.bundle
    if bundle is None:
        raise AutomationError("context_revoked", "The exact approval source is unavailable.")
    return digest({
        "owner": state.userId, "ownerEpoch": state.ownerEpoch,
        "session": state.sessionId, "deletionEpoch": state.deletionEpoch,
        "run": state.runId, "fingerprint": state.fingerprint,
        "source": bundle.source, "approvedBundle": bundle.approvedBundleDigest,
        "bundle": bundle.bundleDigest, "operation": draft.operationId,
        "draft": draft.id, "tool": draft.tool, "canonicalTool": draft.canonicalTool,
        "contract": draft.contractDigest, "destination": draft.destination,
        "arguments": draft.argumentsDigest, "expiresAt": utc(draft.expiresAt).isoformat(),
        "limits": state.limits.model_dump(mode="json"),
    })


def quote_for_call(
    state: WorkflowCheckpoint, draft: InvocationDraft, account: RunMoney | None, *,
    definition: ToolDefinition | None, now: datetime,
) -> ApprovalSpend:
    impact = operation_impact(definition, draft.contractDigest)
    if account is not None and (impact.coverage != "bounded" or account.blocked):
        raise AutomationError("spend_unbounded", "This exact operation has no supported monetary bound.")
    return ApprovalSpend.create(
        binding=approval_binding(state, draft), now=now, expires=draft.expiresAt,
        impact=impact, budget=BudgetView.from_account(account),
    )


def require_quote_current(
    state: WorkflowCheckpoint, draft: InvocationDraft, account: RunMoney | None, *,
    definition: ToolDefinition | None, now: datetime, challenged: bool = False,
) -> None:
    quote = draft.spend
    if quote is None:
        if account is not None:
            raise AutomationError("spend_quote_missing", "The capped approval has no immutable spend evidence.")
        return
    if (
        quote.bindingDigest != approval_binding(state, draft)
        or quote.quoteDigest != quote.content_digest()
        or utc(now) >= utc(quote.expiresAt)
    ):
        raise AutomationError("spend_quote_changed", "The spend quote no longer matches this exact call.")
    if (
        quote.budget != BudgetView.from_account(account)
        or quote.impact != operation_impact(definition, draft.contractDigest)
        or (account is not None and account.blocked)
    ):
        raise AutomationError(
            "spend_quote_stale", "The budget or spend coverage changed. Review a fresh quote for this exact call.",
        )
    if challenged and draft.challengeSpendDigest != quote.quoteDigest:
        raise AutomationError("spend_quote_changed", "The one-time challenge belongs to different spend evidence.")


def same_prices(pricing: PricingBook, model: str, *, version: str | None, input_rate: str | None, output_rate: str | None) -> bool:
    rate = pricing.rate(model)
    return (
        pricing.currency == "USD" and pricing.version == version and rate is not None
        and str(rate.input_per_1m) == input_rate and str(rate.output_per_1m) == output_rate
    )
