"""Privacy-safe monotonic timing for one accepted chat turn."""
from __future__ import annotations

import contextvars
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class ChatTiming:
    def __init__(
        self,
        *,
        stream: bool,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = monotonic
        self._accepted_at = monotonic()
        self._first_content_at: float | None = None
        self._last_gateway_at: float | None = None
        self._gateway_seconds = 0.0
        self._gateway_calls = 0
        self._persistence_seconds = 0.0
        self.stream = stream
        self.tool_loop = False

    def mark_tool_loop(self) -> None:
        self.tool_loop = True

    def mark_first_content(self) -> None:
        if self._first_content_at is None:
            self._first_content_at = self._clock()

    def gateway_started(self) -> float:
        return self._clock()

    def gateway_finished(self, started_at: float) -> None:
        now = self._clock()
        self._gateway_seconds += max(0.0, now - started_at)
        self._gateway_calls += 1
        self._last_gateway_at = now

    async def measure_persistence(self, operation: Awaitable[T]) -> T:
        started_at = self._clock()
        try:
            return await operation
        finally:
            self._persistence_seconds += max(0.0, self._clock() - started_at)

    def terminal_attributes(self) -> dict[str, object]:
        terminal_at = self._clock()
        first_content_ms = (
            _milliseconds(self._first_content_at - self._accepted_at)
            if self._first_content_at is not None
            else None
        )
        finalization_ms = (
            _milliseconds(terminal_at - self._last_gateway_at)
            if self._last_gateway_at is not None
            else None
        )
        return {
            "timingCoverage": "chat-v1",
            "timingAvailable": True,
            "turnTotalMs": _milliseconds(terminal_at - self._accepted_at),
            "firstContentMs": first_content_ms,
            "firstContentObserved": self._first_content_at is not None,
            "gatewayMs": _milliseconds(self._gateway_seconds),
            "gatewayCalls": self._gateway_calls,
            "gatewayTimingAvailable": self._gateway_calls > 0,
            "persistenceMs": _milliseconds(self._persistence_seconds),
            "finalizationMs": finalization_ms,
            "stream": self.stream,
            "toolLoop": self.tool_loop,
        }


def _milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1000))


_current_timing: contextvars.ContextVar[ChatTiming | None] = contextvars.ContextVar(
    "chat_timing", default=None
)


def bind_chat_timing(timing: ChatTiming) -> None:
    _current_timing.set(timing)


def current_chat_timing() -> ChatTiming | None:
    return _current_timing.get()
