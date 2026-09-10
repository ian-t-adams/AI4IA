"""Reduction-only request scope, never an authorization grant or durable setting."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .sessions.models import Session
    from .sessions.repository import SessionRepository

CANARY_SENTINEL = "Reply with the single word: ready."
CANARY_MAX_OUTPUT_TOKENS = 64


@dataclass(frozen=True)
class RequestConstraints:
    tools: bool = True
    automatic_memory: bool = True
    fresh_session: bool = False


_current: ContextVar[RequestConstraints] = ContextVar(
    "request_constraints", default=RequestConstraints()
)


@dataclass
class _FreshDispatch:
    owner: str
    session_id: str
    epoch: str
    deployment: str
    api: str
    content: str
    consumed: bool = False


_fresh_dispatch: ContextVar[_FreshDispatch | None] = ContextVar("fresh_dispatch", default=None)


def tools_allowed() -> bool:
    return _current.get().tools


def automatic_memory_allowed() -> bool:
    return _current.get().automatic_memory


def fresh_session_required() -> bool:
    return _current.get().fresh_session


@contextmanager
def constrain_request(
    *, tools: bool, automatic_memory: bool, require_fresh_session: bool = False,
) -> Iterator[None]:
    parent = _current.get()
    token = _current.set(RequestConstraints(
        tools=parent.tools and tools,
        automatic_memory=parent.automatic_memory and automatic_memory,
        fresh_session=parent.fresh_session or require_fresh_session,
    ))
    slot = _fresh_dispatch.set(_fresh_dispatch.get() if parent.fresh_session else None)
    try:
        yield
    finally:
        _fresh_dispatch.reset(slot)
        _current.reset(token)


def arm_fresh_dispatch(
    owner_id: str, session: Session, deployment: str, api: str, content: str,
) -> None:
    if (
        not fresh_session_required() or tools_allowed() or automatic_memory_allowed()
        or session.userId != owner_id or session.deletionProtocol != 1
        or not session.freshTurnClaimed or not session.deletionEpoch
        or _fresh_dispatch.get() is not None
    ):
        raise ValueError("A fresh dispatch requires one successfully claimed constrained turn.")
    _fresh_dispatch.set(_FreshDispatch(
        owner_id, session.id, session.deletionEpoch, deployment, api, content,
    ))


def _bounded_payload(
    slot: _FreshDispatch, payload: dict[str, Any], *, sentinel_only: bool, max_output: int,
) -> bool:
    try:
        content_size = len(slot.content.encode("utf-8"))
    except UnicodeError:
        return False
    if (
        not 1 <= content_size <= 4096
        or (sentinel_only and slot.content != CANARY_SENTINEL)
        or payload.get("model", slot.deployment) != slot.deployment
    ):
        return False
    messages = [{"role": "user", "content": slot.content}]
    if slot.api == "responses":
        keys = {"model", "input", "max_output_tokens", "store", "reasoning", "stream"}
        if (
            payload.get("model") != slot.deployment or payload.get("input") != messages
            or payload.get("store") is not False
            or payload.get("reasoning", {"effort": "none"}) != {"effort": "none"}
        ):
            return False
        maximum = payload.get("max_output_tokens")
    elif slot.api == "chat":
        keys = {
            "model", "messages", "max_tokens", "max_completion_tokens",
            "reasoning_effort", "stream", "stream_options",
        }
        if (
            payload.get("messages") != messages
            or payload.get("reasoning_effort", "none") != "none"
            or ("max_tokens" in payload) == ("max_completion_tokens" in payload)
            or payload.get("stream_options", {"include_usage": True}) != {"include_usage": True}
        ):
            return False
        maximum = payload.get("max_tokens", payload.get("max_completion_tokens"))
    else:
        return False
    return (
        set(payload) <= keys and type(maximum) is int
        and 1 <= maximum <= max_output
        and type(payload.get("stream", False)) is bool
    )


def _build_dispatch_guard(
    repo: SessionRepository, *, sentinel_only: bool, max_output: int,
) -> Callable[[str, str, dict[str, Any]], Awaitable[bool]]:
    async def guard(owner_id: str, deployment: str, payload: dict[str, Any]) -> bool:
        from .sessions.repository import SessionNotFoundError

        slot = _fresh_dispatch.get()
        if (
            slot is None or slot.consumed or slot.owner != owner_id or slot.deployment != deployment
            or not fresh_session_required() or tools_allowed() or automatic_memory_allowed()
        ):
            return False
        # Consume before any await. A denied/ambiguous attempt cannot retry with
        # a different body, and child tasks share this request-local allowance.
        slot.consumed = True
        if not _bounded_payload(slot, payload, sentinel_only=sentinel_only, max_output=max_output):
            return False
        try:
            current = await repo.get_session(owner_id, slot.session_id)
        except SessionNotFoundError:
            return False
        return (
            current.freshTurnClaimed and current.deletionProtocol == 1
            and current.deletionEpoch == slot.epoch
            and current.agentName is None and current.systemPrompt is None
            and current.summary is None and current.summarizedThroughMessageId is None
            and current.libraryDocumentIds == []
            and not current.toolOverrides.added and not current.toolOverrides.removed
        )

    return guard


def build_canary_dispatch_guard(
    repo: SessionRepository,
) -> Callable[[str, str, dict[str, Any]], Awaitable[bool]]:
    return _build_dispatch_guard(repo, sentinel_only=True, max_output=CANARY_MAX_OUTPUT_TOKENS)


def build_evaluation_dispatch_guard(
    repo: SessionRepository,
) -> Callable[[str, str, dict[str, Any]], Awaitable[bool]]:
    return _build_dispatch_guard(repo, sentinel_only=False, max_output=256)


def constrain_tool_parameters(params: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(params or {})
    if not tools_allowed():
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            result.pop(key, None)
    return result


def contains_tool_output(payload: dict[str, Any]) -> bool:
    """Recognize chat and provider-native tool events before adapters can drop them."""
    for choice in payload.get("choices") or []:
        for key in ("message", "delta"):
            part = choice.get(key) or {}
            if part.get("tool_calls") or part.get("function_call"):
                return True
    items = [
        payload.get("item"), payload.get("content_block"),
        *(payload.get("output") or []), *(payload.get("content") or []),
        *((payload.get("response") or {}).get("output") or []),
    ]
    return any(
        isinstance(item, dict) and isinstance(item.get("type"), str)
        and (item["type"].endswith("_call") or item["type"].endswith("tool_use"))
        for item in items
    )
