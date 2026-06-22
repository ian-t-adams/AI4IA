# pyright: reportArgumentType=false, reportCallIssue=false
# ^ Azure Cosmos SDK typing friction, not real defects: container.query_items's
#   `parameters` is typed list[dict[str, object]], but our list[dict[str, str]]
#   literals are rejected by list/dict invariance, which also makes the query_items
#   overloads fail to resolve. The queries are correct at runtime. Scoped to this
#   Cosmos repo module so the rules stay active everywhere else.
"""Cosmos DB (NoSQL) usage ledger using AAD (managed identity) auth.

Container ``usage`` (PK ``/userId``) is created by infra/modules/data.bicep. The
api managed identity already holds the Cosmos Built-in Data Contributor role, so
no extra RBAC is required. Azure SDKs are imported lazily so the app and tests
run without them installed.

Resilience: a missing container or a transient write failure must not break the
chat path. ``record`` lets exceptions propagate to the service, which shields and
logs them; ``summarize`` degrades to an empty summary if the container is absent.
"""
from __future__ import annotations

from datetime import datetime

from .models import UsageRecord, UsageSummary, summarize_records


class CosmosUsageRepository:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._usage = db.get_container_client("usage")

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    async def record(self, record: UsageRecord) -> None:
        await self._usage.create_item(record.model_dump(mode="json"))

    async def summarize(
        self, user_id: str, *, since: datetime, since_days: int, now: datetime
    ) -> UsageSummary:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        query = (
            "SELECT * FROM c WHERE c.userId = @uid AND c.createdAt >= @since "
            "ORDER BY c.createdAt DESC"
        )
        params = [
            {"name": "@uid", "value": user_id},
            {"name": "@since", "value": since.isoformat()},
        ]
        try:
            records = [
                UsageRecord.model_validate(doc)
                async for doc in self._usage.query_items(query=query, parameters=params)
            ]
        except CosmosResourceNotFoundError:
            records = []
        return summarize_records(
            user_id, records, since_days=since_days, from_time=since, to_time=now
        )

    async def query_records(
        self, *, since: datetime, now: datetime, limit: int
    ) -> list[UsageRecord]:
        """Admin-only cross-partition window scan, capped at ``limit`` rows.

        ``TOP`` bounds the RU a single dashboard request can consume; ordering
        newest-first means the cap drops the oldest rows when a window overflows.
        A missing container degrades to an empty list (best-effort), never a 500.
        """
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        query = (
            f"SELECT TOP {int(limit)} * FROM c "
            "WHERE c.createdAt >= @since AND c.createdAt <= @now "
            "ORDER BY c.createdAt DESC"
        )
        params = [
            {"name": "@since", "value": since.isoformat()},
            {"name": "@now", "value": now.isoformat()},
        ]
        try:
            return [
                UsageRecord.model_validate(doc)
                async for doc in self._usage.query_items(query=query, parameters=params)
            ]
        except CosmosResourceNotFoundError:
            return []
