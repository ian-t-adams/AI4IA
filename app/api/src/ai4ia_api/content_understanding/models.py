"""Parsed result of a Content Understanding analyze operation.

The CU GET response is ``{id, status, result:{analyzerId, contents:[{markdown,
fields,...}], warnings}}``. We normalize it to a flat, RAG-ready shape: the
concatenated Markdown across contents (the parse output we index + store) plus the
extracted fields and the raw envelope (kept for grounding/citations later).
"""
from __future__ import annotations

import re
from math import ceil
from dataclasses import dataclass, field
from typing import Any

# Terminal CU operation states (case-insensitive). ``Running``/``NotStarted`` mean
# keep polling. ``Canceled`` is terminal too and is spelled with one "l" by Azure;
# both spellings are accepted so a service-side change cannot silently turn a
# finished operation back into "poll until the budget runs out and report 408",
# which hid the real outcome behind a timeout.
TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled", "cancelled"})
CU_GA_API_VERSION = "2025-11-01"
CU_PREVIEW_API_VERSION = "2026-06-01-preview"
CU_SYNC_MAX_BYTES = 10 * 1024 * 1024
CU_SYNC_MAX_PDF_PAGES = 5
_PAGE_USAGE_METERS = {
    "documentPagesMinimal": "content-understanding-document-minimal",
    "documentPagesMinimalInline": "content-understanding-document-minimal",
    "documentPagesBasic": "content-understanding-document-basic",
    "documentPagesBasicInline": "content-understanding-document-basic",
    "documentPagesStandard": "content-understanding-document-standard",
    "documentPagesStandardInline": "content-understanding-document-standard",
}

# Content Understanding analyzer-id contract: 1-64 characters of letters, digits,
# '.', '_' and '-' (matches the service's own resource-id rules). ``fullmatch`` (not
# ``match`` + ``$``) so a trailing newline can never sneak through — ``$`` alone
# matches just before a final "\n", which ``fullmatch`` correctly rejects.
#
# Shared between the request-model validator (``AnalyzerCreate.baseAnalyzerId`` in
# routers/library.py) and :meth:`ContentUnderstandingClient.submit_url` below, so a
# persisted/legacy analyzer id that predates or otherwise bypasses the request
# model (the ``Analyzer`` domain model itself has no such validator) can never be
# interpolated into a live CU request URL.
ANALYZER_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def is_valid_analyzer_id(value: str) -> bool:
    """Whether ``value`` satisfies the CU analyzer-id contract above."""
    return bool(ANALYZER_ID_RE.fullmatch(value))


