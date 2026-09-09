"""The atomic primitive is store CAS, never a lock in an API replica."""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Protocol

from .models import QuotaError, QuotaState, Snapshot, state_document


class ReservationStore(Protocol):
    async def read(self, owner: str) -> Snapshot: ...

    async def replace(self, owner: str, snapshot: Snapshot, state: QuotaState) -> bool:
        """Compare exact ETag and replace atomically. False means a lost race.

        Missing state, IO failures and malformed state MUST raise, never return
        an empty balance. No implementation may create/upsert on this path.
        """
        ...


class LocalReservationStore:
    """Explicitly seeded local/test fake. NOT distributed or restart-durable."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._rows: dict[str, tuple[QuotaState, int]] = {}

    def seed(self, owner: str) -> QuotaState:
        """Test/local cutover only; never overwrite even an empty existing row."""
        if owner in self._rows:
            raise QuotaError("Quota state already exists.", code=409)
        now = int(self._clock())
        state = QuotaState(
            userId=owner, epoch=uuid.uuid4().hex,
            validAfter=now, replayFloor=now, observedAt=now,
        )
        self._rows[owner] = (state, 1)
        return state

    async def read(self, owner: str) -> Snapshot:
        row = self._rows.get(owner)
        if row is None:
            raise QuotaError("Hard quota state is absent; reviewed bootstrap is required.")
        state, version = row
        return Snapshot(state.model_copy(deep=True), str(version), int(self._clock()))

    async def replace(self, owner: str, snapshot: Snapshot, state: QuotaState) -> bool:
        state_document(state)
        if owner != state.userId or owner != snapshot.state.userId:
            raise QuotaError("Hard quota owner mismatch.", code=403)
        row = self._rows.get(owner)
        if row is None:
            raise QuotaError("Hard quota state is absent; reviewed bootstrap is required.")
        if str(row[1]) != snapshot.etag:
            return False
        # No await between compare and replacement: this is a test-store atomic
        # operation, not the implementation used for distributed coordination.
        self._rows[owner] = (state.model_copy(deep=True), row[1] + 1)
        return True
