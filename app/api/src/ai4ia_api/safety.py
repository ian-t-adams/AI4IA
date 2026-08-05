"""Normalizes Azure content-safety annotations into a display-safe shape.

AI4IA deploys every model under an **annotate-only** Responsible AI policy
(`infra/modules/foundry.bicep`): every filter is enabled, none of them block.
Foundry therefore returns a verdict for each category on every turn, and the app
previously discarded all of it -- so the safety system ran, cost nothing to
consult, and was invisible to the person best placed to judge the result.

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
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Severities Azure emits for the four harm categories, weakest first. Anything
# outside this list is passed through untouched rather than guessed at, so a new
# provider value surfaces as itself instead of being silently downgraded.
SEVERITY_ORDER = ("safe", "low", "medium", "high")

# Hard cap: a malformed or hostile payload must not be able to inflate a message
# document. There are ~11 configured filters, so this is generous.
MAX_SIGNALS = 32


class SafetySignal(BaseModel):
    """One category's verdict for one half of the exchange."""

    category: str
    # "prompt" (what the user sent) or "completion" (what the model produced).
    scope: str
    # Harm categories only; None for detection-style filters.
    severity: str | None = None
    # Detection-style filters only (jailbreak, protected material); None for
    # harm categories.
    detected: bool | None = None
    # Whether the PLATFORM blocked on this signal. Always False under the
    # annotate-only policy; retained so that flipping the policy to blocking
    # shows up here rather than silently changing behaviour with no signal.
    filtered: bool = False

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
    """The annotate-only safety verdicts for one turn."""

    signals: list[SafetySignal] = Field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return any(s.is_notable for s in self.signals)

    @property
    def notable(self) -> list[SafetySignal]:
        return [s for s in self.signals if s.is_notable]


def _parse_group(results: Any, scope: str) -> list[SafetySignal]:
    """Turn one ``content_filter_results`` dict into signals."""
    if not isinstance(results, dict):
        return []
    signals: list[SafetySignal] = []
    for category, verdict in results.items():
        if not isinstance(category, str) or not isinstance(verdict, dict):
            continue
        severity = verdict.get("severity")
        detected = verdict.get("detected")
        signals.append(
            SafetySignal(
                # Bound the label: it is rendered, and it comes from upstream.
                category=category[:64],
                scope=scope,
                severity=severity[:32] if isinstance(severity, str) else None,
                detected=detected if isinstance(detected, bool) else None,
                filtered=bool(verdict.get("filtered")),
            )
        )
    return signals


def parse_safety(payload: Any) -> MessageSafety | None:
    """Extract annotations from a chat-completions response body or SSE chunk.

    Returns ``None`` when the payload carries no annotations at all, so callers
    can distinguish "the platform said nothing" from "the platform said
    everything is safe" -- only the second is evidence the filters ran.

    Never raises: a turn must not fail because its annotations were malformed.
    """
    if not isinstance(payload, dict):
        return None

    signals: list[SafetySignal] = []

    for entry in payload.get("prompt_filter_results") or []:
        if isinstance(entry, dict):
            signals.extend(_parse_group(entry.get("content_filter_results"), "prompt"))

    for choice in payload.get("choices") or []:
        if isinstance(choice, dict):
            signals.extend(_parse_group(choice.get("content_filter_results"), "completion"))

    if not signals:
        return None
    return MessageSafety(signals=signals[:MAX_SIGNALS])


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
    merged: dict[tuple[str, str], SafetySignal] = {
        (s.category, s.scope): s for s in left.signals
    }
    for signal in right.signals:
        merged[(signal.category, signal.scope)] = signal
    return MessageSafety(signals=list(merged.values())[:MAX_SIGNALS])
