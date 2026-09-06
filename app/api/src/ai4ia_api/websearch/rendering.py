"""Bounded, credential-redacted, nonce-fenced WebIQ data (never instructions)."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Any

from ..agents.tools import redact, redact_obj

# Keep WebIQ's complete envelope within the generic 8 KiB tool-result budget.
# ASCII JSON makes this include escaping and fences, independent of any larger
# runtime safety ceiling for governed WebIQ results.
MAX_OUTPUT_CHARS_PER_CALL = 8192
MAX_OUTPUT_CHARS_PER_TURN = 100_000
MAX_NODES = 2000
MAX_DEPTH = 12
MAX_FIELDS = 64

_CONTENT_FIELDS = {"content", "description", "summary", "caption", "passage", "text", "snippet"}
_SOURCE_FIELDS = (
    "title", "name", "url", "hostPageUrl", "momentUrl", "source", "publishedBy",
    "lastUpdatedAt", "crawledAt", "publishedAt", "timestamp", "traceId",
)


@dataclass
class _ContentText:
    value: str


def _content_cost(value: str) -> int:
    """Additional encoded characters when text is inside JSON inside a tool result."""
    return len(json.dumps(json.dumps(value))) - len(json.dumps(json.dumps("")))


def clean_scalar(value: Any, *, nonce: str, limit: int = 300) -> str:
    text = redact(str(value or ""))
    text = text.replace(f"BEGIN RESULTS {nonce}", "[escaped result marker]")
    text = text.replace(f"END RESULTS {nonce}", "[escaped result marker]")
    return text.replace("\n", " ").replace("\r", " ").strip()[:limit]


def render_results(
    value: Any, *, nonce: str, results_cap: int, content_cap: int, output_cap: int,
    web_results_cap: int | None = None,
) -> tuple[str, bool]:
    """Preserve heterogeneous nested answers without trusting their shape or size."""
    truncated = False
    nodes = 0
    remaining_chars = output_cap
    content_slots: list[_ContentText] = []

    def clean(item: Any, *, field: str = "", depth: int = 0) -> Any:
        nonlocal nodes, truncated, remaining_chars
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            truncated = True
            return "[structure truncated]"
        if isinstance(item, Mapping):
            if item.get("isAdult") is True:
                return {"filtered": "Adult result withheld."}
            # Keep citations/timestamps ahead of long content when the overall
            # response cap cuts the representation short.
            keys = [key for key in _SOURCE_FIELDS if key in item]
            keys += list(islice((key for key in item if key not in _SOURCE_FIELDS), MAX_FIELDS))
            if len(item) > MAX_FIELDS:
                truncated = True
            out = {}
            for key in keys[:MAX_FIELDS]:
                if nodes >= MAX_NODES:
                    truncated = True
                    break
                key_text = clean_scalar(key, nonce=nonce, limit=100)
                # Redact credential-keyed values before descending; bounded
                # traversal avoids running the generic recursive redactor on an
                # unbounded provider response.
                masked = redact_obj({str(key): None})
                if masked[str(key)] is not None:
                    out[key_text] = masked[str(key)]
                else:
                    out[key_text] = clean(item[key], field=str(key), depth=depth + 1)
            return out
        if isinstance(item, (list, tuple)):
            limit = min(results_cap, web_results_cap) if (
                field == "webResults" and web_results_cap is not None
            ) else results_cap
            if len(item) > limit:
                truncated = True
            out = []
            for child in item[:limit]:
                if nodes >= MAX_NODES:
                    truncated = True
                    break
                out.append(clean(child, field=field, depth=depth + 1))
            return out
        if isinstance(item, str):
            if field in _CONTENT_FIELDS:
                # Defer verbose content until all citations/timestamps have been
                # retained. One long first result must not consume every later
                # source's space before the aggregate budget is applied.
                cap = min(content_cap, max(0, output_cap))
                text = redact(item[:cap + 1])
                text = text.replace(f"BEGIN RESULTS {nonce}", "[escaped result marker]")
                text = text.replace(f"END RESULTS {nonce}", "[escaped result marker]")
                if len(item) > cap + 1 or len(text) > cap:
                    truncated = True
                slot = _ContentText(text[:cap])
                content_slots.append(slot)
                return slot
            if remaining_chars <= 0:
                truncated = True
                return "[text omitted]"
            # Do not materialize/redact arbitrarily large provider strings when
            # only this bounded prefix could reach the model anyway.
            raw_cap = max(2048, remaining_chars + 1)
            text = redact(item[:raw_cap])
            text = text.replace(f"BEGIN RESULTS {nonce}", "[escaped result marker]")
            text = text.replace(f"END RESULTS {nonce}", "[escaped result marker]")
            cap = 2048 if "url" in field.lower() else 1000
            cap = min(cap, remaining_chars)
            if len(item) > raw_cap or len(text) > cap:
                truncated = True
            remaining_chars -= min(len(text), cap)
            return text[:cap]
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        return clean_scalar(item, nonce=nonce)

    cleaned = clean(value)
    prefix, ending = f"BEGIN RESULTS {nonce}\n", f"\nEND RESULTS {nonce}"

    def encode() -> str:
        return json.dumps(cleaned, ensure_ascii=True, separators=(",", ":"), default=lambda slot: slot.value)

    candidates = [slot.value for slot in content_slots]
    for slot in content_slots:
        slot.value = ""
    fixed_body = encode()
    remaining = max(0, output_cap - len(json.dumps(prefix + fixed_body + ending)))
    costs = sorted((_content_cost(text), index) for index, text in enumerate(candidates))
    for position, (cost, index) in enumerate(costs):
        allowance = min(cost, remaining // (len(costs) - position))
        text = candidates[index]
        if cost > allowance:
            truncated = True
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if _content_cost(text[:middle]) <= allowance:
                    low = middle
                else:
                    high = middle - 1
            text = text[:low]
        content_slots[index].value = text
        remaining -= _content_cost(text)
    body = encode()
    suffix = "\n[response truncated by server limits; request fewer results or shorter content]"
    if len(json.dumps(prefix + body + ending)) > output_cap:
        truncated = True
        low, high = 0, min(len(body), output_cap)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(prefix + body[:middle] + suffix + ending)) <= output_cap:
                low = middle
            else:
                high = middle - 1
        body = body[:low] + suffix
    return prefix + body + ending, truncated
