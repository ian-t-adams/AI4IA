"""Execution receipts: what a turn actually sent, offered, and ran.

The activity trace answers "which tools ran?" in one line each. It deliberately
carries no arguments and no results, which makes it useless for the question an
owner reviewing their own conversation actually has: *what was supplied to the
model, what was it allowed to do, and what did it do with it?*

These tests pin the properties that make the answer trustworthy rather than
merely present:

* credentials never survive into a persisted receipt,
* a hostile payload is bounded, and the fact that it was bounded survives,
* "offered" and "invoked" stay distinguishable,
* automatically-injected context (recalled memory above all) is recorded along
  with whether it was actually admitted,
* an older Cosmos row still loads,
* and nothing here claims to expose model-internal reasoning.
"""
from __future__ import annotations

import json

from ai4ia_api.agents.receipt import ReceiptDraft, receipt_tool_calls
from ai4ia_api.agents.runtime import AgentStep, DelegatedRunTrace
from ai4ia_api.gateway.client import RESPONSES_OUTPUT_ITEMS_KEY
from ai4ia_api.receipts import (
    MAX_PAYLOAD_BYTES,
    MAX_RECEIPT_BYTES,
    MAX_TOOL_CALLS,
    RECEIPT_VERSION,
    ExecutionReceipt,
    ReceiptRuntime,
    build_receipt,
    json_payload,
    text_payload,
)
from ai4ia_api.sessions.models import Message, MessageRole

RUNTIME = ReceiptRuntime(
    modelId="gpt-4o",
    deployment="gpt-4o-eastus2",
    region="eastus2",
    sku="GlobalStandard",
    dataZone="us",
    residency="global",
    api="chat",
    agent="researcher",
)


def _draft(**kwargs) -> ReceiptDraft:
    base = {
        "correlation_id": "corr-1",
        "runtime": RUNTIME,
        "prompt_messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ],
    }
    base.update(kwargs)
    return ReceiptDraft(**base)


# --- Redaction ---------------------------------------------------------------


