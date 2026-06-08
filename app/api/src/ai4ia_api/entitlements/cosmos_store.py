"""Cosmos DB (NoSQL) entitlement override store using AAD (managed identity).

Container ``entitlements`` (PK ``/userId``) is created by infra/modules/data.bicep
and shares the api managed identity's Cosmos Data Contributor role (no extra RBAC).
One document per user (``id == userId == partition key``), so ``get``/``delete``
are single-partition point operations and ``put`` is an upsert.

Resilience: a missing container or transient failure must not break the chat
path. ``get`` returns ``None`` (treated as the unlimited default) when the
container or item is absent; the service additionally fails open on any error.
"""
from __future__ import annotations

from .models import Entitlement


class CosmosEntitlementStore:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._container = db.get_container_client("entitlements")

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    async def get(self, user_id: str) -> Entitlement | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            doc = await self._container.read_item(item=user_id, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return None
        return Entitlement.model_validate(doc)

    async def put(self, entitlement: Entitlement) -> None:
        await self._container.upsert_item(entitlement.model_dump(mode="json"))

    async def delete(self, user_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            await self._container.delete_item(item=user_id, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return None

    async def list(self) -> list[Entitlement]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        # Admin-only, cross-partition; the override set is expected to be small
        # (only users with an explicit limit have a document).
        query = "SELECT * FROM c"
        try:
            return [
                Entitlement.model_validate(doc)
                async for doc in self._container.query_items(query=query)
            ]
        except CosmosResourceNotFoundError:
            return []