@dataclass(slots=True)
class CUResult:
    status: str
    analyzer_id: str
    # Concatenated Markdown across all ``contents`` (empty if CU returned none).
    markdown: str
    # First content's extracted fields (CU field name -> value object).
    fields: dict[str, Any] = field(default_factory=dict)
    # Raw ``result.contents`` list (per-page/segment markdown + grounding).
    contents: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[Any] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    content_filters: list[Any] = field(default_factory=list)
    # Full GET response envelope, retained for diagnostics / future grounding.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status.lower() == "succeeded"

    @property
    def page_count(self) -> int | None:
        page_total = 0.0
        saw_page_meter = False
        for key in _PAGE_USAGE_METERS:
            value = self.usage.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                # Only a *non-zero* meter counts as "the service told us pages".
                # A real CU response zero-fills every documentPages* key, so
                # treating a present-but-zero key as authoritative made the
                # segment-count fallback dead and reported 0 pages for analyzers
                # that have no page meter at all (audio/video/image search).
                page_total += float(value)
                saw_page_meter = True
        if saw_page_meter:
            return int(ceil(page_total))
        return len(self.contents) or None

    @property
    def metered_page_count(self) -> int | None:
        """Pages the *service* billed, or ``None`` when it billed none.

        Deliberately narrower than :attr:`page_count`: it never falls back to
        ``len(contents)``. The fallback is right for a provider whose contents
        *are* pages (Mistral OCR returns one content per page), but for a
        Content Understanding audio/video analyzer ``contents`` are timed
        segments, so billing them as pages invents chargeable units the service
        never metered. Anything that writes ``billing_unit="page"`` for CU must
        read this, not :attr:`page_count`.
        """
        total = 0.0
        seen = False
        for key in _PAGE_USAGE_METERS:
            value = self.usage.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                total += float(value)
                seen = True
        return int(ceil(total)) if seen else None

    def contextualization_tokens_by_tier(self) -> dict[str, int]:
        """Contextualization tokens keyed by the priced model id, per tier.

        Content Understanding reports the two tiers as *separate* usage
        properties: ``contextualizationTokens`` (standard) and, in the
        2026-06-01-preview API, ``advancedContextualizationTokens`` (advanced —
        the rate that ``advanced.*`` and ``agentic.*`` workflows are billed at).
        Reading only the first left every advanced/agentic analyzer — the five
        tax analyzers and agentic mode — recording no contextualization usage at
        all, while a locally authored analyzer label decided which price row the
        standard tokens landed on. The tier now comes from the provider.
        """
        tiers = {
            "contextualizationTokens": (
                "content-understanding-contextualization-standard"
            ),
            "advancedContextualizationTokens": (
                "content-understanding-contextualization-advanced"
            ),
        }
        out: dict[str, int] = {}
        for key, model_id in tiers.items():
            value = self.usage.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                out[model_id] = out.get(model_id, 0) + int(value)
        return out

    def page_usage_by_meter(self) -> dict[str, int]:
        meters: dict[str, int] = {}
        for key, model_id in _PAGE_USAGE_METERS.items():
            value = self.usage.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                meters[model_id] = meters.get(model_id, 0) + int(
                    ceil(float(value))
                )
        return meters

    def model_token_counts(self) -> tuple[int, int, int, bool]:
        tokens = self.usage.get("tokens")
        if not isinstance(tokens, dict):
            return 0, 0, 0, False
        prompt = 0
        completion = 0
        known = False
        for raw_name, raw_value in tokens.items():
            if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
                continue
            value = max(0, int(raw_value))
            name = str(raw_name).lower()
            if name.endswith("-output"):
                completion += value
            else:
                # Completion-model input and embedding tokens are both provider
                # input work; keep them out of the completion bucket.
                prompt += value
            known = True
        return prompt, completion, prompt + completion, known

    def token_usage_by_model(self) -> dict[str, tuple[int, int]]:
        tokens = self.usage.get("tokens")
        if not isinstance(tokens, dict):
            return {}
        usage: dict[str, list[int]] = {}
        for raw_name, raw_value in tokens.items():
            if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
                continue
            name = str(raw_name)
            model = name
            bucket = 0
            if name.endswith("-input"):
                model = name.removesuffix("-input")
            elif name.endswith("-output"):
                model = name.removesuffix("-output")
                bucket = 1
            counts = usage.setdefault(model, [0, 0])
            counts[bucket] += max(0, int(raw_value))
        return {model: (counts[0], counts[1]) for model, counts in usage.items()}

    def field_evidence_summary(self) -> dict[str, int | float | None]:
        confidences: list[float] = []
        grounded = 0
        stack: list[Any] = [self.fields]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                confidence = value.get("confidence")
                if (
                    isinstance(confidence, (int, float))
                    and not isinstance(confidence, bool)
                    and 0 <= float(confidence) <= 1
                ):
                    confidences.append(float(confidence))
                source = value.get("source")
                if source not in (None, "", [], {}):
                    grounded += 1
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        return {
            "confidenceCount": len(confidences),
            "groundedFieldCount": grounded,
            "averageConfidence": (
                round(sum(confidences) / len(confidences), 6)
                if confidences
                else None
            ),
            "minimumConfidence": (
                round(min(confidences), 6) if confidences else None
            ),
        }


def parse_result(body: dict[str, Any]) -> CUResult:
    """Normalize a CU analyzer-results GET body into a :class:`CUResult`."""
    nested = body.get("result")
    result = nested if isinstance(nested, dict) else body
    status = str(body.get("status") or ("Succeeded" if result.get("contents") else ""))
    analyzer_id = str(result.get("analyzerId", "") or "")
    contents = result.get("contents") or []
    if not isinstance(contents, list):
        contents = []
    parts = [
        c["markdown"]
        for c in contents
        if isinstance(c, dict) and isinstance(c.get("markdown"), str) and c["markdown"].strip()
    ]
    markdown = "\n\n".join(parts)
    fields: dict[str, Any] = {}
    for c in contents:
        if isinstance(c, dict) and isinstance(c.get("fields"), dict) and c["fields"]:
            fields = c["fields"]
            break
    warnings = result.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [warnings]
    usage = body.get("usage") or result.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    content_filters = (
        body.get("content_filters") or result.get("content_filters") or []
    )
    if not isinstance(content_filters, list):
        content_filters = [content_filters]
    return CUResult(
        status=status,
        analyzer_id=analyzer_id,
        markdown=markdown,
        fields=fields,
        contents=contents,
        warnings=warnings,
        usage=usage,
        content_filters=content_filters,
        raw=body,
    )
