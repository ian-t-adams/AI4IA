"""Deterministic intent router for the library compute path (Phase 11C).

Classifies a single chat turn against three branches the design doc's "arc" calls
out for a ready document library:

* ``qa`` — interrogate / cite / summarize. The existing Tier-1/2 RAG + streaming
  path (11B-2) handles this and stays the **front door**.
* ``compute`` — run code over the document (totals, statistics, charts, tabular
  transforms) via the governed ``run_code`` Code Interpreter tool.
* ``transform`` — "adjust & return it": produce a **new versioned artifact** via
  the governed ``export_document`` tool.

The router is intentionally **pure and deterministic** (keyword/regex signals, no
IO, no model call):

* It adds *zero* latency and can never fail a chat turn (the caller wraps it
  best-effort anyway).
* It is fully unit-testable offline.
* Crucially it encodes the repo invariant that **Code Interpreter is never the
  default**: only an explicit compute/tabular or transform signal routes away
  from ``qa``. Anything ambiguous stays ``qa`` (cheap, corpus-search-strong RAG).

The router only decides *whether to offer* the compute tools; the model still
decides whether to call them, and every call remains ownership- and status-gated
in the capability layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    qa = "qa"
    compute = "compute"
    transform = "transform"


# "Adjust & return it" signals: the turn asks for a NEW artifact derived from the
# document. Checked first because such asks often also contain compute words
# ("calculate the totals and save them as a new sheet") but the defining feature
# is that a new versioned blob should be written back.
_TRANSFORM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pat, re.IGNORECASE))
    for name, pat in (
        ("export", r"\bexport(s|ed|ing)?\b"),
        ("save_as", r"\bsave\s+(it\s+|this\s+|that\s+|them\s+)?as\b"),
        ("save_new", r"\bsave\s+(a\s+)?(new|copy|version)\b"),
        ("download", r"\bdownload(able)?\b"),
        ("convert", r"\bconvert(s|ed|ing)?\b"),
        ("reformat", r"\bre-?format(s|ted|ting)?\b"),
        ("rewrite", r"\bre-?writ(e|es|ing|ten)\b"),
        ("transform", r"\btransform(s|ed|ing)?\b"),
        ("translate", r"\btranslat(e|es|ed|ing|ion)\b"),
        ("redact", r"\bredact(s|ed|ing)?\b"),
        ("annotate", r"\bannotat(e|es|ed|ing)\b"),
        ("new_version", r"\b(new|updated|revised|adjusted)\s+(version|copy|file|document|sheet|spreadsheet|csv|workbook)\b"),
        ("make_file", r"\b(make|create|produce|generate|give\s+me)\b.{0,40}\b(file|csv|spreadsheet|workbook|document|report|version|copy)\b"),
        ("adjust_return", r"\badjust(s|ed|ing)?\b.{0,40}\b(return|back|new|copy|version|file)\b"),
        ("fill_in", r"\bfill(\s+in|\s+out)?\b.{0,30}\b(form|template|fields?)\b"),
    )
)

# Compute / tabular / charting signals. Short words use word boundaries so they
# don't fire inside larger words (e.g. "sum" must not match "summary", "min" must
# not match "minutes"). Phrases ("how many", "group by") are matched directly.
_COMPUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pat, re.IGNORECASE))
    for name, pat in (
        ("calculate", r"\bcalculat(e|es|ed|ing|ion|ions)\b"),
        ("compute", r"\bcomput(e|es|ed|ing|ation)\b"),
        ("sum", r"\bsums?\b"),
        ("total", r"\btotals?(ed|ing)?\b"),
        ("subtotal", r"\bsub-?totals?\b"),
        ("average", r"\baverages?\b"),
        ("median", r"\bmedian\b"),
        ("count", r"\bcounts?\b"),
        ("how_many", r"\bhow\s+many\b"),
        ("aggregate", r"\baggregat(e|es|ed|ing|ion)\b"),
        ("group_by", r"\bgroup(ed)?\s+by\b"),
        ("pivot", r"\bpivot\b"),
        ("chart", r"\bcharts?\b"),
        ("plot", r"\bplots?(ted|ting)?\b"),
        ("graph", r"\bgraphs?\b"),
        ("histogram", r"\bhistograms?\b"),
        ("bar_chart", r"\bbar\s+charts?\b"),
        ("correlate", r"\bcorrelat(e|es|ed|ing|ion|ions)\b"),
        ("trend", r"\btrends?\b"),
        ("regression", r"\bregression\b"),
        ("forecast", r"\bforecast(s|ed|ing)?\b"),
        ("percentage", r"\bpercent(age|ages|s)?\b"),
        ("ratio", r"\bratios?\b"),
        ("statistics", r"\bstatistics?\b"),
        ("stats", r"\bstats\b"),
        ("std_dev", r"\bstandard\s+deviation\b"),
        ("variance", r"\bvariance\b"),
        ("min_max", r"\b(minimum|maximum)\b"),
        ("tabulate", r"\btabulat(e|es|ed|ing|ion)\b"),
        ("spreadsheet", r"\b(spread-?sheet|workbook|pivot\s+table)\b"),
        ("rows_cols", r"\b(per\s+row|per\s+column|each\s+row|each\s+column)\b"),
        ("run_code", r"\brun\s+(some\s+|python\s+)?code\b"),
    )
)


@dataclass(slots=True)
class RouteDecision:
    """The router's verdict for one turn.

    ``signals`` lists the matched signal names (for tracing/tests); ``intent`` is
    the branch. ``compute`` is True for both ``compute`` and ``transform`` (both
    *offer* the governed compute toolset).
    """

    intent: Intent
    signals: list[str] = field(default_factory=list)

    @property
    def offers_compute(self) -> bool:
        return self.intent in (Intent.compute, Intent.transform)


def _match(text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    return [name for name, rx in patterns if rx.search(text)]


class IntentRouter:
    """Pure, deterministic classifier of a turn into :class:`Intent`.

    Holds no state and performs no IO; safe to construct once and share. Default
    is ``qa`` and any ambiguity resolves to ``qa`` — the router never routes to
    Code Interpreter "by default", only on an explicit compute/transform signal.
    """

    def classify(self, text: str) -> RouteDecision:
        cleaned = (text or "").strip()
        if not cleaned:
            return RouteDecision(Intent.qa)

        transform_hits = _match(cleaned, _TRANSFORM_PATTERNS)
        if transform_hits:
            return RouteDecision(Intent.transform, transform_hits)

        compute_hits = _match(cleaned, _COMPUTE_PATTERNS)
        if compute_hits:
            return RouteDecision(Intent.compute, compute_hits)

        return RouteDecision(Intent.qa)
