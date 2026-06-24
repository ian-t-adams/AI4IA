"""Unit tests for the admin user directory: model, memory repo, and service.

Covers the model/repo round-trip, the capture dedupe cache (writes at most once
per window, skips when there's no name AND no email, swallows store errors), and
the best-effort resolve (degrades to ``{}`` when disabled or on a store error).
"""
from __future__ import annotations

from collections.abc import Iterable

from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.directory.memory_repo import InMemoryUserDirectoryRepository
from ai4ia_api.directory.model import PROFILE_ID, UserDirectoryEntry
from ai4ia_api.directory.service import UserDirectoryService


def _user(
    uid: str = "hash-1",
    name: str | None = "Ada Lovelace",
    email: str | None = "ada@example.com",
) -> AuthenticatedUser:
    return AuthenticatedUser(
        internal_user_id=uid,
        subject="sub",
        issuer="iss",
        tenant_id="tid",
        provider="dev",
        name=name,
        email=email,
    )


class _BoomRepo:
    """Repo whose every operation raises, to prove best-effort behaviour."""

    async def upsert(self, entry: UserDirectoryEntry) -> None:  # noqa: ARG002
        raise RuntimeError("cosmos down")

    async def resolve(
        self, user_ids: Iterable[str]
    ) -> dict[str, UserDirectoryEntry]:  # noqa: ARG002
        raise RuntimeError("cosmos down")

    async def close(self) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ---- model + memory repo ----


def test_entry_build_sets_constant_id_and_fields():
    entry = UserDirectoryEntry.build("hash-9", "Grace Hopper", "grace@navy.mil")
    assert entry.id == PROFILE_ID
    assert entry.userId == "hash-9"
    assert entry.displayName == "Grace Hopper"
    assert entry.email == "grace@navy.mil"
    assert entry.updatedAt is not None


async def test_memory_repo_upsert_then_resolve_round_trips():
    repo = InMemoryUserDirectoryRepository()
    await repo.upsert(UserDirectoryEntry.build("u1", "One", "one@x.test"))
    await repo.upsert(UserDirectoryEntry.build("u2", "Two", None))
    out = await repo.resolve(["u1", "u2"])
    assert out["u1"].displayName == "One"
    assert out["u2"].email is None


async def test_memory_repo_resolve_only_returns_requested_and_known():
    repo = InMemoryUserDirectoryRepository()
    await repo.upsert(UserDirectoryEntry.build("u1", "One", None))
    # Unknown id is omitted; un-requested known id is not leaked.
    await repo.upsert(UserDirectoryEntry.build("u2", "Two", None))
    out = await repo.resolve(["u1", "missing"])
    assert set(out) == {"u1"}


# ---- capture: dedupe / skip / best-effort ----


async def test_capture_writes_once_per_window():
    repo = InMemoryUserDirectoryRepository()
    svc = UserDirectoryService(repo, enabled=True)
    task = svc.capture(_user("u1"))
    assert task is not None
    await task
    # Second capture inside the window is deduped -> no task, no extra write.
    assert svc.capture(_user("u1")) is None
    out = await repo.resolve(["u1"])
    assert out["u1"].displayName == "Ada Lovelace"
    assert out["u1"].email == "ada@example.com"


async def test_capture_writes_again_after_ttl_expiry():
    repo = InMemoryUserDirectoryRepository()
    clock = _Clock()
    svc = UserDirectoryService(
        repo, enabled=True, dedupe_ttl_seconds=900, clock=clock
    )
    first = svc.capture(_user("u1"))
    assert first is not None
    await first
    assert svc.capture(_user("u1")) is None  # still within window
    clock.t += 901  # advance past the TTL
    again = svc.capture(_user("u1"))
    assert again is not None
    await again


async def test_capture_skips_when_disabled():
    repo = InMemoryUserDirectoryRepository()
    svc = UserDirectoryService(repo, enabled=False)
    assert svc.capture(_user("u1")) is None
    assert await repo.resolve(["u1"]) == {}


async def test_capture_skips_when_no_name_and_no_email():
    repo = InMemoryUserDirectoryRepository()
    svc = UserDirectoryService(repo, enabled=True)
    assert svc.capture(_user("u1", name=None, email=None)) is None
    assert await repo.resolve(["u1"]) == {}


async def test_capture_writes_when_only_email_present():
    repo = InMemoryUserDirectoryRepository()
    svc = UserDirectoryService(repo, enabled=True)
    task = svc.capture(_user("u1", name=None, email="only@x.test"))
    assert task is not None
    await task
    out = await repo.resolve(["u1"])
    assert out["u1"].email == "only@x.test"


async def test_capture_swallows_store_errors():
    svc = UserDirectoryService(_BoomRepo(), enabled=True)
    task = svc.capture(_user("u1"))
    assert task is not None
    # The fire-and-forget write fails internally but must not raise to the caller.
    await task


async def test_capture_evicts_when_over_capacity():
    repo = InMemoryUserDirectoryRepository()
    svc = UserDirectoryService(repo, enabled=True, dedupe_max_entries=2)
    for uid in ("a", "b", "c"):
        t = svc.capture(_user(uid))
        if t is not None:
            await t
    # Cache is bounded; the oldest id ("a") was evicted, so it can capture again.
    assert svc.capture(_user("a")) is not None


# ---- resolve: bounded, best-effort ----


async def test_resolve_returns_known_entries():
    repo = InMemoryUserDirectoryRepository()
    await repo.upsert(UserDirectoryEntry.build("u1", "One", "one@x.test"))
    svc = UserDirectoryService(repo, enabled=True)
    out = await svc.resolve(["u1", "u2"])
    assert out["u1"].displayName == "One"
    assert "u2" not in out


async def test_resolve_empty_when_disabled():
    repo = InMemoryUserDirectoryRepository()
    await repo.upsert(UserDirectoryEntry.build("u1", "One", None))
    svc = UserDirectoryService(repo, enabled=False)
    assert await svc.resolve(["u1"]) == {}


async def test_resolve_skips_blank_ids():
    repo = InMemoryUserDirectoryRepository()
    svc = UserDirectoryService(repo, enabled=True)
    assert await svc.resolve(["", None]) == {}  # type: ignore[list-item]


async def test_resolve_degrades_to_empty_on_store_error():
    svc = UserDirectoryService(_BoomRepo(), enabled=True)
    assert await svc.resolve(["u1"]) == {}
