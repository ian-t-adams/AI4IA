# pyright: reportArgumentType=false, reportCallIssue=false
# ^ Azure Cosmos SDK typing friction, not real defects: container.query_items's
#   `parameters` is typed list[dict[str, object]], but our list[dict[str, str]]
#   literals are rejected by list/dict invariance, which also makes the query_items
#   overloads fail to resolve. The queries are correct at runtime. Scoped to this
#   Cosmos store module so the rules stay active everywhere else.
"""UserMcpServerStore protocol + in-memory / Cosmos implementations + factory.

Mirrors the user-agent store (``agents/store.py`` + ``agents/cosmos_store.py``):
records are keyed by ``(userId, name)``, reads are single-partition point
lookups on ``userId``, and a missing item/container returns ``None``/``[]``.
The Cosmos container is ``mcpServers`` (PK ``/userId``), provisioned by infra
alongside the existing ``agents``/``workflows`` containers.

Unlike user agents, MCP-server *reads* are **not** on the chat hot path in this
sub-phase (they back a management API and, later, per-turn tool injection), so
the store surfaces errors to the caller rather than failing open.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from ..config import Environment, Settings, SessionStoreKind
from . import mcp_health
from .mcp_servers import UserMcpServer

logger = logging.getLogger(__name__)

_CONTAINER = "mcpServers"


@runtime_checkable
class UserMcpServerStore(Protocol):
    async def list(self, user_id: str) -> list[UserMcpServer]: ...

    async def get(self, user_id: str, name: str) -> UserMcpServer | None: ...

    async def put(self, server: UserMcpServer) -> None: ...

    async def update_health(
        self, user_id: str, name: str, *, ok: bool, error: object | None
    ) -> tuple[UserMcpServer | None, bool, bool]: ...

    async def delete(self, user_id: str, name: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryUserMcpServerStore:
    """Non-durable store for local/dev/tests."""

    def __init__(self) -> None:
        self._by_user: dict[str, dict[str, UserMcpServer]] = {}

    async def list(self, user_id: str) -> list[UserMcpServer]:
        return list(self._by_user.get(user_id, {}).values())

    async def get(self, user_id: str, name: str) -> UserMcpServer | None:
        return self._by_user.get(user_id, {}).get(name)

    async def put(self, server: UserMcpServer) -> None:
        self._by_user.setdefault(server.userId, {})[server.name] = server

    async def update_health(
        self, user_id: str, name: str, *, ok: bool, error: object | None
    ) -> tuple[UserMcpServer | None, bool, bool]:
        current = self._by_user.get(user_id, {}).get(name)
        if current is None:
            return None, False, False
        updated = current.model_copy(deep=True)
        was_quarantined = mcp_health.is_quarantined(updated)
        changed = (
            mcp_health.record_success(updated)
            if ok
            else mcp_health.record_failure(updated, error)
        )
        became_quarantined = (
            not was_quarantined and mcp_health.is_quarantined(updated)
        )
        if changed:
            self._by_user.setdefault(user_id, {})[name] = updated
        return updated, changed, became_quarantined

    async def delete(self, user_id: str, name: str) -> None:
        self._by_user.get(user_id, {}).pop(name, None)

    async def close(self) -> None:
        return None


class CosmosUserMcpServerStore:
    """Cosmos DB (NoSQL) store using AAD (managed identity) auth.

    Each document has ``id == name`` and ``partition key == userId``, so
    ``get``/``delete`` are single-partition point operations and ``put`` is an
    upsert. A missing container/item returns ``None``/``[]``.
    """

    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._container = db.get_container_client(_CONTAINER)

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    async def list(self, user_id: str) -> list[UserMcpServer]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        query = "SELECT * FROM c WHERE c.userId = @uid"
        params = [{"name": "@uid", "value": user_id}]
        try:
            return [
                UserMcpServer.model_validate(doc)
                async for doc in self._container.query_items(
                    query=query, parameters=params, partition_key=user_id
                )
            ]
        except CosmosResourceNotFoundError:
            return []

    async def get(self, user_id: str, name: str) -> UserMcpServer | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            doc = await self._container.read_item(item=name, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return None
        server = UserMcpServer.model_validate(doc)
        # Defense in depth: never return a record whose denormalized owner differs.
        if server.userId != user_id:
            return None
        return server

    async def put(self, server: UserMcpServer) -> None:
        await self._container.upsert_item(server.model_dump(mode="json"))

    async def update_health(
        self, user_id: str, name: str, *, ok: bool, error: object | None
    ) -> tuple[UserMcpServer | None, bool, bool]:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import (
            CosmosAccessConditionFailedError,
            CosmosResourceNotFoundError,
        )

        for _ in range(4):
            try:
                raw = await self._container.read_item(item=name, partition_key=user_id)
            except CosmosResourceNotFoundError:
                return None, False, False
            current = UserMcpServer.model_validate(raw)
            if current.userId != user_id:
                return None, False, False
            updated = current.model_copy(deep=True)
            was_quarantined = mcp_health.is_quarantined(updated)
            changed = (
                mcp_health.record_success(updated)
                if ok
                else mcp_health.record_failure(updated, error)
            )
            became_quarantined = (
                not was_quarantined and mcp_health.is_quarantined(updated)
            )
            if not changed:
                return updated, False, became_quarantined
            health = updated.model_dump(
                mode="json",
                include={
                    "consecutiveFailures",
                    "quarantinedUntil",
                    "lastHealthCheck",
                    "lastHealthError",
                },
            )
            operations = [
                {"op": "set", "path": f"/{field}", "value": value}
                for field, value in health.items()
            ]
            try:
                await self._container.patch_item(
                    item=name,
                    partition_key=user_id,
                    patch_operations=operations,
                    etag=raw.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
                return updated, True, became_quarantined
            except CosmosAccessConditionFailedError:
                continue
            except CosmosResourceNotFoundError:
                return None, False, False
        raise RuntimeError("MCP health update conflicted repeatedly.")

    async def delete(self, user_id: str, name: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            await self._container.delete_item(item=name, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return None


def build_user_mcp_server_store(settings: Settings) -> UserMcpServerStore:
    """Pick the durable Cosmos store when configured, else in-memory.

    Mirrors ``build_user_agent_store``: the kind follows ``session_store`` so it
    never disagrees with the rest of the app about durability, and a loud
    warning is emitted when the in-memory store is selected outside ``local``.
    """
    if settings.session_store == SessionStoreKind.cosmos and settings.cosmos_endpoint:
        return CosmosUserMcpServerStore(settings.cosmos_endpoint, settings.cosmos_database)
    if settings.env != Environment.local:
        logger.warning(
            "mcp servers: using the in-memory store outside local; registered "
            "servers will not survive a restart or replica rollover."
        )
    return InMemoryUserMcpServerStore()
