# pyright: reportArgumentType=false, reportCallIssue=false
# ^ Azure Cosmos SDK typing friction, not real defects (see usage/cosmos_repo.py).
"""Cosmos DB (NoSQL) user directory using AAD (managed identity) auth.

Container ``userDirectory`` (PK ``/userId``) is created by infra/modules/data.bicep
and shares the api managed identity's Cosmos Data Contributor role (no extra RBAC).
One profile document per user (``id == "profile"``, partition == ``userId``), so
``upsert`` and each id in ``resolve`` are single-partition point operations.

Resilience: a missing container or a transient failure must NEVER break a chat
turn or an admin read. ``upsert`` lets exceptions propagate to the directory
service (which schedules it fire-and-forget and swallows them); ``resolve`` skips
any id that errors and returns ``{}`` when the container is absent.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable

from .model import PROFILE_ID, UserDirectoryEntry

# Hard cap on ids a single resolve may point-read, bounding the per-request RU /
# fan-out even if a caller passes an unusually large id set.
MAX_RESOLVE_IDS = 500


class CosmosUserDirectoryRepository:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._container = db.get_container_client("userDirectory")

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    async def upsert(self, entry: UserDirectoryEntry) -> None:
        await self._container.upsert_item(entry.model_dump(mode="json"))

    async def _read_one(self, user_id: str) -> UserDirectoryEntry | None:
        from azure.cosmos.exceptions import CosmosHttpResponseError

        try:
            doc = await self._container.read_item(
                item=PROFILE_ID, partition_key=user_id
            )
        except CosmosHttpResponseError:
            # Missing item/container or any transient read error -> skip this id.
            return None
        return UserDirectoryEntry.model_validate(doc)

    async def resolve(
        self, user_ids: Iterable[str]
    ) -> dict[str, UserDirectoryEntry]:
        """Point-read the profile doc for each (deduped, capped) id concurrently.

        Bounded to :data:`MAX_RESOLVE_IDS` ids; a missing item/container or any
        per-id error is skipped so a partial directory never fails an admin read.
        """
        ids = list(dict.fromkeys(uid for uid in user_ids if uid))[:MAX_RESOLVE_IDS]
        if not ids:
            return {}
        results = await asyncio.gather(
            *(self._read_one(uid) for uid in ids), return_exceptions=True
        )
        out: dict[str, UserDirectoryEntry] = {}
        for uid, res in zip(ids, results):
            if isinstance(res, UserDirectoryEntry):
                out[uid] = res
        return out