def test_credentials_never_reach_a_persisted_receipt():
    """The receipt is a persistence boundary, so it redacts on its own account.

    ``AgentStep`` arrives already redacted from the runtime, but a receipt that
    depended on that would be one refactor away from writing a live key into
    Cosmos.
    """
    step = AgentStep(
        kind="tool_result",
        tool="browse_url",
        arguments={"api_key": "sk-live-abcdef", "url": "https://example.test"},
        result={"body": "authorization: Bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )
    call = receipt_tool_calls([step])[0]

    assert call.arguments is not None and call.result is not None
    assert "sk-live-abcdef" not in call.arguments.text
    assert "REDACTED" in call.arguments.text
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in call.result.text
    # The non-secret argument is still legible -- redaction that hid everything
    # would make the receipt useless for the review it exists to support.
    assert "https://example.test" in call.arguments.text


def test_prompt_and_context_text_is_redacted_too():
    payload = text_payload("here is my password: hunter2seventeen")
    assert "hunter2seventeen" not in payload.text
    assert "REDACTED" in payload.text


def test_signed_download_url_is_redacted_at_receipt_boundary():
    payload = json_payload(
        {
            "downloadUrl": (
                "https://blob.example.test/result.csv"
                "?sv=2026-01-01&sig=ab%2Fcd%2Bef"
            )
        }
    )

    assert "ab%2Fcd%2Bef" not in payload.text
    assert "blob.example.test/result.csv" in payload.text


def test_redaction_is_not_vacuous():
    """Control: an ordinary payload passes through unchanged.

    Without this, a redactor that masked everything would look identical to one
    that worked.
    """
    payload = json_payload({"url": "https://example.test", "count": 3})
    assert json.loads(payload.text) == {"url": "https://example.test", "count": 3}


# --- Bounds ------------------------------------------------------------------


def test_oversized_payload_is_truncated_but_its_real_size_survives():
    """A tool cannot inflate a Cosmos message, and cannot hide that it tried."""
    # Ordinary prose, not one long opaque token: the credential redactor masks
    # 32+ character tokens wholesale, and a payload it had already erased would
    # test the redactor rather than the size bound.
    huge = "lorem ipsum dolor sit amet " * (MAX_PAYLOAD_BYTES // 4)
    payload = json_payload({"body": huge})

    assert len(payload.text.encode("utf-8")) <= MAX_PAYLOAD_BYTES + 64
    assert payload.truncated is True
    # ``bytes`` describes the FULL redacted payload, not the retained slice.
    assert payload.bytes > MAX_PAYLOAD_BYTES * 3
    assert len(payload.sha256) == 64


def test_within_cap_payload_is_not_marked_truncated():
    """Control for the bound: the flag has to mean something."""
    payload = json_payload({"body": "short"})
    assert payload.truncated is False
    assert payload.bytes == len(payload.text.encode("utf-8"))


def test_whole_receipt_is_bounded_and_says_so():
    filler = "lorem ipsum dolor sit amet " * (MAX_PAYLOAD_BYTES // 8)
    steps = [
        AgentStep(
            kind="tool_result",
            tool="web_search",
            arguments={"q": filler},
            result={"body": filler},
        )
        for _ in range(MAX_TOOL_CALLS)
    ]
    receipt = _draft().build(steps=steps, iterations=2)

    size = len(receipt.model_dump_json().encode("utf-8"))
    assert size <= MAX_RECEIPT_BYTES
    assert receipt.truncated is True
    assert "receipt_size_capped" in receipt.notes
    # Shedding a body keeps the evidence about it.
    shed = [c for c in receipt.toolCalls if c.result is not None and not c.result.text]
    assert shed and all(len(c.result.sha256) == 64 for c in shed if c.result)


def test_excess_tool_calls_are_counted_not_silently_dropped():
    steps = [
        AgentStep(kind="tool_result", tool="calculator", arguments={"n": i})
        for i in range(MAX_TOOL_CALLS + 5)
    ]
    receipt = _draft().build(steps=steps)

    assert len(receipt.toolCalls) == MAX_TOOL_CALLS
    assert receipt.toolCallCount == MAX_TOOL_CALLS + 5
    assert "tool_calls_capped" in receipt.notes


# --- Offered vs. invoked -----------------------------------------------------


def _offered(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"does {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def test_offered_tools_are_recorded_even_when_never_invoked():
    """'The model could have sent mail and chose not to' is a fact about the
    turn that the call list alone cannot express."""
    receipt = _draft(offered=_offered("web_search", "send_mail")).build(
        steps=[AgentStep(kind="tool_result", tool="web_search", arguments={"q": "hi"})]
    )

    assert [o.name for o in receipt.toolsOffered] == ["web_search", "send_mail"]
    assert [c.tool for c in receipt.toolCalls] == ["web_search"]
    assert receipt.toolsOfferedCount == 2 and receipt.toolCallCount == 1


def test_a_turn_with_no_tools_records_an_empty_offer_not_a_missing_one():
    receipt = _draft().build()
    assert receipt.toolsOffered == [] and receipt.toolsOfferedCount == 0
    assert receipt.toolCalls == [] and receipt.toolCallCount == 0


def test_unsafe_tool_names_are_sentineled_at_the_receipt_boundary():
    """Same rule the activity trace applies: a name that is dispatchable is not
    automatically safe to persist verbatim."""
    forged = receipt_tool_calls(
        [AgentStep(kind="tool_result", tool="ok\nagent tool ran: admin")]
    )
    assert forged[0].tool == "unknown_tool"
    receipt = build_receipt(offered=_offered("bad\nname"))
    assert receipt.toolsOffered[0].name == "unknown_tool"


def test_denied_and_errored_calls_keep_their_outcome_and_reason():
    receipt = _draft().build(
        steps=[
            AgentStep(kind="tool_denied", tool="browse_url", detail="approval_required"),
            AgentStep(kind="tool_error", tool="run_code", detail="execution_error"),
            AgentStep(kind="delegate", tool="delegate_to_agent", arguments={"a": 1}),
            # Not a finalized call: the pre-execution "running X" marker.
            AgentStep(kind="tool_start", tool="web_search", arguments={"q": "x"}),
            AgentStep(kind="final"),
        ]
    )
    assert [(c.tool, c.outcome, c.detail) for c in receipt.toolCalls] == [
        ("browse_url", "denied", "approval_required"),
        ("run_code", "error", "execution_error"),
        ("delegate_to_agent", "delegate", None),
    ]


# --- Prompt and automatic context -------------------------------------------


def test_recalled_memory_is_recorded_as_admitted_context():
    """Memory is injected without the user asking for it, so "what was supplied
    on my behalf" is exactly what a receipt has to answer."""
    receipt = _draft(
        blocks=[("memory", "Earlier you said you prefer Python.", True)]
    ).build()

    memory = next(b for b in receipt.contextBlocks if b.kind == "memory")
    assert memory.admitted is True
    assert memory.content is not None
    assert "prefer Python" in memory.content.text


def test_context_sources_record_memory_identity_version_and_redacted_digest():
    receipt = _draft(
        blocks=[("memory", "Earlier you said you prefer Python.", True)],
        block_sources={
            "memory": [
                {
                    "id": "memory-1",
                    "version": 4,
                    "updatedAt": "2026-09-03T00:00:00+00:00",
                    "kind": "user_message",
                    "content": "api_key=sk-live-secret; prefers Python",
                    "score": 0.91234,
                }
            ]
        },
    ).build()

    [source] = receipt.contextBlocks[0].sources
    assert source.id == "memory-1"
    assert source.version == "4"
    assert source.score == 0.91234
    assert source.contentSha256
    assert "sk-live-secret" not in receipt.model_dump_json()


def test_a_displaced_context_block_is_recorded_as_not_admitted():
    """A block the budget dropped never influenced the answer. Showing it
    without saying so would describe a turn that did not happen."""
    receipt = _draft(
        blocks=[
            ("memory", "recalled text", True),
            ("library", "excerpt text", False),
        ],
        dropped_context_blocks=["library"],
        dropped_history_messages=3,
    ).build()

    library = next(b for b in receipt.contextBlocks if b.kind == "library")
    assert library.admitted is False and library.content is None
    assert receipt.droppedContextBlocks == ["library"]
    assert receipt.droppedHistoryMessages == 3


def test_effective_prompt_is_snapshotted_with_byte_totals():
    receipt = _draft().build()
    assert [m.role for m in receipt.prompt] == ["system", "user"]
    assert receipt.promptMessageCount == 2
    assert receipt.promptBytes == sum(m.content.bytes for m in receipt.prompt)


def test_later_model_request_keeps_tool_call_ids_and_grouping():
    first = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "question"},
    ]
    second = [
        *first,
        {
            "role": "assistant",
            "content": "",
            RESPONSES_OUTPUT_ITEMS_KEY: [
                {
                    "type": "reasoning",
                    "encrypted_content": "opaque-reasoning-secret",
                }
            ],
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "arguments": '{"expression":"6*7"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"result":42}',
        },
    ]

    receipt = _draft().build(
        prompt_messages=first,
        model_requests=[first, second],
    )

    [request] = receipt.modelRequests
    assert request.iteration == 2
    assistant = next(
        message for message in request.prompt if message.role == "assistant"
    )
    tool = next(message for message in request.prompt if message.role == "tool")
    assert assistant.toolCalls is not None
    assert "call-1" in assistant.toolCalls.text
    assert tool.toolCallId == "call-1"
    serialized = receipt.model_dump_json()
    assert "opaque-reasoning-secret" not in serialized
    assert "encrypted_content" not in serialized


def test_successful_delegation_keeps_its_own_prompt_tools_and_calls():
    trace = DelegatedRunTrace(
        agent="helper",
        effective_prompt=[
            {"role": "system", "content": "You are helper."},
            {"role": "user", "content": "calculate"},
        ],
        offered_tools=[
            {
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "Calculate.",
                    "parameters": {"type": "object"},
                },
            }
        ],
        steps=[
            AgentStep(
                kind="tool_result",
                tool="calculator",
                arguments={"expression": "6*7"},
                result={"result": 42},
            )
        ],
        iterations=2,
    )

    receipt = _draft().build(delegations=[trace])

    [nested] = receipt.delegations
    assert nested.runtime.agent == "helper"
    assert [message.role for message in nested.prompt] == ["system", "user"]
    assert [offer.name for offer in nested.toolsOffered] == ["calculator"]
    assert [call.tool for call in nested.toolCalls] == ["calculator"]


# --- No chain of thought -----------------------------------------------------

# Anything that would assert access to a model's internal deliberation. The
# platform does not report one, so a field named like this could only ever be a
# claim the system cannot support.
_FORBIDDEN = (
    "reasoning",
    "reasoningcontent",
    "thought",
    "thoughts",
    "thinking",
    "chainofthought",
    "cot",
    "scratchpad",
    "internalmonologue",
)


def _all_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).replace("_", "").lower())
            keys |= _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            keys |= _all_keys(child)
    return keys


