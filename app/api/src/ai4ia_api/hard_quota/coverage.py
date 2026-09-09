"""Versioned meter coverage. An absent bound is never a zero-price estimate."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from ..catalog import ModelCatalog
from ..usage.pricing import PricingBook, conservative_token_cost
from .models import Amounts, Bounds, Surface

# The transport sites and their meters are intentionally finite. Adding an
# outbound metered client requires adding a site and a paired no-dispatch test.
COVERAGE: dict[Surface, str] = {
    "chat": "request only without a reviewed downstream-attempt envelope",
    "embedding": "request only without catalog context and a downstream-attempt envelope",
    "image": "request only; image/multimodal tokens and dollars unbounded",
    "video": "request only; asynchronous video meter unbounded",
    "transcription": "request only; audio duration/token meter unbounded",
    "speech": "request only; audio/token meter unbounded",
    "realtime": "request only; session tokens/dollars unbounded",
    "compute": "one sandbox attempt; tokens/dollars unbounded",
    "document": "request only; CU/OCR page and downstream model meter unbounded",
    "web_search": "request only; service and downstream model meter unbounded",
    "mcp": "request only; remote tool/resource meter unbounded",
    "external_tool": "request only; remote execution meter unbounded",
}


@dataclass(frozen=True)
class AttemptEnvelope:
    """A transport integration contract, NEVER an env/admin acknowledgement.

    The shipping proxy/APIM path has no verified envelope. Runtime factories
    supply None; only deterministic test transports currently supply one.
    """

    version: str
    max_attempts: int

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", self.version) is None
            or type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 64
        ):
            raise ValueError("Invalid downstream attempt envelope.")


def _text_only(value: Any) -> bool:
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str) and kind not in {
            "text", "input_text", "output_text", "message", "function",
            "function_call", "function_call_output", "tool_use", "tool_result",
            "object", "string", "number", "integer", "boolean", "array", "null",
        }:
            return False
        return all(
            key not in {"audio", "image_url", "file_id", "file_data", "encrypted_content"}
            and _text_only(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return all(_text_only(child) for child in value)
    return value is None or isinstance(value, (str, int, float, bool))


def reservation_bounds(
    surface: Surface, payload: dict[str, Any], *, deployment: str | None,
    catalog: ModelCatalog, pricing: PricingBook, attempts: AttemptEnvelope | None = None,
) -> Bounds:
    fallback = Bounds(
        amounts=Amounts(compute=1 if surface == "compute" else 0),
        basis="compute-v1" if surface == "compute" else "request-v1",
    )
    if surface not in {"chat", "embedding"} or attempts is None:
        return fallback
    matches = [
        entry for entry in catalog.models
        if any(option.deploymentName == deployment for option in catalog.eligible_options(entry))
    ]
    if len(matches) != 1:
        return fallback
    model = matches[0]
    context = model.contextWindow
    if context is None or context <= 0:
        return fallback
    if surface == "embedding":
        inputs = payload.get("input")
        if model.category != "embedding" or not isinstance(inputs, list) or not inputs:
            return fallback
        if not all(isinstance(item, str) for item in inputs):
            return fallback
        prompt_bound, output_bound = context * len(inputs), 0
        basis = "catalog-embedding-v1"
    else:
        if (
            not model.conversational or not _text_only(payload)
            or payload.get("n", 1) != 1
            or any(payload.get(key) for key in (
                "background", "previous_response_id", "conversation", "prediction", "audio",
            ))
        ):
            return fallback
        output_bound = model.maxOutputTokens
        if output_bound is None or output_bound <= 0:
            return fallback
        for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            if name not in payload:
                continue
            requested = payload[name]
            if (
                not isinstance(requested, int) or isinstance(requested, bool)
                or requested <= 0 or requested > output_bound
            ):
                return fallback
            output_bound = requested
            break
        prompt_bound = context
        basis = "catalog-text-v1"
    # Reserve the full catalog input envelope, not a tokenizer heuristic. The
    # catalog describes total context; adding output again is conservative.
    prompt_bound *= attempts.max_attempts
    output_bound *= attempts.max_attempts
    tokens = prompt_bound + output_bound
    snapshot = pricing.snapshot_token_prices(model.id)
    rate = snapshot.rate(model.id)
    version = snapshot.version
    if (
        rate is None or snapshot.currency != "USD" or not version
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", version) is None
        or not all(math.isfinite(v) and v >= 0 for v in (rate.input_per_1m, rate.output_per_1m))
    ):
        return Bounds(
            amounts=Amounts(tokens=tokens), basis=basis,
            attemptVersion=attempts.version, maxAttempts=attempts.max_attempts,
        )
    estimate = snapshot.estimate_token_bound(
        model.id, prompt_tokens=prompt_bound, completion_tokens=output_bound,
    )
    return Bounds(
        amounts=Amounts(tokens=tokens, microUsd=estimate.micro_usd), basis=basis, priceVersion=version,
        inputRate=str(rate.input_per_1m), outputRate=str(rate.output_per_1m),
        attemptVersion=attempts.version, maxAttempts=attempts.max_attempts,
    )


def actual_amounts(bounds: Bounds, raw: dict[str, Any] | None) -> Amounts | None:
    """Refund only a complete, internally consistent provider usage object."""
    if bounds.amounts.tokens is None and bounds.amounts.microUsd is None:
        return Amounts(compute=bounds.amounts.compute)
    if bounds.maxAttempts != 1:
        # A final backend response cannot account for earlier retried attempts.
        return None
    if not isinstance(raw, dict):
        return None
    prompt, completion = raw.get("prompt_tokens"), raw.get("completion_tokens")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53 - 1
        for value in (prompt, completion)
    ):
        return None
    assert isinstance(prompt, int) and isinstance(completion, int)
    total = raw.get("total_tokens", prompt + completion)
    if not isinstance(total, int) or isinstance(total, bool) or total != prompt + completion:
        return None
    micro = None
    if bounds.inputRate is not None and bounds.outputRate is not None:
        # Same pinned pricing rates; ceiling never understates the shared
        # pricing book's rounded estimate. No current-book read at settlement.
        micro = conservative_token_cost(
            prompt_tokens=prompt, completion_tokens=completion,
            input_rate=bounds.inputRate, output_rate=bounds.outputRate,
        )
    return Amounts(tokens=total, microUsd=micro, compute=bounds.amounts.compute)
