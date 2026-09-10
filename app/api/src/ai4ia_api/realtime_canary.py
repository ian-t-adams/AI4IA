"""Reduction-only GA setup envelope, selected exclusively by authenticated policy."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

SETUP_INPUT = (
    '{"type":"session.update","session":{"turn_detection":null,'
    '"modalities":["text"],"max_response_output_tokens":64}}'
)
SETUP_MAX_SECONDS = 15.0
SETUP_MAX_FRAME_BYTES = 16 * 1024
SETUP_MAX_TOTAL_BYTES = 32 * 1024


class RealtimeSetupRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("The realtime setup-only envelope was refused.")


def _object(frame: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RealtimeSetupRejected()
            result[key] = value
        return result

    def nonfinite(_: str) -> None:
        raise RealtimeSetupRejected()

    try:
        if len(frame.encode("utf-8")) > SETUP_MAX_FRAME_BYTES:
            raise RealtimeSetupRejected()
        value = json.loads(frame, object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RealtimeSetupRejected() from exc
    if not isinstance(value, dict):
        raise RealtimeSetupRejected()
    pending: list[tuple[Any, int]] = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 12 or count > 2048:
            raise RealtimeSetupRejected()
        if isinstance(item, float) and not math.isfinite(item):
            raise RealtimeSetupRejected()
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def _canonical(frame: str) -> str:
    try:
        return json.dumps(_object(frame), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, OverflowError) as exc:
        raise RealtimeSetupRejected() from exc


@dataclass
class RealtimeSetup:
    owner_id: str
    deployment: str
    endpoint: str
    rewritten_update: str
    authorize: Callable[[], Awaitable[bool]]
    clock: Callable[[], float] = time.monotonic
    deadline: float = field(init=False)
    opened: bool = False
    received_update: bool = False
    send_started: bool = False
    sent_update: bool = False
    server_phase: int = 0
    server_bytes: int = 0

    def __post_init__(self) -> None:
        self.deadline = self.clock() + SETUP_MAX_SECONDS
        _canonical(self.rewritten_update)

    async def check_current(self) -> None:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RealtimeSetupRejected()
        try:
            async with asyncio.timeout(remaining):
                allowed = await self.authorize()
        except TimeoutError as exc:
            raise RealtimeSetupRejected() from exc
        if allowed is not True or self.clock() >= self.deadline:
            raise RealtimeSetupRejected()

    async def admit_open(self, owner: str, deployment: str, payload: dict[str, Any]) -> bool:
        if (
            self.opened or owner != self.owner_id or deployment != self.deployment
            or payload != {
                "operation": "session_open", "endpoint": self.endpoint,
                "protocol": "ga", "provider": "azure_openai",
            }
        ):
            return False
        self.opened = True
        try:
            await self.check_current()
        except RealtimeSetupRejected:
            return False
        return True

    def client_frame(self, frame: str) -> None:
        if (
            not self.opened or self.received_update
            or _canonical(frame) != _canonical(SETUP_INPUT)
        ):
            raise RealtimeSetupRejected()
        self.received_update = True

    async def before_send(self, *, text: str | None, data: bytes | None) -> None:
        if (
            not self.opened or not self.received_update or self.send_started
            or data is not None or text is None
            or _canonical(text) != _canonical(self.rewritten_update)
        ):
            raise RealtimeSetupRejected()
        self.send_started = True
        await self.check_current()
        self.sent_update = True

    async def server_frame(self, *, text: str | None, data: bytes | None) -> bool:
        if not self.opened or data is not None or text is None:
            raise RealtimeSetupRejected()
        payload = _object(text)
        self.server_bytes += len(text.encode("utf-8"))
        if self.server_bytes > SETUP_MAX_TOTAL_BYTES:
            raise RealtimeSetupRejected()
        await self.check_current()
        kind = payload.get("type")
        if kind == "session.created" and self.server_phase == 0:
            self.server_phase = 1
            return False
        if kind == "session.updated" and self.server_phase == 1 and self.sent_update:
            self.server_phase = 2
            return True
        raise RealtimeSetupRejected()


_current: ContextVar[RealtimeSetup | None] = ContextVar("realtime_setup_canary", default=None)


@contextmanager
def realtime_setup_scope(guard: RealtimeSetup | None) -> Iterator[None]:
    token = _current.set(guard)
    try:
        yield
    finally:
        _current.reset(token)


def current_realtime_setup() -> RealtimeSetup | None:
    return _current.get()


async def realtime_canary_dispatch_guard(
    owner_id: str, deployment: str, payload: dict[str, Any],
) -> bool:
    guard = current_realtime_setup()
    return guard is not None and await guard.admit_open(owner_id, deployment, payload)