def test_receipt_has_no_chain_of_thought_field():
    receipt = _draft(offered=_offered("web_search")).build(
        steps=[AgentStep(kind="tool_result", tool="web_search", arguments={"q": "x"})],
        iterations=2,
    )
    assert not (_all_keys(receipt.model_dump(mode="json")) & set(_FORBIDDEN))


def test_forbidden_key_detector_is_not_vacuous():
    """Control: the scan must actually be able to fail."""
    assert _all_keys({"reasoning_content": "..."}) & set(_FORBIDDEN)


# --- Persistence compatibility ----------------------------------------------


def test_old_rows_load_without_a_receipt():
    """Additive optional field: rows written before this feature must still load
    and must not be given a fabricated receipt."""
    legacy = Message.model_validate(
        {
            "sessionId": "s",
            "userId": "u",
            "role": "assistant",
            "content": "hi",
            "safety": {"signals": []},
        }
    )
    assert legacy.executionReceipt is None


def test_receipt_round_trips_through_the_message_document():
    receipt = _draft(offered=_offered("web_search")).build(
        steps=[
            AgentStep(
                kind="tool_result",
                tool="web_search",
                arguments={"q": "weather"},
                result={"items": 3},
            )
        ],
        iterations=2,
    )
    message = Message(
        sessionId="s",
        userId="u",
        role=MessageRole.assistant,
        content="done",
        executionReceipt=receipt,
    )
    restored = Message.model_validate(message.model_dump(mode="json"))

    assert restored.executionReceipt is not None
    assert restored.executionReceipt.version == RECEIPT_VERSION
    assert restored.executionReceipt.runtime.residency == "global"
    assert restored.executionReceipt.toolCalls[0].tool == "web_search"


def test_build_is_total_over_missing_data():
    """Every persistence path must be able to produce a receipt, including the
    ones that hold almost nothing."""
    receipt = ReceiptDraft().build(status="cancelled", partial=True)
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.status == "cancelled" and receipt.partial is True


def test_a_broken_draft_degrades_instead_of_breaking_the_turn():
    """A receipt is a record of a turn, not a participant in it."""

    class Exploding:
        """Not a list subclass: ``list()`` has a fast path for those that never
        calls ``__iter__``, so the hostile behaviour would never fire."""

        def __iter__(self):
            raise RuntimeError("boom")

    draft = ReceiptDraft(prompt_messages=Exploding())  # pyright: ignore[reportArgumentType]
    receipt = draft.build(status="complete")
    assert receipt.notes == ["receipt_build_failed"] and receipt.partial is True
