"""Reassemble ONE streamed chat-completions iteration into a complete turn step.

The agent runtime used to run each model round trip to completion
(``gateway.complete``) before it could decide whether a tool was wanted, so a
tool-using turn emitted nothing until the whole round trip finished (audit
finding P1-16). This module is the missing half: it consumes ``gateway.stream``
for a single iteration, hands every assistant text increment to a callback the
moment it arrives, and *rebuilds* the ``tool_calls`` array the iteration asked
for so the runtime's existing governance path is unchanged downstream.

Rebuilding is the fiddly part, and it is fiddly for a specific reason: a streamed
tool call is not delivered as an object. It arrives as a sequence of partial
fragments that must be merged by slot::

    delta.tool_calls = [{"index": 0, "id": "call_a", "type": "function",
                         "function": {"name": "web_search", "arguments": ""}}]
    delta.tool_calls = [{"index": 0, "function": {"arguments": "{\\"qu"}}]
    delta.tool_calls = [{"index": 0, "function": {"arguments": "ery\\":1}"}}]

Three properties of that shape drive the code below:

* ``arguments`` is split at arbitrary byte boundaries — often mid-token, mid-key
  or mid-escape — so a fragment is NEVER independently parseable as JSON. Only
  the concatenation is. Parsing early is the classic bug here.
* ``id``/``type``/``function.name`` are normally sent once, on the first fragment
  for a slot, and omitted (or empty) afterwards. A later empty value must not
  erase the real one.
* ``index`` — not position in the array — identifies the slot, which is what
  makes *parallel* tool calls interleave safely.

Everything here is defensive by construction: a malformed fragment is skipped
rather than raised, because this runs inside the one code path every chat request
takes and a provider quirk must degrade to "no tool call" rather than to a 500.
An oversized argument accumulation is truncated instead of buffered without
bound; the truncated string then fails ``json.loads`` in the runtime and becomes
the ordinary, already-tested ``invalid_arguments`` structured tool result.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..gateway.client import ModelGatewayError

logger = logging.getLogger(__name__)

# Ceiling on the accumulated ``arguments`` string for a single tool call. Model
# output budgets put every real call orders of magnitude below this; the cap
# exists so a malfunctioning or hostile upstream cannot grow one turn's buffers
# without bound. Exceeding it yields a truncated (therefore unparseable)
# argument string, which the runtime already answers with a structured
# ``invalid_arguments`` tool result rather than a crash.
_MAX_ARGUMENT_CHARS = 128 * 1024

# What the caller is told when the gateway reports an error *inside* an
# otherwise-200 SSE body. Deliberately fixed and content-free: the raw frame can
# carry upstream deployment detail, and ``_agentic_stream`` surfaces
# ``ModelGatewayError.detail`` to the client verbatim. This mirrors the sanitized
# single error the plain (non-tool) streaming path already emits.
_STREAM_FAILED = "Model stream failed."


@dataclass
class _Slot:
    """One in-progress tool call, keyed by its streamed ``index``."""

    seq: int
    index: int | None = None
    id: str = ""
    type: str = ""
    name: str = ""
    arguments: str = ""
    overflowed: bool = False

    def sort_key(self) -> tuple[int, int]:
        return (self.index if self.index is not None else self.seq, self.seq)


class ToolCallAccumulator:
    """Merge streamed ``delta.tool_calls`` fragments back into whole calls."""

    def __init__(self) -> None:
        self._slots: dict[object, _Slot] = {}
        self._by_id: dict[str, object] = {}
        self._last_key: object | None = None
        self._seq = 0

    def _slot_for(self, fragment: dict[str, Any]) -> _Slot:
        """Resolve the slot a fragment belongs to.

        ``index`` is authoritative when present. Otherwise fall back to the
        fragment's ``id`` (some providers key by id alone), and finally to the
        most recently touched slot — which is the only sane reading of a bare
        ``{"function": {"arguments": "..."}}`` continuation.
        """
        raw_index = fragment.get("index")
        index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else None
        call_id = fragment.get("id")
        call_id = call_id if isinstance(call_id, str) and call_id else ""

        key: object | None = None
        if index is not None:
            key = ("index", index)
        elif call_id and call_id in self._by_id:
            key = self._by_id[call_id]
        elif call_id:
            key = ("id", call_id)
        elif self._last_key is not None:
            key = self._last_key
        else:
            key = ("seq", self._seq)

        slot = self._slots.get(key)
        if slot is None:
            slot = _Slot(seq=self._seq, index=index)
            self._seq += 1
            self._slots[key] = slot
        if call_id:
            self._by_id.setdefault(call_id, key)
        self._last_key = key
        return slot

    def add(self, fragments: Sequence[Any]) -> None:
        """Fold one chunk's ``delta.tool_calls`` list into the open slots."""
        if not isinstance(fragments, Sequence) or isinstance(fragments, (str, bytes)):
            return
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            slot = self._slot_for(fragment)
            call_id = fragment.get("id")
            # ``not slot.id`` is the whole guard, and it is load-bearing in both
            # directions: the first non-empty value wins, so a later empty ``id``
            # cannot erase it and a later *different* one cannot hijack the slot.
            # An additional ``and call_id`` here would read as defensive but be
            # dead — ``not slot.id`` already makes assigning "" a no-op — and a
            # dead condition is indistinguishable from a working one until
            # something mutates it. (It was written that way first; the mutation
            # test survived, which is how it was found.)
            if isinstance(call_id, str) and not slot.id:
                slot.id = call_id
            call_type = fragment.get("type")
            if isinstance(call_type, str) and not slot.type:
                slot.type = call_type
            function = fragment.get("function")
            if not isinstance(function, dict):
                continue
            # ``name`` is normally whole on the first fragment, but the wire
            # format permits splitting it, so append rather than assign.
            name = function.get("name")
            if isinstance(name, str) and name:
                slot.name += name
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                room = _MAX_ARGUMENT_CHARS - len(slot.arguments)
                if room > 0:
                    slot.arguments += arguments[:room]
                if len(arguments) > room and not slot.overflowed:
                    slot.overflowed = True
                    logger.warning(
                        "streamed tool-call arguments exceeded %d chars; truncating",
                        _MAX_ARGUMENT_CHARS,
                    )

    def finalize(self) -> list[dict[str, Any]]:
        """Whole tool calls in streamed slot order, shaped like the non-stream API.

        A slot with neither a name nor any arguments never carried a call and is
        dropped; anything else is emitted so the runtime can govern it (an
        unnamed-but-argument-bearing call must still reach the governance path,
        where it is refused by name, rather than vanishing here).
        """
        out: list[dict[str, Any]] = []
        for slot in sorted(self._slots.values(), key=_Slot.sort_key):
            if not slot.name and not slot.arguments:
                continue
            out.append(
                {
                    "id": slot.id or None,
                    "type": slot.type or "function",
                    "function": {"name": slot.name, "arguments": slot.arguments},
                }
            )
        return out


