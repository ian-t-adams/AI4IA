"""Task-local actor binding; status/cleanup/accounting never require a new grant."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..auth.base import AuthenticatedUser
from ..catalog import DeploymentOption
from .models import EffectivePolicy, PolicyDecision, PolicyError, PolicyRequest

if TYPE_CHECKING:
    from .service import PolicyService


@dataclass(frozen=True)
class PolicyBinding:
    service: PolicyService
    owner_id: str
    user: AuthenticatedUser | None = None

    async def resolve(self) -> EffectivePolicy:
        if self.user is not None:
            return await self.service.resolve(self.user, expected_owner=self.owner_id)
        return await self.service.resolve_unattended(self.owner_id)


_current: ContextVar[PolicyBinding | None] = ContextVar("application_policy_actor", default=None)
_source_check: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "publication_execution_check", default=None,
)
_tool: ContextVar[str | None] = ContextVar("policy_tool", default=None)


def clear_policy_context() -> None:
    _current.set(None)
    _source_check.set(None)
    _tool.set(None)


def bind_authenticated(service: PolicyService, user: AuthenticatedUser) -> None:
    _current.set(PolicyBinding(service, user.internal_user_id, user.model_copy(deep=True)))


@contextmanager
def unattended_policy_scope(service: PolicyService, owner_id: str) -> Iterator[None]:
    token = _current.set(PolicyBinding(service, owner_id))
    source = _source_check.set(None)
    try:
        yield
    finally:
        _source_check.reset(source)
        _current.reset(token)


@contextmanager
def publication_scope(check: Callable[[], Awaitable[None]]) -> Iterator[None]:
    previous = _source_check.get()

    async def combined() -> None:
        if previous is not None:
            await previous()
        await check()

    token = _source_check.set(combined)
    try:
        yield
    finally:
        _source_check.reset(token)


def current_binding() -> PolicyBinding | None:
    return _current.get()


@contextmanager
def tool_policy_scope(name: str) -> Iterator[None]:
    token = _tool.set(name)
    try:
        yield
    finally:
        _tool.reset(token)


def current_tool() -> str | None:
    return _tool.get()


def model_allowed(category: str, deployment: DeploymentOption) -> bool:
    binding = _current.get()
    return binding is None or binding.service.allows_model_snapshot(
        binding.user, category, deployment,
    )


def tool_allowed(name: str) -> bool:
    binding = _current.get()
    return binding is None or binding.service.allows_tool_snapshot(binding.user, name)


async def require_policy(request: PolicyRequest, *, owner_id: str | None = None) -> None:
    binding = _current.get()
    if binding is None:
        return
    if owner_id is not None and binding.owner_id != owner_id:
        raise PolicyError(PolicyDecision("deny", "owner_mismatch"))
    if binding.service.enabled:
        await binding.service.require(await binding.resolve(), request)
    check = _source_check.get()
    if check is not None:
        await check()


async def require_bound_policy(service: PolicyService, request: PolicyRequest) -> None:
    binding = _current.get()
    if service.enabled and (binding is None or binding.service is not service):
        raise PolicyError(PolicyDecision("unavailable", "reauthentication_required"))
    await require_policy(request)


def canonical_tool_name(name: str, aliases: Mapping[str, str]) -> str:
    return next((durable for durable, alias in aliases.items() if alias == name), name)
