"""Cosmos DB (NoSQL) UserAgentStore using AAD (managed identity) auth.

Container ``agents`` (PK ``/userId``) is created by infra/modules/data.bicep and
shares the api managed identity's account-scoped Cosmos Data Contributor role (no
extra RBAC). Each document has ``id == name`` and ``partition key == userId``, so
``get``/``delete`` are single-partition point operations and ``put`` is an upsert.

Resilience: a missing container or transient failure must not break the chat
path. ``get`` returns ``None`` when the item/container is absent; ``list`` returns
``[]``. The service additionally fails open to the curated catalog on any error.
"""
from __future__ import annotations

from .user_agents import UserAgent


class CosmosUserAgentStore:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._container = db.get_container_client("agents")

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    async def list(self, user_id: str) -> list[UserAgent]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        query = "SELECT * FROM c WHERE c.userId = @uid"
        params = [{"name": "@uid", "value": user_id}]
        try:
            return [
                UserAgent.model_validate(doc)
                async for doc in self._container.query_items(
                    query=query, parameters=params, partition_key=user_id
                )
            ]
        except CosmosResourceNotFoundError:
            return []

    async def get(self, user_id: str, name: str) -> UserAgent | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            doc = await self._container.read_item(item=name, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return None
        agent = UserAgent.model_validate(doc)
        # Defense in depth: the partition already scopes to the user, but never
        # return a record whose denormalized owner doesn't match.
        if agent.userId != user_id:
            return None
        return agent

    async def put(self, agent: UserAgent) -> None:
        await self._container.upsert_item(agent.model_dump(mode="json"))

    async def delete(self, user_id: str, name: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            await self._container.delete_item(item=name, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return None