@dataclass
class StreamedIteration:
    """The completed shape of one streamed model round trip."""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None


async def stream_iteration(
    *,
    gateway: Any,
    deployment: str,
    messages: Sequence[dict[str, Any]],
    params: dict[str, Any] | None,
    correlation_id: str | None,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
    on_usage: Callable[[dict[str, Any]], None] | None = None,
) -> StreamedIteration:
    """Run ONE model iteration over SSE, forwarding text the instant it arrives.

    Text is handed to ``on_delta`` before the chunk is inspected for tool-call
    fragments, because time-to-first-token is the whole point of this path: any
    work done first is latency the user pays for.

    An ``error`` object inside the SSE body is raised as a *sanitized*
    :class:`ModelGatewayError`, matching the plain streaming path's contract that
    a stream failure surfaces exactly one error frame and never echoes upstream
    detail.
    """
    accumulator = ToolCallAccumulator()
    content: list[str] = []
    usage: dict[str, Any] | None = None

    async for chunk in gateway.stream(
        deployment=deployment,
        messages=messages,
        params=params,
        correlation_id=correlation_id,
    ):
        if chunk.usage:
            usage = chunk.usage
            if on_usage is not None:
                on_usage(chunk.usage)
        if chunk.delta:
            content.append(chunk.delta)
            if on_delta is not None:
                await on_delta(chunk.delta)
        if chunk.done or not chunk.raw:
            continue
        try:
            payload = json.loads(chunk.raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("error"):
            raise ModelGatewayError(502, _STREAM_FAILED)
        choices = payload.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            fragments = delta.get("tool_calls")
            if fragments is not None:
                accumulator.add(fragments)

    return StreamedIteration(
        content="".join(content),
        tool_calls=accumulator.finalize(),
        usage=usage,
    )
