"""Deterministic no-tool live checks; content and receipts are transient inputs only."""
from __future__ import annotations

import re

from jsonschema import Draft202012Validator

from .contracts import Check, overall
from .live_contracts import (
    LIVE_CHECK_IDS, MAX_CASE_SECONDS, MAX_ESTIMATE_MICRO_USD, MAX_OUTPUT_TOKENS,
    LiveCase, LiveError, LiveMeasurements, LiveResult,
)
from .oracles import _check, _json_object


def object_value(value: object) -> dict:
    if not isinstance(value, dict):
        raise LiveError("shape")
    return value


def count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**31 else None


def score_live(
    case: LiveCase, message: dict, *, model_id: str, prices: dict, latency_ms: int,
) -> LiveResult:
    receipt = object_value(message.get("executionReceipt"))
    runtime = object_value(receipt.get("runtime"))
    raw_calls = runtime.get("modelCalls")
    if not isinstance(raw_calls, list) or len(raw_calls) != 1:
        raise LiveError("shape")
    call = object_value(raw_calls[0])
    parameters = object_value(call.get("parameters"))
    instruction = runtime.get("instructionSha256")
    instruction = instruction if isinstance(instruction, str) and re.fullmatch(r"[a-f0-9]{64}", instruction) else None
    checks = {name: Check(id=name, status="unscored", reason="not_applicable") for name in LIVE_CHECK_IDS}
    checks["execution"] = _check("execution", (
        message.get("role") == "assistant" and message.get("status") == "complete"
        and receipt.get("status") == "complete" and receipt.get("partial") is False
        and receipt.get("truncated") is False and runtime.get("modelId") == model_id
        and runtime.get("api") == "chat" and count(runtime.get("modelCallCount")) == 1
        and instruction is not None and call.get("providerCompleted") is True
    ))
    checks["transport"] = _check("transport", (
        call.get("coverage") == "recorded" and count(call.get("httpAttempts")) == 1
        and call.get("modelId") == model_id and call.get("api") == "chat"
        and parameters.get("outputTokenField") == "max_tokens"
        and count(parameters.get("maxOutputTokens")) is not None
        and 0 < parameters["maxOutputTokens"] <= MAX_OUTPUT_TOKENS
    ))
    blocks = receipt.get("contextBlocks")
    checks["isolation"] = _check("isolation", (
        receipt.get("toolCalls") == [] and count(receipt.get("toolCallCount")) == 0
        and receipt.get("toolsOffered") == [] and count(receipt.get("toolsOfferedCount")) == 0
        and receipt.get("delegations") == [] and receipt.get("modelRequests") == []
        and runtime.get("agent") is None
        and count(receipt.get("approvalsRequested")) == count(receipt.get("approvalsGranted")) == 0
        and isinstance(blocks, list) and all(
            isinstance(block, dict) and (
                block.get("admitted") is False
                or block.get("kind") == "notice" and block.get("sources") == []
            ) for block in blocks
        )
    ))
    text = message.get("content")
    if case.exact_text is not None:
        checks["content"] = _check("content", isinstance(text, str) and text.strip() == case.exact_text)
    if case.output_schema is not None:
        valid, result = _json_object(text)
        checks["output_schema"] = _check("output_schema", (
            valid and Draft202012Validator(case.output_schema).is_valid(result)
        ))
    cost = object_value(call.get("cost"))
    input_tokens, output_tokens = count(call.get("promptTokens")), count(call.get("completionTokens"))
    value = count(cost.get("estCostMicroUsd"))
    rates = object_value(prices.get("models", {}).get(model_id))
    known = (
        call.get("usageKnown") is True and call.get("usageComplete") is True
        and input_tokens is not None and output_tokens is not None
        and cost.get("coverage") == "known" and value is not None and cost.get("currency") == "USD"
        and cost.get("pricingBasis") == "input_output_tokens"
        and cost.get("priceVersion") == prices.get("version")
        and cost.get("priceInputPer1M") == rates.get("inputPer1M")
        and cost.get("priceOutputPer1M") == rates.get("outputPer1M")
    )
    checks["cost"] = _check("cost", value <= MAX_ESTIMATE_MICRO_USD if known and value is not None else None)
    checks["latency"] = _check("latency", latency_ms <= MAX_CASE_SECONDS * 1000)
    checks["cleanup"] = Check(id="cleanup", status="unknown", reason="not_run")
    return LiveResult(
        case_id=case.id, status=overall([check.status for check in checks.values()]),
        checks=tuple(checks[name] for name in LIVE_CHECK_IDS),
        measurements=LiveMeasurements(
            latency_ms=latency_ms, cost_micro_usd=value if known else None,
            prompt_tokens=input_tokens, completion_tokens=output_tokens,
            model_calls=count(runtime.get("modelCallCount")), instruction_sha256=instruction,
        ),
    )


def with_cleanup(result: LiveResult, *, verified: bool, requests: int) -> LiveResult:
    checks = tuple(
        _check("cleanup", True if verified else None) if item.id == "cleanup" else item
        for item in result.checks
    )
    return LiveResult(
        case_id=result.case_id, status=overall([item.status for item in checks]),
        checks=checks, measurements=LiveMeasurements(
            **{**result.measurements.model_dump(), "http_attempts": requests},
        ),
    )
