"""Score executed app evidence; never serialize the transient content below."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import JsonValue, TypeAdapter, ValidationError

from .contracts import (
    CHECK_IDS, Case, CaseResult, Check, Dataset, EvaluationError, Measurements, Status,
    applicable_checks, canonical_bytes, decode_json, overall,
)

_JSON_VALUE = TypeAdapter(JsonValue)


@dataclass
class Observation:
    messages: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    request_paths: list[str] = field(default_factory=list)
    model_id: str = ""
    deployment: str = ""
    aliases: dict[str, str] = field(default_factory=dict)
    fixture_latency_ms: int = 0
    consumed_replies: int = 0
    transport_valid: bool = True
    stream_finished: bool = True
    network_attempts: int = 0
    owned_sources: dict[str, dict] = field(default_factory=dict)
    foreign_sources: set[str] = field(default_factory=set)
    dispatch_counts: list[int] = field(default_factory=list)
    approval_prompts: list[int] = field(default_factory=list)
    exact_dispatch: bool = True
    original_approval_consumed: bool | None = None
    owner_read_denied: bool = False


def _check(name: str, matched: bool | None, *, applicable: bool = True) -> Check:
    if not applicable:
        return Check(id=name, status="unscored", reason="not_applicable")
    if matched is None:
        return Check(id=name, status="unknown", reason="evidence_missing")
    return Check(
        id=name, status="passed" if matched else "failed",
        reason="matched" if matched else "violated",
    )


def _json_object(text: str | None) -> tuple[bool, JsonValue]:
    if not isinstance(text, str):
        return False, None
    try:
        return True, _JSON_VALUE.validate_python(decode_json(
            text.encode("utf-8"), 32_768, "output_too_large",
        ))
    except (EvaluationError, ValidationError):
        return False, None


def _known_total(values: list[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _tool_outputs(request: dict, protocol: str) -> list[tuple[str, JsonValue]]:
    outputs = []
    if protocol == "responses":
        for item in request.get("input", []):
            if item.get("type") == "function_call_output":
                valid, value = _json_object(item.get("output"))
                if valid and isinstance(item.get("call_id"), str):
                    outputs.append((item["call_id"], value))
    else:
        for message in request.get("messages", []):
            if protocol == "chat" and message.get("role") == "tool":
                valid, value = _json_object(message.get("content"))
                if valid and isinstance(message.get("tool_call_id"), str):
                    outputs.append((message["tool_call_id"], value))
            elif protocol == "anthropic" and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if block.get("type") == "tool_result":
                        valid, value = _json_object(block.get("content"))
                        if valid and isinstance(block.get("tool_use_id"), str):
                            outputs.append((block["tool_use_id"], value))
    return outputs


def _fixture_text(body: dict, protocol: str) -> str:
    if protocol == "chat":
        return body["choices"][0]["message"].get("content") or ""
    if protocol == "responses":
        blocks = [
            block for item in body.get("output", []) if item.get("type") == "message"
            for block in item.get("content", []) if block.get("type") == "output_text"
        ]
    else:
        blocks = [block for block in body.get("content", []) if block.get("type") == "text"]
    return "".join(block.get("text", "") for block in blocks)


def _citations(case: Case, observed: Observation) -> bool:
    row = observed.messages[-1]
    citations, sources = row.get("citations") or [], row.get("sources") or []
    spans = {source["spanId"]: source for source in sources}
    if not citations or not sources or len(spans) != len(sources):
        return False
    prompts = json.dumps(observed.requests)
    for source in sources:
        owned = observed.owned_sources.get(source.get("documentId"))
        if (
            owned is None or source.get("documentId") in observed.foreign_sources
            or source.get("documentVersion") != owned["version"]
            or source.get("contentSha256") != owned["version"]
            or source.get("excerpt") != owned["text"]
            or source.get("filename") != owned["filename"]
            or f"cite-as: [[cite:{source['spanId']}]]" not in prompts
        ):
            return False
    if case.document is None or case.document.foreign_text in prompts:
        return False
    return all(
        citation.get("status") == "verified"
        and citation.get("spanId") in spans
        and citation.get("documentId") == spans[citation["spanId"]]["documentId"]
        and f"[[cite:{citation['spanId']}]]" in row["content"]
        for citation in citations
    )


def _approval(case: Case, observed: Observation, receipts) -> bool:
    if case.scenario == "workflow":
        return (
            observed.dispatch_counts == [0]
            and receipts[0].approvalsRequested > 0
            and bool(receipts[0].toolCalls)
            and all(call.outcome == "denied" for call in receipts[0].toolCalls)
        )
    expected = [0, 1, 0] if case.approval_variant == "exact-replay" else [0, 0]
    if (
        observed.dispatch_counts != expected
        or len(observed.approval_prompts) != len(expected)
        or not all(count > 0 for count in observed.approval_prompts)
        or not observed.exact_dispatch
    ):
        return False
    if case.approval_variant == "exact-replay":
        return (
            observed.original_approval_consumed is True
            and [call.outcome for call in receipts[0].toolCalls] == ["denied"]
            and [call.outcome for call in receipts[1].toolCalls] == ["result", "denied"]
            and receipts[1].toolCalls[0].approval == "invocation"
            and [call.outcome for call in receipts[2].toolCalls] == ["denied"]
            and receipts[1].approvalsGranted == 1
            and receipts[2].approvalsGranted == 0
        )
    return (
        all(receipt.toolCalls and all(
            call.outcome == "denied" for call in receipt.toolCalls
        ) for receipt in receipts)
        and (
            case.approval_variant != "cross-owner"
            or (observed.owner_read_denied and observed.original_approval_consumed is False)
        )
    )


def score_case(dataset: Dataset, case: Case, observed: Observation) -> CaseResult:
    from jsonschema import Draft202012Validator

    from ai4ia_api.receipts import MAX_RECEIPT_BYTES, RECEIPT_VERSION, ExecutionReceipt

    if not observed.messages or any(not row.get("executionReceipt") for row in observed.messages):
        from .contracts import unknown_result

        return unknown_result(case, "missing_outcome")
    receipts = [
        ExecutionReceipt.model_validate(row["executionReceipt"]) for row in observed.messages
    ]
    leaves = (
        [ExecutionReceipt.model_validate(child) for child in
         observed.messages[0].get("workflowStepReceipts") or []]
        if case.scenario == "workflow" else receipts
    )
    calls = [call for receipt in receipts for call in receipt.toolCalls]
    model_calls = [call for receipt in leaves for call in receipt.runtime.modelCalls or []]
    instructions = tuple(sorted({
        leaf.runtime.instructionSha256 for leaf in leaves if leaf.runtime.instructionSha256
    }))
    complete_evidence = bool(leaves) and all(
        receipt.version == RECEIPT_VERSION
        and not receipt.truncated
        and receipt.toolCallCount == len(receipt.toolCalls)
        and receipt.toolsOfferedCount == len(receipt.toolsOffered)
        and len(canonical_bytes(receipt.model_dump(mode="json"))) <= MAX_RECEIPT_BYTES
        for receipt in [*receipts, *leaves]
    )
    complete_evidence = complete_evidence and all(
        leaf.prompt and leaf.runtime.instructionSha256
        and leaf.runtime.modelId == observed.model_id
        and leaf.runtime.api == case.protocol
        and leaf.runtime.modelCallCount is not None
        and leaf.runtime.modelCallCount == len(leaf.runtime.modelCalls or [])
        and leaf.runtime.modelCallCount > 0
        for leaf in leaves
    )
    statuses_match = all(
        row.get("status") == case.expected.status and receipt.status == case.expected.status
        for row, receipt in zip(observed.messages, receipts)
    )
    checks = [_check("execution", bool(
        complete_evidence and statuses_match and observed.stream_finished and instructions
    ))]
    checks.append(_check("transport", (
        observed.transport_valid and observed.network_attempts == 0
        and 0 < len(observed.requests) == len(model_calls) == observed.consumed_replies
        and observed.consumed_replies == len(case.replies)
        and len(observed.requests) <= case.expected.max_model_calls
        and all(
            call.coverage == "recorded" and call.httpAttempts == 1 and call.providerCompleted
            for call in model_calls
        )
    )))

    required = {observed.aliases.get(name, name) for name in case.expected.required_tools}
    forbidden = {observed.aliases.get(name, name) for name in case.expected.forbidden_tools}
    offered = {tool.name for receipt in receipts for tool in receipt.toolsOffered}
    successful = {call.tool for call in calls if call.outcome == "result"}
    attempted = {call.tool for call in calls}
    tools_ok = required <= successful and not (forbidden & attempted) and attempted <= offered
    for tool, expected_result in case.expected.tool_results.items():
        matching = [
            call for call in calls
            if call.tool == observed.aliases.get(tool, tool) and call.outcome == "result"
        ]
        tools_ok = tools_ok and bool(matching) and all(
            call.result is not None and not call.result.truncated
            and _json_object(call.result.text) == (True, expected_result)
            for call in matching
        )
    checks.append(_check("tool_choice", tools_ok))

    outputs = [
        output for request in observed.requests for output in _tool_outputs(request, case.protocol)
    ]
    feedback_calls = [
        call for call in calls if call.outcome == "result" and call.tool in required
    ]
    checks.append(_check("tool_feedback", bool(feedback_calls) and all(
        call.result is not None
        and _json_object(call.result.text)[0]
        and (call.callId, _json_object(call.result.text)[1]) in outputs
        for call in feedback_calls
    ), applicable=case.expected.require_tool_feedback))
    checks.append(_check("citations", _citations(case, observed)
                         if case.expected.require_citations else False,
                         applicable=case.expected.require_citations))
    approval_applicable = case.scenario == "approval" or (
        case.scenario == "workflow" and bool(observed.aliases)
    )
    checks.append(_check(
        "approval", _approval(case, observed, receipts) if approval_applicable else False,
        applicable=approval_applicable,
    ))

    text = observed.messages[-1].get("content") or ""
    schema = case.expected.output_schema
    valid_json, output = _json_object(text)
    checks.append(_check(
        "output_schema",
        valid_json and Draft202012Validator(schema).is_valid(output) if schema is not None else False,
        applicable=schema is not None,
    ))
    checks.append(_check("content", (
        all(part in text for part in case.expected.required_text)
        and all(part not in text for part in case.expected.forbidden_text)
    ), applicable="content" in applicable_checks(case)))
    safety_rows = [row.get("safety") for row in observed.messages]
    safety_ok = all(
        receipt.safety.status == case.expected.safety_status
        and receipt.safety.mode in (
            ("annotate_only", None) if case.expected.safety_status == "unavailable"
            else ("annotate_only",)
        )
        for receipt in receipts
    )
    for row in safety_rows:
        if case.expected.safety_status == "reported":
            safety_ok = safety_ok and bool(
                row and row.get("signals") and "completion" in row.get("coverage", [])
            )
        if row:
            safety_ok = safety_ok and all(
                signal.get("filtered") is case.expected.safety_filtered
                for signal in row.get("signals", [])
            )
    checks.append(_check("safety", safety_ok))

    costs = [receipt.usage.cost for receipt in receipts]
    known_costs = [cost for cost in costs if cost and cost.estCostMicroUsd is not None]
    cost_known = bool(costs) and len(known_costs) == len(costs) and all(
        cost.coverage == "known" and cost.pricedCalls == cost.totalCalls
        and cost.priceVersions == (dataset.config.price_version,)
        and not cost.priceVersionsTruncated
        for cost in known_costs
    ) and all(
        call.usageKnown and call.usageComplete
        and call.cost.coverage == "known"
        and call.cost.priceVersion == dataset.config.price_version
        for call in model_calls
    )
    subtotal = _known_total([cost.estCostMicroUsd for cost in known_costs])
    cost_value = subtotal if cost_known else None
    checks.append(_check(
        "cost", cost_value <= case.expected.max_cost_micro_usd if cost_value is not None else None,
    ))
    checks.append(_check("latency", observed.fixture_latency_ms <= case.expected.max_latency_ms))

    workflow_ok = len(leaves) == case.expected.workflow_steps and all(
        leaf.runtime.modelCalls and leaf.runtime.modelCalls[0].iteration == 1 for leaf in leaves
    )
    if case.scenario == "workflow" and len(leaves) > 1:
        intermediate = [
            text for reply in case.replies
            if (text := _fixture_text(reply.body, case.protocol))
        ]
        workflow_ok = workflow_ok and len(intermediate) == len(leaves)
        for index, leaf in enumerate(leaves[1:], start=1):
            user_prompts = [message.content.text for message in leaf.prompt if message.role == "user"]
            expected_prompt = case.workflow_instructions[index].format(
                input=case.input, previous=intermediate[index - 1],
            )
            workflow_ok = workflow_ok and bool(user_prompts) and expected_prompt in user_prompts[-1]
    checks.append(_check("workflow", bool(workflow_ok), applicable=case.scenario == "workflow"))
    assert tuple(check.id for check in checks) == CHECK_IDS
    usage_known = bool(model_calls) and all(call.usageKnown and call.usageComplete for call in model_calls)
    statuses: list[Status] = [check.status for check in checks]
    return CaseResult(
        case_id=case.id, protocol=case.protocol, status=overall(statuses), checks=tuple(checks),
        instruction_hashes=instructions,
        measurements=Measurements(
            model_calls=len(observed.requests), tool_calls=len(calls),
            fixture_latency_ms=observed.fixture_latency_ms,
            cost_micro_usd=cost_value, known_cost_subtotal_micro_usd=subtotal,
            cost_coverage="known" if cost_known else "partial" if known_costs else "unknown",
            prompt_tokens=_known_total([call.promptTokens for call in model_calls]) if usage_known else None,
            completion_tokens=_known_total([call.completionTokens for call in model_calls]) if usage_known else None,
        ),
    )
