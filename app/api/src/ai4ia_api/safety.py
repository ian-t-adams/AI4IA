"""Normalizes Azure content-safety annotations into a display-safe shape.

AI4IA deploys every model under an **annotate-only** Responsible AI policy
(`infra/modules/foundry.bicep`): configured filters assess but do not block.
Where a provider/API surface returns verdicts, the app preserves every category;
where it returns none, the app records coverage as unavailable instead of
inventing a clean assessment.

This module turns that raw provider payload into a bounded, stable structure the
UI can show. It is deliberately **descriptive, not enforcing**: nothing here
blocks, rewrites, or refuses. It reports what the platform observed.

Shape returned by Azure OpenAI (chat completions)::

    "prompt_filter_results": [
        {"prompt_index": 0, "content_filter_results": {
            "hate": {"filtered": false, "severity": "safe"},
            "jailbreak": {"filtered": false, "detected": false}}}],
    "choices": [
        {"content_filter_results": {
            "violence": {"filtered": false, "severity": "low"},
            "protected_material_text": {"filtered": false, "detected": false}}}]

Two verdict styles appear: harm categories carry a ``severity``, while the
detection filters (jailbreak, protected material) carry a boolean ``detected``.
Both are preserved rather than flattened, because "no jailbreak detected" and
"hate: safe" are different statements.

Two further things this module reports, and one it refuses to:

* A **normalized ordinal** alongside the provider's own severity string. "medium"
  is only meaningful to someone who already knows the scale has four steps, so
  each harm verdict also carries ``severityLevel`` (safe=0, low=1, medium=2,
  high=3) and the UI can render "medium (level 2 of 3)". The raw provider value
  is never overwritten, and a severity outside the known scale stays unranked
  (``None``) rather than being guessed at.
* An explicit **assessment status**. Previously "the provider returned no
  annotations" and "this turn has no safety record" were the same ``None``, so a
  non-Azure surface or an annotation-less turn silently rendered no panel at
  all -- indistinguishable, to a reader, from a clean bill of health. A turn can
  now carry a record whose ``status`` is ``unavailable``, which says plainly
  that no platform guardrail assessment was returned.

What it will not do is invent a verdict. ``unavailable`` is the honest answer
when nothing was reported, and nothing here upgrades an absent annotation into
"safe".
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# Severities Azure emits for the four harm categories, weakest first. Anything
# outside this list is passed through untouched rather than guessed at, so a new
# provider value surfaces as itself instead of being silently downgraded.
SEVERITY_ORDER = ("safe", "low", "medium", "high")

# Ordinal for each known severity, and the top of the scale. Exported so the web
# app's "level N of M" rendering and this module cannot drift apart.
SEVERITY_LEVELS = {name: index for index, name in enumerate(SEVERITY_ORDER)}
MAX_SEVERITY_LEVEL = len(SEVERITY_ORDER) - 1

# Hard cap: a malformed or hostile payload must not be able to inflate a message
# document. There are ~11 configured filters, so this is generous.
MAX_SIGNALS = 32


def severity_level(severity: str | None) -> int | None:
    """Normalized ordinal for a provider severity, or ``None`` if unranked."""
    if not isinstance(severity, str):
        return None
    return SEVERITY_LEVELS.get(severity.strip().lower())


class SafetyStatus(str, Enum):
    """Whether a platform guardrail assessment exists for this turn."""

    # The provider returned annotations and they are in ``signals``.
    reported = "reported"
    # Some verdicts were returned, but at least one content-filter evaluator
    # reported an error. The visible signals remain useful; the gap stays explicit.
    partial = "partial"
    # No assessment came back: a surface that does not annotate, a tool-loop
    # turn whose provider response was not annotated, or a provider that simply
    # returned none. Deliberately NOT the same as "nothing was flagged".
    unavailable = "unavailable"


class SafetySignal(BaseModel):
    """One category's verdict for one half of the exchange."""

    category: str
    # "prompt" (what the user sent) or "completion" (what the model produced).
    scope: str
    # Harm categories only; None for detection-style filters. This is the
    # PROVIDER's own string, preserved verbatim.
    severity: str | None = None
    # Normalized rank of ``severity`` on a 0..MAX_SEVERITY_LEVEL scale. ``None``
    # for detection-style filters and for any severity outside the known scale,
    # so an unrecognized provider value is shown as itself rather than ranked
    # against a scale it may not belong to.
    severityLevel: int | None = None
    # Detection-style filters only (jailbreak, protected material); None for
    # harm categories.
    detected: bool | None = None
    # Whether the PLATFORM reported filtering on this signal. Expected false
    # under the annotate-only policy; retained so provider-native enforcement or
    # policy drift shows up instead of being mislabeled as application behavior.
    filtered: bool = False
    # Agent/tool loops can make several model calls. None for a single-call turn
    # or while streamed chunks from one call are still being merged.
    modelCall: int | None = None
    # Linked-agent assessments keep their local model-call ordinal and name the
    # agent that produced them.
    agent: str | None = None

    @property
    def is_notable(self) -> bool:
        """Whether this verdict is worth drawing attention to.

        ``safe``/not-detected verdicts are the overwhelming majority and are not
        interesting on their own; they matter only as the denominator.
        """
        if self.filtered:
            return True
        if self.detected is not None:
            return bool(self.detected)
        return self.severity is not None and self.severity != "safe"


