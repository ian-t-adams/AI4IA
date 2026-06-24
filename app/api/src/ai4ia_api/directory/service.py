"""UserDirectoryService: best-effort capture (deduped) + admin resolve.

Capture is invoked on the auth dependency that yields the current user, so it runs
on *every* authenticated request. To keep that to at most one extra Cosmos write
per user per window, an in-process TTL + LRU dedupe cache short-circuits repeat
captures (default ~15 min). The write itself is fire-and-forget (scheduled, never
awaited in the request path) and fully swallows store errors, so a directory
failure can never break a chat turn.

Resolve is the admin read side: a bounded, best-effort batch lookup of the ids
already present in an admin response. Any failure degrades to an empty mapping so
the dashboard simply falls back to the short hash.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

from ..auth.base import AuthenticatedUser
from .model import UserDirectoryEntry
from .repository import UserDirectoryRepository

logger = logging.getLogger(__name__)

# At most one capture write per user per this window (seconds).
DEFAULT_DEDUPE_TTL_SECONDS = 15 * 60
# Bound the dedupe cache so it can't grow without limit on a long-lived process.
DEFAULT_DEDUPE_MAX_ENTRIES = 10_000


class UserDirectoryService:
    def __init__(
        self,
        repo: UserDirectoryRepository,
        *,
        enabled: bool = True,
        dedupe_ttl_seconds: int = DEFAULT_DEDUPE_TTL_SECONDS,
        dedupe_max_entries: int = DEFAULT_DEDUPE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = repo
        self._enabled = enabled
        self._ttl = max(0, dedupe_ttl_seconds)
        self._max_entries = max(1, dedupe_max_entries)
        self._clock = clock
        # userId -> monotonic expiry. Insertion-ordered for LRU eviction.
        self._seen: dict[str, float] = {}
        # Keep references to in-flight fire-and-forget writes so they aren't GC'd
        # mid-flight; tasks discard themselves on completion.
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---- capture (write side; deduped, best-effort, non-blocking) ----

    def _should_capture(self, user_id: str) -> bool:
        """Dedupe gate: True at most once per ``user_id`` per TTL window.

        Records the new expiry and moves the id to the most-recently-used end,
        evicting the oldest entries when over capacity. Pure aside from mutating
        the in-process cache, so it is directly unit-testable with a fake clock.
        """
        now = self._clock()
        existing = self._seen.get(user_id)
        if existing is not None and existing > now:
            return False
        # (Re)insert at the MRU end so eviction drops genuinely-oldest ids.
        self._seen.pop(user_id, None)
        self._seen[user_id] = now + self._ttl
        self._evict(now)
        return True

    def _evict(self, now: float) -> None:
        if len(self._seen) <= self._max_entries:
            return
        # Drop expired ids first, then oldest-inserted, until within capacity.
        for uid in list(self._seen):
            if len(self._seen) <= self._max_entries:
                break
            if self._seen[uid] <= now:
                del self._seen[uid]
        while len(self._seen) > self._max_entries:
            oldest = next(iter(self._seen))
            del self._seen[oldest]

    def capture(self, user: AuthenticatedUser) -> "asyncio.Task | None":
        """Schedule a best-effort directory upsert for ``user`` (or skip).

        Returns the scheduled task (handy for tests to await), or ``None`` when
        the capture was skipped: feature disabled, no name AND no email to store,
        deduped within the window, or no running event loop. Never raises."""
        if not self._enabled:
            return None
        user_id = user.internal_user_id
        name = user.name
        email = user.email
        # Nothing readable to store -> don't create an empty row.
        if not user_id or (not name and not email):
            return None
        if not self._should_capture(user_id):
            return None
        entry = UserDirectoryEntry.build(user_id, name, email)
        try:
            task = asyncio.ensure_future(self._safe_upsert(entry))
        except RuntimeError:
            # No running loop (shouldn't happen in the async request path).
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _safe_upsert(self, entry: UserDirectoryEntry) -> None:
        try:
            await self._repo.upsert(entry)
        except Exception:  # noqa: BLE001 - capture is best-effort; never propagate
            logger.warning(
                "user directory upsert failed (user=%s)", entry.userId, exc_info=True
            )

    # ---- resolve (admin read side; bounded, best-effort) ----

    async def resolve(
        self, user_ids: Iterable[str]
    ) -> dict[str, UserDirectoryEntry]:
        """Batch-resolve the given ids to directory entries (best-effort).

        Returns ``{}`` when disabled or on any store error so an admin read always
        degrades cleanly to the short hash rather than failing."""
        if not self._enabled:
            return {}
        ids = [uid for uid in user_ids if uid]
        if not ids:
            return {}
        try:
            return await self._repo.resolve(ids)
        except Exception:  # noqa: BLE001 - the enrichment join is advisory
            logger.warning("user directory resolve failed", exc_info=True)
            return {}

    async def close(self) -> None:
        close = getattr(self._repo, "close", None)
        if close is not None:
            await close()
