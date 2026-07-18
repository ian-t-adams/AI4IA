"""Parsed result of a Content Understanding analyze operation.

The CU GET response is ``{id, status, result:{analyzerId, contents:[{markdown,
fields,...}], warnings}}``. We normalize it to a flat, RAG-ready shape: the
concatenated Markdown across contents (the parse output we index + store) plus the
extracted fields and the raw envelope (kept for grounding/citations later).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Terminal CU operation states (case-insensitive). ``Running``/``NotStarted`` mean
# keep polling.
TERMINAL_STATES = frozenset({"succeeded", "failed"})

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
    # Full GET response envelope, retained for diagnostics / future grounding.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status.lower() == "succeeded"


def parse_result(body: dict[str, Any]) -> CUResult:
    """Normalize a CU analyzer-results GET body into a :class:`CUResult`."""
    status = str(body.get("status", ""))
    result = body.get("result") or {}
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
    return CUResult(
        status=status,
        analyzer_id=analyzer_id,
        markdown=markdown,
        fields=fields,
        contents=contents,
        warnings=warnings,
        raw=body,
    )