class MessageSafety(BaseModel):
    """The annotate-only safety verdicts for one turn, and their provenance.

    ``status``/``provider``/``mode``/``coverage`` are additive with defaults that
    describe exactly what an older Cosmos row was: a set of reported Azure
    annotations under the annotate-only policy. A row written before these
    fields existed therefore loads with the same meaning it always had.
    """

    signals: list[SafetySignal] = Field(default_factory=list)
    status: SafetyStatus = SafetyStatus.reported
    # Which platform produced (or failed to produce) the assessment.
    provider: str | None = None
    # Enforcement posture the assessment ran under. Annotate-only means nothing
    # was blocked or rewritten; it is recorded so a future blocking policy is a
    # visible change rather than a silent one.
    mode: str = "annotate_only"
    # Halves of the exchange the provider actually assessed ("prompt",
    # "completion"). Empty on an unavailable assessment.
    coverage: list[str] = Field(default_factory=list)
    # Count before the persisted list bound. A provider that returns more than
    # MAX_SIGNALS is visibly partial rather than silently shortened.
    signalCount: int = 0
    truncated: bool = False
    errors: list[str] = Field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return any(s.is_notable for s in self.signals)

    @property
    def notable(self) -> list[SafetySignal]:
        return [s for s in self.signals if s.is_notable]

    @property
    def assessed(self) -> bool:
        return self.status in {SafetyStatus.reported, SafetyStatus.partial}


def _parse_group(results: Any, scope: str) -> list[SafetySignal]:
    """Turn one ``content_filter_results`` dict into signals."""
    if not isinstance(results, dict):
        return []
    signals: list[SafetySignal] = []
    for category, verdict in results.items():
        if category == "error":
            continue
        if not isinstance(category, str) or not isinstance(verdict, dict):
            continue
        severity = verdict.get("severity")
        detected = verdict.get("detected")
        severity_text = severity[:32] if isinstance(severity, str) else None
        signals.append(
            SafetySignal(
                # Bound the label: it is rendered, and it comes from upstream.
                category=category[:64],
                scope=scope,
                severity=severity_text,
                severityLevel=severity_level(severity_text),
                detected=detected if isinstance(detected, bool) else None,
                filtered=bool(verdict.get("filtered")),
            )
        )
    return signals


def _coverage(signals: list[SafetySignal]) -> list[str]:
    """Halves of the exchange that were actually assessed, in a stable order."""
    scopes = {signal.scope for signal in signals}
    return [scope for scope in ("prompt", "completion") if scope in scopes]


def _filter_error_code(results: Any) -> str | None:
    if not isinstance(results, dict):
        return None
    error = results.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if not isinstance(code, str) or not code.strip():
        return "content_filter_error"
    normalized = "".join(
        character
        for character in code.strip()
        if character.isalnum() or character in {"_", "-", "."}
    )
    return normalized[:64] or "content_filter_error"


def _bound_signals(signals: list[SafetySignal]) -> list[SafetySignal]:
    """Keep notable and latest-call evidence before ordinary clean rows."""
    ranked = sorted(
        enumerate(signals),
        key=lambda item: (
            0 if item[1].is_notable else 1,
            -(item[1].modelCall or 0),
            item[0],
        ),
    )
    return [signal for _, signal in ranked[:MAX_SIGNALS]]


