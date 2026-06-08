"""Memory data model (Phase 5).

A :class:`MemoryRecord` is one durable, per-user snippet of recallable context.
Records are always scoped to an ``internal_user_id`` so isolation is enforced by
the store layer (every store method requires a user id — see ``base.py``).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MemoryRecord:
    """A single recallable memory belonging to exactly one user.

    ``kind`` distinguishes the source (currently only ``"user_message"``; a future
    increment adds extracted ``"fact"``/``"summary"`` records). ``score`` is the
    similarity of a search hit and is unset on stored records.
    """

    user_id: str
    text: str
    session_id: str | None = None
    kind: str = "user_message"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_now)
    score: float | None = None
