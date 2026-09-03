"""Turn an agent-runtime step trace into an :mod:`ai4ia_api.receipts` receipt.

Deliberately a **separate** serializer from :mod:`ai4ia_api.agents.activity`
rather than an extension of it. ``ActivityStep`` is the coarse, glanceable panel
("Searched the web"), and widening it to carry arguments and results would
change what every existing surface renders and persists. The two answer
different questions and are allowed to disagree about how much detail is
appropriate:

* ``activity`` — a label and a fixed reason category, nothing content-bearing.
* ``receipt``  — the call's arguments and result, bounded and re-redacted.

``AgentStep.arguments``/``AgentStep.result`` are already credential-redacted by
the runtime (``redact_obj``), which is why they can be reused here at all. This
module still re-redacts through :func:`~ai4ia_api.receipts.json_payload`, on the
same defense-in-depth principle ``activity`` applies to the tool name: this is
the boundary that actually writes to Cosmos, so it does not delegate the
guarantee to whoever built the step.

The ``tool_start`` marker is excluded. It is the pre-execution "running X…"
indicator, not an outcome, and a receipt records what happened.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..receipts import (
    MAX_DETAIL_CHARS,
    ExecutionReceipt,
    ReceiptRuntime,
    ReceiptToolCall,
    build_receipt,
    json_payload,
    safe_tool_label,
)
from ..safety import MessageSafety, attributed_safety
from ..usage.models import TokenUsage
from .runtime import AgentStep, DelegatedRunTrace

# Runtime step kind -> receipt outcome. ``tool_start`` and ``final`` are absent
# on purpose: neither is a finalized tool call.
_OUTCOMES: dict[str, str] = {
    "tool_result": "result",
    "delegate": "delegate",
    "tool_denied": "denied",
    "tool_error": "error",
}


def _detail(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed[:MAX_DETAIL_CHARS] if collapsed else None


def receipt_tool_calls(steps: list[AgentStep] | None) -> list[ReceiptToolCall]:
    """Every finalized tool call in ``steps``, in execution order."""
    calls: list[ReceiptToolCall] = []
    for step in steps or []:
        outcome = _OUTCOMES.get(getattr(step, "kind", ""))
        if outcome is None:
            continue
        calls.append(
            ReceiptToolCall(
                tool=safe_tool_label(getattr(step, "tool", None)),
                outcome=outcome,
                detail=_detail(getattr(step, "detail", None)),
                arguments=(
                    json_payload(step.arguments) if step.arguments is not None else None
                ),
                result=(
                    json_payload(step.result) if step.result is not None else None
                ),
            )
        )
    return calls


def delegation_receipts(
    traces: list[DelegatedRunTrace] | None,
    *,
    provider: str | None = None,
) -> list[ExecutionReceipt]:
    """Build one bounded child receipt per successful linked-agent run."""
    return [
        build_receipt(
            runtime=ReceiptRuntime(agent=trace.agent),
            prompt_messages=trace.effective_prompt,
            model_requests=trace.model_requests,
            offered=trace.offered_tools,
            calls=receipt_tool_calls(trace.steps),
            iterations=trace.iterations,
            usage=trace.usage,
            safety=attributed_safety(trace.safety, provider),
            status=trace.status,  # pyright: ignore[reportArgumentType]
            partial=trace.partial,
        )
        for trace in traces or []
    ]


@dataclass
class ReceiptDraft:
    """The turn-invariant half of a receipt, assembled once before the model runs.

    Everything here is known at prompt-assembly time and does not change with
    the outcome, so each persistence path (streaming, non-streaming, success,
    error, cancellation, fallback) can call :meth:`build` with only the outcome
    it holds and get a consistent receipt. That is what keeps the paths from
    drifting: they cannot each decide what a receipt contains.
    """

    correlation_id: str | None = None
    runtime: ReceiptRuntime = field(default_factory=ReceiptRuntime)
    prompt_messages: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[tuple[str, str, bool]] = field(default_factory=list)
    block_sources: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    dropped_history_messages: int = 0
    dropped_context_blocks: list[str] = field(default_factory=list)
    offered: list[dict[str, Any]] = field(default_factory=list)
    approvals_granted: int = 0

    def build(
        self,
        *,
        steps: list[AgentStep] | None = None,
        iterations: int = 0,
        status: Literal["complete", "incomplete", "error", "cancelled"] = "complete",
        partial: bool = False,
        approvals_requested: int = 0,
        dropped_history_messages: int = 0,
        offered: list[dict[str, Any]] | None = None,
        prompt_messages: list[dict[str, Any]] | None = None,
        usage: TokenUsage | None = None,
        safety: MessageSafety | None = None,
        delegations: list[DelegatedRunTrace] | None = None,
        model_requests: list[list[dict[str, Any]]] | None = None,
    ) -> ExecutionReceipt:
        """Finalize the receipt for one outcome. Never raises.

        ``offered`` overrides the draft's own list with what the runtime reports
        it actually advertised. The agent loop resolves its registry schema
        itself, so only it knows the full offer; re-deriving that here would be
        a second implementation of the same decision, and the one that drifted
        would be the one nobody was watching.

        A receipt is a record of a turn, not a participant in it: if assembling
        one somehow fails, the turn still completes and the failure is reported
        *in* the receipt (``partial`` plus a ``receipt_build_failed`` note)
        rather than becoming the turn's error.
        """
        try:
            return build_receipt(
                correlation_id=self.correlation_id,
                runtime=self.runtime,
                prompt_messages=(
                    self.prompt_messages
                    if prompt_messages is None
                    else prompt_messages
                ),
                blocks=self.blocks,
                block_sources=self.block_sources,
                dropped_history_messages=(
                    self.dropped_history_messages + max(0, dropped_history_messages)
                ),
                dropped_context_blocks=self.dropped_context_blocks,
                offered=self.offered if offered is None else offered,
                calls=receipt_tool_calls(steps),
                approvals_requested=approvals_requested,
                approvals_granted=self.approvals_granted,
                usage=usage,
                safety=safety,
                delegations=delegation_receipts(
                    delegations,
                    provider=safety.provider if safety is not None else None,
                ),
                model_requests=model_requests,
                iterations=iterations,
                status=status,
                partial=partial,
            )
        except Exception:  # noqa: BLE001 - a receipt must never break a turn
            return ExecutionReceipt(
                correlationId=self.correlation_id,
                runtime=self.runtime,
                status=status,
                partial=True,
                notes=["receipt_build_failed"],
            )