def parse_safety(payload: Any) -> MessageSafety | None:
    """Extract annotations from a chat-completions response body or SSE chunk.

    Returns ``None`` when the payload carries no annotations at all, so callers
    can distinguish "the platform said nothing" from "the platform said
    everything is safe" -- only the second is evidence the filters ran. Callers
    that must persist a record either way wrap this in
    :func:`safety_assessment`.

    Never raises: a turn must not fail because its annotations were malformed.
    """
    if not isinstance(payload, dict):
        return None

    signals: list[SafetySignal] = []
    errors: list[str] = []

    for entry in payload.get("prompt_filter_results") or []:
        if isinstance(entry, dict):
            results = entry.get("content_filter_results")
            if code := _filter_error_code(results):
                errors.append(code)
            signals.extend(_parse_group(results, "prompt"))

    for choice in payload.get("choices") or []:
        if isinstance(choice, dict):
            results = choice.get("content_filter_results")
            if code := _filter_error_code(results):
                errors.append(code)
            signals.extend(_parse_group(results, "completion"))

    # Responses API uses a Foundry extension at the top level rather than the
    # chat-completions fields above. Preserve the same stable signal model so a
    # model routed to `responses` does not silently lose guardrail evidence.
    for entry in payload.get("content_filters") or []:
        if not isinstance(entry, dict):
            continue
        source_type = entry.get("source_type")
        scope = (
            source_type
            if source_type in {"prompt", "completion"}
            else "unknown"
        )
        results = entry.get("content_filter_results")
        if code := _filter_error_code(results):
            errors.append(code)
        group = _parse_group(results, scope)
        if entry.get("blocked") is True:
            for signal in group:
                signal.filtered = True
        signals.extend(group)

    if not signals and not errors:
        return None
    if not signals:
        return MessageSafety(
            status=SafetyStatus.unavailable,
            errors=list(dict.fromkeys(errors))[:8],
        )
    bounded = _bound_signals(signals)
    return MessageSafety(
        signals=bounded,
        status=(
            SafetyStatus.partial
            if errors
            else SafetyStatus.reported
        ),
        coverage=_coverage(bounded),
        signalCount=len(signals),
        truncated=len(signals) > len(bounded),
        errors=list(dict.fromkeys(errors))[:8],
    )


def provider_for_api(api: str | None) -> str:
    """Which platform's guardrails a turn on ``api`` would have been assessed by.

    A label for the receipt/panel, not a routing decision. Everything AI4IA
    serves goes through Foundry, so an unrecognized surface reports the
    deployment platform rather than guessing a vendor.
    """
    if api == "anthropic":
        return "azure_foundry_anthropic"
    return "azure_openai"


def unavailable_safety(provider: str | None = None) -> MessageSafety:
    """A record stating plainly that no guardrail assessment came back.

    Not a verdict. It carries no signals and claims nothing about the content;
    its entire purpose is to stop the absence of an assessment from being
    invisible, which under an annotate-only policy is indistinguishable from a
    clean result to anyone reading the conversation.
    """
    return MessageSafety(
        signals=[],
        status=SafetyStatus.unavailable,
        provider=provider,
        coverage=[],
        signalCount=0,
    )


def attributed_safety(
    assessment: MessageSafety | None,
    provider: str | None,
) -> MessageSafety:
    """Attach provider provenance, or return an explicit unavailable record."""
    if assessment is None:
        return unavailable_safety(provider)
    attributed = assessment.model_copy(deep=True)
    attributed.provider = provider
    return attributed


def safety_assessment(payload: Any, *, provider: str | None = None) -> MessageSafety:
    """Annotations from ``payload`` if there are any, else an explicit gap.

    Total, so every turn that runs through it persists an assessment record and
    no path can leave a reader unable to tell "clean" from "never checked".
    """
    parsed = parse_safety(payload)
    if parsed is None:
        return unavailable_safety(provider)
    parsed.provider = provider
    return parsed


def merge_safety(
    left: MessageSafety | None, right: MessageSafety | None
) -> MessageSafety | None:
    """Combine annotations across streamed chunks.

    A streamed turn reports prompt annotations on the first chunk and completion
    annotations on a later one, so the full picture only exists after merging.
    Later verdicts for the same (category, scope) win: Azure refines a
    completion verdict as more of the answer is produced, and the final word is
    the accurate one.
    """
    if left is None:
        return right
    if right is None:
        return left
    merged: dict[tuple[str, str, int | None, str | None], SafetySignal] = {
        (s.category, s.scope, s.modelCall, s.agent): s for s in left.signals
    }
    for signal in right.signals:
        merged[(signal.category, signal.scope, signal.modelCall, signal.agent)] = signal
    all_signals = list(merged.values())
    omitted = max(0, left.signalCount - len(left.signals)) + max(
        0, right.signalCount - len(right.signals)
    )
    signals = _bound_signals(all_signals)
    signal_count = len(all_signals) + omitted
    errors = list(dict.fromkeys([*left.errors, *right.errors]))[:8]
    return MessageSafety(
        signals=signals,
        # Any reported half makes the combined record a reported assessment;
        # merging an unavailable placeholder must not erase real annotations.
        status=(
            SafetyStatus.partial
            if errors and signals
            else SafetyStatus.reported
            if signals
            else (left.status if left.status is right.status else SafetyStatus.unavailable)
        ),
        provider=right.provider or left.provider,
        mode=right.mode or left.mode,
        coverage=_coverage(signals),
        signalCount=signal_count,
        truncated=(
            left.truncated
            or right.truncated
            or signal_count > len(signals)
        ),
        errors=errors,
    )
