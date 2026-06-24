"""UserDirectoryRepository protocol.

The directory is partitioned by the hashed internal ``userId`` with a single
profile document per partition, so ``upsert`` and each id in ``resolve`` are
single-partition point operations. Both are best-effort by contract: an
implementation degrades to a no-op write / empty resolve rather than raising into
a chat turn or an admin read.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from .model import UserDirectoryEntry


@runtime_checkable
class UserDirectoryRepository(Protocol):
    async def upsert(self, entry: UserDirectoryEntry) -> None: ...

    async def resolve(
        self, user_ids: Iterable[str]
    ) -> dict[str, UserDirectoryEntry]: ...

    async def close(self) -> None: ...
