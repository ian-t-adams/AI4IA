"""Execution receipts: what was actually sent, offered, and run for one turn.

The activity trace (:mod:`ai4ia_api.agents.activity`) answers "which tools ran?"
in one coarse line each, deliberately carrying no arguments or results. That is
the right shape for a glanceable panel and the wrong shape for the only question
an owner reviewing their own conversation actually needs answered: *what was
supplied to the model, what was it allowed to do, and what did it do with it?*

An execution receipt records exactly that, and nothing else:

* **Runtime** — correlation id, model id, resolved deployment/region/SKU/data
  zone/residency, the provider API surface, and the agent the turn was routed to.
* **Prompt** — the effective message list as a bounded canonical snapshot, plus
  the context blocks (recalled memory, session documents, library excerpts, the
  rolling summary) with whether each was actually *admitted* to the prompt.
* **Offer vs. use** — every tool schema offered to the model, separately from the
  tool calls it finalized, with each call's arguments and result.
* **Bounds** — what was dropped, what was truncated, and whether the turn ended
  partial.

Three properties are load-bearing.

**No hidden reasoning.** A receipt reports server-owned facts about the request
and the governed calls it produced. It never claims to expose a model's internal
deliberation, and it carries no field for one. The provider does not hand this
app a chain of thought, so inventing a place to put one would be a claim the
system cannot support.

**"Raw" means redacted-canonical, never unredacted.** Every payload here is run
through the shared credential redactor (:func:`~ai4ia_api.agents.tools.redact_obj`)
and serialized canonically. Tool arguments/results arrive already redacted on
:class:`~ai4ia_api.agents.runtime.AgentStep`; this module redacts again anyway,
because this is the persistence boundary and it must not depend on an upstream
caller having remembered. Provider exception bodies and free-form log output are
never included.

**Bounded, so a tool cannot inflate a Cosmos message.** Every payload is capped
individually, the whole receipt is capped in total, and anything cut is reported
as cut. A hostile tool result that pads itself to a megabyte lands here as a
digest, an original byte length, and ``truncated: true`` -- so the fact of the
padding survives while the padding does not.

Immutability is the same bargain :mod:`ai4ia_api.citations` already makes: the
snapshot is what was supplied *at the time*, so deleting the source memory (or
re-chunking a library document) does not rewrite prior conversation receipts,
exactly as it does not rewrite the prior messages that quoted it.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from .agents.tools import is_safe_tool_name, redact, redact_obj
from .agents.consent import ApprovalSource, ToolConsentSummary
from .safety import MessageSafety
from .usage.models import TokenUsage

# Receipt schema generation. Bumped when the shape changes in a way a reader
# must notice; old rows keep their own version so they are never misread as new.
RECEIPT_VERSION = 1

# --- Hard bounds --------------------------------------------------------------
# A receipt rides on the assistant message document, so every one of these is a
# ceiling on what a hostile tool result or an oversized prompt can add to a
# Cosmos row. They are generous for honest traffic and absolute for the rest.

# Per-payload cap (one tool's arguments, one tool's result, one prompt message,
# one context block).
MAX_PAYLOAD_BYTES = 2_048
# How many entries of each list survive. Anything past the cap is counted, not
# silently dropped: every list carries its own pre-bound total.
MAX_PROMPT_MESSAGES = 40
MAX_CONTEXT_BLOCKS = 12
MAX_CONTEXT_SOURCES_PER_BLOCK = 16
MAX_TOOLS_OFFERED = 64
MAX_TOOL_CALLS = 16
MAX_DELEGATIONS = 4
MAX_LATER_MODEL_REQUESTS = 7
# Total serialized receipt budget. Enforced after assembly by shedding payload
# bodies (digests and byte counts are kept) in a fixed order, so two identical
# turns always shed identically.
MAX_RECEIPT_BYTES = 32_768

# Bounds on short descriptive strings that originate upstream (tool descriptions,
# step detail categories).
MAX_DESCRIPTION_CHARS = 200
MAX_DETAIL_CHARS = 80
MAX_LABEL_CHARS = 64

_TRUNCATION_MARK = "…[truncated]"

# The fixed sentinel the runtime/activity boundary uses for a tool name that is
# not safe to surface verbatim. Repeated here because this is an independent
# persistence boundary (see agents/activity.py for the same reasoning).
_UNKNOWN_TOOL = "unknown_tool"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    """Stable JSON for an already-redacted value.

    ``sort_keys`` makes two structurally identical payloads serialize
    identically, so the digest is a property of the payload rather than of the
    order a dict happened to be built in. ``default=str`` means a value the JSON
    encoder cannot represent degrades to its string form instead of raising --
    a receipt must never be the reason a turn fails.
    """
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):  # pragma: no cover - default=str covers these
        return repr(value)


def _bound_text(text: str) -> tuple[str, int, bool]:
    """Cap ``text`` at :data:`MAX_PAYLOAD_BYTES`, reporting the ORIGINAL size."""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_PAYLOAD_BYTES:
        return text, len(encoded), False
    kept = encoded[:MAX_PAYLOAD_BYTES].decode("utf-8", "ignore")
    return kept + _TRUNCATION_MARK, len(encoded), True


class ReceiptPayload(BaseModel):
    """One bounded, credential-redacted payload plus proof of what was cut.

    ``sha256`` and ``bytes`` are taken over the **full redacted** payload, not
    over ``text``. That is the whole point: a 400 KB tool result is recorded as a
    digest, its real byte length, and ``truncated`` -- so "this was enormous" is
    still a checkable fact after the bytes themselves are gone.
    """

    text: str = ""
    sha256: str = ""
    bytes: int = 0
    truncated: bool = False

    def shed(self) -> ReceiptPayload:
        """This payload with its body dropped, keeping the evidence about it.

        Used only by the whole-receipt size bound. The digest and original byte
        length survive, so shedding narrows what a reader can see without
        changing what the receipt asserts.
        """
        if not self.text:
            return self
        return ReceiptPayload(
            text="", sha256=self.sha256, bytes=self.bytes, truncated=True
        )


def text_payload(value: str | None) -> ReceiptPayload:
    """Receipt for a plain string (a prompt message, a context block)."""
    redacted = redact(value or "")
    kept, size, truncated = _bound_text(redacted)
    return ReceiptPayload(
        text=kept, sha256=_sha256(redacted), bytes=size, truncated=truncated
    )


def json_payload(value: Any) -> ReceiptPayload:
    """Receipt for a JSON-like value (tool arguments, a tool result).

    Redacts again even though :class:`~ai4ia_api.agents.runtime.AgentStep`
    already carries redacted values: :func:`~ai4ia_api.agents.tools.redact_obj`
    is idempotent, and this is the boundary that actually persists, so it does
    not delegate that guarantee to its caller.
    """
    canonical = _canonical(redact_obj(value))
    kept, size, truncated = _bound_text(canonical)
    return ReceiptPayload(
        text=kept, sha256=_sha256(canonical), bytes=size, truncated=truncated
    )


def _short(value: str | None, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(redact(value).split())
    if not collapsed:
        return None
    return collapsed[:limit]


def safe_tool_label(name: str | None) -> str:
    """A tool name that is safe to persist verbatim, or the fixed sentinel."""
    if isinstance(name, str) and is_safe_tool_name(name):
        return name
    return _UNKNOWN_TOOL


class ReceiptPromptMessage(BaseModel):
    """One message of the effective prompt, exactly as it was sent."""

    role: str
    content: ReceiptPayload = Field(default_factory=ReceiptPayload)
    toolCalls: ReceiptPayload | None = None
    toolCallId: str | None = None


class ReceiptContextSource(BaseModel):
    """Durable identity/version metadata for context behind one block."""

    id: str
    version: str | None = None
    updatedAt: str | None = None
    kind: str | None = None
    documentId: str | None = None
    label: str | None = None
    contentSha256: str | None = None
    score: float | None = None


class ReceiptContextBlock(BaseModel):
    """One optional context block and whether it reached the model.

    ``admitted`` is the fact that matters. A block that was built but displaced
    by the prompt budget never influenced the answer, and a receipt that showed
    it without saying so would describe a turn that did not happen. Content is
    carried only for admitted blocks; a dropped one is recorded by kind alone.
    """

    # memory | documents | library | summary | notice
    kind: str
    admitted: bool = False
    content: ReceiptPayload | None = None
    sources: list[ReceiptContextSource] = Field(default_factory=list)
    sourceCount: int = 0


class ReceiptToolOffer(BaseModel):
    """One tool schema that was advertised to the model this turn.

    Offered is not invoked. Keeping the two lists separate is the point: "the
    model could have sent mail and chose not to" and "the model was never able
    to send mail" are different facts about a turn, and only one of them is
    visible from the calls that happened.
    """

    name: str
    description: str | None = None
    # Digest of the advertised parameter schema, so a changed tool contract is
    # detectable without storing every schema verbatim.
    parametersSha256: str = ""


class ReceiptToolCall(BaseModel):
    """One finalized tool call: what was asked, and what came back."""

    tool: str
    # result | delegate | denied | error
    outcome: str
    # Fixed, bounded reason/category the runtime itself set (e.g.
    # ``approval_required``, ``budget_exceeded``). Never derived from arguments.
    detail: str | None = None
    arguments: ReceiptPayload | None = None
    result: ReceiptPayload | None = None
    approval: ApprovalSource | None = None
    consentId: str | None = Field(default=None, max_length=64)
    callId: str | None = Field(default=None, max_length=128)


class ReceiptRuntime(BaseModel):
    """Where the turn actually ran."""

    modelId: str | None = None
    deployment: str | None = None
    region: str | None = None
    sku: str | None = None
    dataZone: str | None = None
    # Processing scope, which is NOT the same claim as ``dataZone`` -- see
    # ``catalog.DeploymentOption.residency``.
    residency: str | None = None
    # Provider API surface (``chat``, ``responses``, ``anthropic``, ...).
    api: str | None = None
    agent: str | None = None
    instructionSource: str | None = None
    instructionSha256: str | None = None
    agentConfigSha256: str | None = None


class ReceiptUsage(BaseModel):
    """Provider-reported usage for the model calls in this turn."""

    known: bool = False
    complete: bool = False
    calls: int = 0
    promptTokens: int | None = None
    completionTokens: int | None = None
    totalTokens: int | None = None


class ReceiptSafetySummary(BaseModel):
    """Assessment coverage without duplicating every sibling safety signal."""

    status: str = "unavailable"
    provider: str | None = None
    mode: str | None = None
    coverage: list[str] = Field(default_factory=list)
    signalCount: int = 0
    truncated: bool = False


class ReceiptModelRequest(BaseModel):
    """One post-initial model request in a tool loop."""

    iteration: int
    prompt: list[ReceiptPromptMessage] = Field(default_factory=list)
    promptMessageCount: int = 0
    promptBytes: int = 0


class ExecutionReceipt(BaseModel):
    """The full, bounded record of one assistant turn's execution.

    Additive and optional on the message exactly like ``steps``/``safety``:
    existing Cosmos documents lack the field and deserialize to ``None``, so
    there is no migration and no reinterpretation of an older row.
    """

    version: int = RECEIPT_VERSION
    correlationId: str | None = None
    runtime: ReceiptRuntime = Field(default_factory=ReceiptRuntime)

    # --- Prompt / context ---
    prompt: list[ReceiptPromptMessage] = Field(default_factory=list)
    # Message count BEFORE the receipt's own list bound, so a capped list still
    # reports how much prompt there really was.
    promptMessageCount: int = 0
    # Redacted byte total across the whole effective prompt.
    promptBytes: int = 0
    contextBlocks: list[ReceiptContextBlock] = Field(default_factory=list)
    # History turns the prompt budget displaced before the model was called.
    droppedHistoryMessages: int = 0
    # Kinds of optional context block that did not fit.
    droppedContextBlocks: list[str] = Field(default_factory=list)

    # --- Offer vs. use ---
    toolsOffered: list[ReceiptToolOffer] = Field(default_factory=list)
    toolsOfferedCount: int = 0
    toolCalls: list[ReceiptToolCall] = Field(default_factory=list)
    toolCallCount: int = 0

    # --- Governance ---
    # Per-invocation approval prompts raised this turn, and grants redeemed into
    # it. Recorded where available; both stay 0 on a turn with no gated call.
    approvalsRequested: int = 0
    approvalsGranted: int = 0
    autoApprovedToolCalls: int = 0
    toolConsent: ToolConsentSummary | None = None
    usage: ReceiptUsage = Field(default_factory=ReceiptUsage)
    safety: ReceiptSafetySummary = Field(default_factory=ReceiptSafetySummary)
    delegations: list[ExecutionReceipt] = Field(default_factory=list)
    # Iteration 1 is the top-level ``prompt``. These are iterations 2+.
    modelRequests: list[ReceiptModelRequest] = Field(default_factory=list)

    # --- Outcome ---
    iterations: int = 0
    # complete | error | cancelled
    status: str = "complete"
    # True when the turn did not finish cleanly, so the receipt describes work
    # that stopped partway rather than a completed exchange.
    partial: bool = False
    # True when anything in this receipt was shortened by a bound.
    truncated: bool = False
    # Fixed, bounded machine-readable markers for why (``prompt_capped``,
    # ``tool_calls_capped``, ``receipt_size_capped``, ``receipt_build_failed``).
    notes: list[str] = Field(default_factory=list)


def _note(receipt: ExecutionReceipt, marker: str) -> None:
    if marker not in receipt.notes:
        receipt.notes.append(marker)
    receipt.truncated = True


def enforce_receipt_budget(receipt: ExecutionReceipt) -> ExecutionReceipt:
    """Shed payload bodies until the receipt fits :data:`MAX_RECEIPT_BYTES`.

    Deterministic and ordered: tool results first (the largest and least
    trustworthy), then tool arguments, then prompt bodies, then context bodies.
    Digests and byte counts are never shed, so the receipt keeps asserting the
    same facts with less of the text behind them.
    """

    def size() -> int:
        # Durable activities may use ASCII-escaped JSON with spaces rather than
        # Pydantic's compact UTF-8 encoding. Bound the larger wire representation.
        return len(json.dumps(receipt.model_dump(mode="json"), ensure_ascii=True).encode("ascii"))

    if size() <= MAX_RECEIPT_BYTES:
        return receipt

    for call in receipt.toolCalls:
        if call.result is not None:
            call.result = call.result.shed()
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    for call in receipt.toolCalls:
        if call.arguments is not None:
            call.arguments = call.arguments.shed()
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    for message in receipt.prompt:
        message.content = message.content.shed()
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    for block in receipt.contextBlocks:
        if block.content is not None:
            block.content = block.content.shed()
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    for nested in receipt.delegations:
        for call in nested.toolCalls:
            if call.result is not None:
                call.result = call.result.shed()
            if call.arguments is not None:
                call.arguments = call.arguments.shed()
        for message in nested.prompt:
            message.content = message.content.shed()
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    for request in receipt.modelRequests:
        for message in request.prompt:
            message.content = message.content.shed()
            if message.toolCalls is not None:
                message.toolCalls = message.toolCalls.shed()
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    if receipt.modelRequests:
        receipt.modelRequests = []
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    if receipt.delegations:
        receipt.delegations = []
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    for block in receipt.contextBlocks:
        if block.sources:
            block.sources = []
        if size() <= MAX_RECEIPT_BYTES:
            _note(receipt, "receipt_size_capped")
            return receipt
    # Last resort: descriptions are the only remaining free text.
    for offer in receipt.toolsOffered:
        offer.description = None
    _note(receipt, "receipt_size_capped")
    return receipt


def prompt_snapshot(
    messages: list[dict[str, Any]] | None,
) -> tuple[list[ReceiptPromptMessage], int, int, bool]:
    """Bounded snapshot of the effective prompt.

    Returns ``(messages, total count, total redacted bytes, capped)``. The byte
    total is computed over EVERY message, including ones the list bound drops,
    so "how much was actually sent" survives the bound.
    """
    source = list(messages or [])
    total_bytes = 0
    snapshot: list[ReceiptPromptMessage] = []
    for index, message in enumerate(source):
        role = _short(str(message.get("role", "")), MAX_LABEL_CHARS) or "unknown"
        raw = message.get("content")
        payload = (
            text_payload(raw) if isinstance(raw, str) or raw is None
            else json_payload(raw)
        )
        tool_calls_payload = (
            json_payload(message.get("tool_calls"))
            if message.get("tool_calls") is not None
            else None
        )
        tool_call_id = _short(
            message.get("tool_call_id"),
            MAX_LABEL_CHARS,
        )
        total_bytes += payload.bytes
        total_bytes += tool_calls_payload.bytes if tool_calls_payload else 0
        total_bytes += len((tool_call_id or "").encode("utf-8"))
        if index < MAX_PROMPT_MESSAGES:
            snapshot.append(
                ReceiptPromptMessage(
                    role=role,
                    content=payload,
                    toolCalls=tool_calls_payload,
                    toolCallId=tool_call_id,
                )
            )
    return snapshot, len(source), total_bytes, len(source) > MAX_PROMPT_MESSAGES


def model_request_snapshots(
    requests: list[list[dict[str, Any]]] | None,
) -> tuple[list[ReceiptModelRequest], int]:
    """Bounded snapshots for model requests after the initial prompt."""
    source = list(requests or [])[1:]
    out: list[ReceiptModelRequest] = []
    for offset, messages in enumerate(
        source[:MAX_LATER_MODEL_REQUESTS],
        start=2,
    ):
        prompt, count, size, _ = prompt_snapshot(messages)
        out.append(
            ReceiptModelRequest(
                iteration=offset,
                prompt=prompt,
                promptMessageCount=count,
                promptBytes=size,
            )
        )
    return out, len(source)


def context_blocks(
    blocks: list[tuple[str, str, bool]] | None,
    sources: dict[str, list[dict[str, Any]]] | None = None,
) -> list[ReceiptContextBlock]:
    """Turn ``(kind, text, admitted)`` triples into bounded receipt entries."""
    out: list[ReceiptContextBlock] = []
    for kind, text, admitted in list(blocks or [])[:MAX_CONTEXT_BLOCKS]:
        label = _short(kind, MAX_LABEL_CHARS) or "unknown"
        raw_sources = list((sources or {}).get(kind, []))
        bounded_sources: list[ReceiptContextSource] = []
        for raw in raw_sources[:MAX_CONTEXT_SOURCES_PER_BLOCK]:
            if not isinstance(raw, dict):
                continue
            source_id = _short(raw.get("id"), MAX_LABEL_CHARS)
            if source_id is None:
                continue
            raw_content = raw.get("content")
            supplied_digest = _short(raw.get("contentSha256"), 64)
            content_digest = (
                text_payload(raw_content).sha256
                if isinstance(raw_content, str)
                else supplied_digest
            )
            score = raw.get("score")
            bounded_sources.append(
                ReceiptContextSource(
                    id=source_id,
                    version=_short(
                        str(raw["version"]) if raw.get("version") is not None else None,
                        MAX_LABEL_CHARS,
                    ),
                    updatedAt=_short(raw.get("updatedAt"), MAX_DESCRIPTION_CHARS),
                    kind=_short(raw.get("kind"), MAX_LABEL_CHARS),
                    documentId=_short(raw.get("documentId"), MAX_LABEL_CHARS),
                    label=_short(raw.get("label"), MAX_DESCRIPTION_CHARS),
                    contentSha256=content_digest,
                    score=(
                        float(score)
                        if isinstance(score, (int, float)) and not isinstance(score, bool)
                        else None
                    ),
                )
            )
        out.append(
            ReceiptContextBlock(
                kind=label,
                admitted=bool(admitted),
                content=text_payload(text) if admitted else None,
                sources=bounded_sources,
                sourceCount=len(raw_sources),
            )
        )
    return out


def tool_offers(schema: list[dict[str, Any]] | None) -> tuple[list[ReceiptToolOffer], int]:
    """Every advertised tool schema, bounded, plus the pre-bound total."""
    source = list(schema or [])
    offers: list[ReceiptToolOffer] = []
    for entry in source[:MAX_TOOLS_OFFERED]:
        function = entry.get("function") if isinstance(entry, dict) else None
        function = function if isinstance(function, dict) else {}
        offers.append(
            ReceiptToolOffer(
                name=safe_tool_label(function.get("name")),
                description=_short(function.get("description"), MAX_DESCRIPTION_CHARS),
                parametersSha256=_sha256(_canonical(function.get("parameters") or {})),
            )
        )
    return offers, len(source)


def build_receipt(
    *,
    correlation_id: str | None = None,
    runtime: ReceiptRuntime | None = None,
    prompt_messages: list[dict[str, Any]] | None = None,
    blocks: list[tuple[str, str, bool]] | None = None,
    block_sources: dict[str, list[dict[str, Any]]] | None = None,
    dropped_history_messages: int = 0,
    dropped_context_blocks: list[str] | None = None,
    offered: list[dict[str, Any]] | None = None,
    calls: list[ReceiptToolCall] | None = None,
    approvals_requested: int = 0,
    approvals_granted: int = 0,
    tool_consent: ToolConsentSummary | None = None,
    usage: TokenUsage | None = None,
    safety: MessageSafety | None = None,
    delegations: list[ExecutionReceipt] | None = None,
    model_requests: list[list[dict[str, Any]]] | None = None,
    iterations: int = 0,
    status: Literal["complete", "incomplete", "error", "cancelled"] = "complete",
    partial: bool = False,
) -> ExecutionReceipt:
    """Assemble a bounded receipt from data the turn already has.

    Total over its inputs: every argument has a defined meaning when absent, so
    a plain no-tool turn and a failed agent turn both produce a valid receipt
    rather than one path silently producing nothing.
    """
    snapshot, prompt_count, prompt_bytes, prompt_capped = prompt_snapshot(prompt_messages)
    offers, offered_count = tool_offers(offered)
    all_calls = list(calls or [])
    effective_usage = usage or TokenUsage.empty()
    later_requests, later_request_count = model_request_snapshots(model_requests)
    receipt = ExecutionReceipt(
        correlationId=correlation_id,
        runtime=runtime or ReceiptRuntime(),
        prompt=snapshot,
        promptMessageCount=prompt_count,
        promptBytes=prompt_bytes,
        contextBlocks=context_blocks(blocks, block_sources),
        droppedHistoryMessages=max(0, int(dropped_history_messages or 0)),
        droppedContextBlocks=[
            label
            for kind in list(dropped_context_blocks or [])[:MAX_CONTEXT_BLOCKS]
            if (label := _short(kind, MAX_LABEL_CHARS))
        ],
        toolsOffered=offers,
        toolsOfferedCount=offered_count,
        toolCalls=all_calls[:MAX_TOOL_CALLS],
        toolCallCount=len(all_calls),
        approvalsRequested=max(0, int(approvals_requested or 0)),
        approvalsGranted=max(0, int(approvals_granted or 0)),
        autoApprovedToolCalls=sum(
            call.approval in {"session", "run"} and call.outcome != "denied"
            for call in all_calls
        ),
        toolConsent=tool_consent,
        usage=ReceiptUsage(
            known=effective_usage.known,
            complete=effective_usage.complete,
            calls=effective_usage.calls,
            promptTokens=(
                effective_usage.prompt if effective_usage.known else None
            ),
            completionTokens=(
                effective_usage.completion if effective_usage.known else None
            ),
            totalTokens=(
                effective_usage.total if effective_usage.known else None
            ),
        ),
        safety=ReceiptSafetySummary(
            status=(
                safety.status.value
                if safety is not None
                else "unavailable"
            ),
            provider=safety.provider if safety is not None else None,
            mode=safety.mode if safety is not None else None,
            coverage=list(safety.coverage) if safety is not None else [],
            signalCount=(
                max(safety.signalCount, len(safety.signals))
                if safety is not None
                else 0
            ),
            truncated=safety.truncated if safety is not None else False,
        ),
        delegations=list(delegations or [])[:MAX_DELEGATIONS],
        modelRequests=later_requests,
        iterations=max(0, int(iterations or 0)),
        status=status,
        partial=bool(partial),
    )
    if prompt_capped:
        _note(receipt, "prompt_capped")
    if offered_count > len(offers):
        _note(receipt, "tools_offered_capped")
    if len(all_calls) > len(receipt.toolCalls):
        _note(receipt, "tool_calls_capped")
    if any(m.content.truncated for m in receipt.prompt):
        _note(receipt, "prompt_payload_truncated")
    if any(
        (c.arguments is not None and c.arguments.truncated)
        or (c.result is not None and c.result.truncated)
        for c in receipt.toolCalls
    ):
        _note(receipt, "tool_payload_truncated")
    if any(block.sourceCount > len(block.sources) for block in receipt.contextBlocks):
        _note(receipt, "context_sources_capped")
    if len(delegations or []) > len(receipt.delegations):
        _note(receipt, "delegations_capped")
    if later_request_count > len(receipt.modelRequests):
        _note(receipt, "model_requests_capped")
    return enforce_receipt_budget(receipt)
