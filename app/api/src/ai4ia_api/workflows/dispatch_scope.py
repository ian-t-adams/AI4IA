"""Task-local automation hooks; ordinary request execution remains unchanged."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from ..usage.models import UsageRecord


class WorkflowExecutionScope(Protocol):
    owner_id: str

    async def before_dispatch(
        self, surface: str, payload: dict[str, Any], *,
        deployment: str | None, target: str | None,
    ) -> str: ...

    async def authorize_dispatch(self, ticket: str) -> None: ...

    async def after_dispatch(
        self, ticket: str, *, usage: dict[str, Any] | None,
        completed: bool, outcome: str,
    ) -> None: ...

    async def capture_usage(self, record: UsageRecord) -> None: ...

    async def before_effect(self, effect: str) -> None: ...


_current: ContextVar[WorkflowExecutionScope | None] = ContextVar("workflow_execution", default=None)


def current_workflow_scope() -> WorkflowExecutionScope | None:
    return _current.get()


@contextmanager
def workflow_execution_scope(scope: WorkflowExecutionScope) -> Iterator[None]:
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


async def require_workflow_effect(effect: str) -> None:
    scope = _current.get()
    if scope is not None:
        await scope.before_effect(effect)
