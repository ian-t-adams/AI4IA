# pyright: reportArgumentType=false, reportCallIssue=false
# ^ Azure Cosmos SDK typing friction, not real defects: container.query_items's
#   `parameters` is typed list[dict[str, object]], but our list[dict[str, str]]
#   literals are rejected by list/dict invariance, which also makes the query_items
#   overloads fail to resolve. The queries are correct at runtime. Scoped to this
#   Cosmos store module so the rules stay active everywhere else.
"""Cosmos DB (NoSQL) WorkflowStore using AAD (managed identity) auth.

Container ``workflows`` (PK ``/userId``) is created by infra/modules/data.bicep
and shares the api managed identity's account-scoped Cosmos Data Contributor role
(no extra RBAC). Each document has ``id == name`` and ``partition key == userId``,
so ``get``/``delete`` are single-partition point operations and ``put`` is an
upsert. ``get``/``list`` return ``None``/``[]`` when the item/container is absent
so a missing container degrades gracefully; the workflow service surfaces other
store errors (workflow reads are not on the chat hot path).
"""
from __future__ import annotations

from .models import Workflow
from ..publishing.store import CosmosRecordStore, delete_definition, replace_definition
from .record_types import (
    CONTROL_RECORD_PREFIX, DEFINITION_QUERY, WORKFLOW_DEFINITION_KIND, is_definition,
)


class CosmosWorkflowStore:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        db = self._client.get_database_client(database)
        self._container = db.get_container_client("workflows")
        self.records = CosmosRecordStore(self._container)

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()

    async def list(self, user_id: str) -> list[Workflow]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        query = DEFINITION_QUERY
        params = [
            {"name": "@uid", "value": user_id},
            {"name": "@controlPrefix", "value": CONTROL_RECORD_PREFIX},
            {"name": "@definitionKind", "value": WORKFLOW_DEFINITION_KIND},
        ]
        try:
            return [
                Workflow.model_validate(doc)
                async for doc in self._container.query_items(
                    query=query, parameters=params, partition_key=user_id
                )
                if is_definition(doc, user_id=user_id, kind=WORKFLOW_DEFINITION_KIND)
            ]
        except CosmosResourceNotFoundError:
            return []

    async def get(self, user_id: str, name: str) -> Workflow | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            doc = await self._container.read_item(item=name, partition_key=user_id)
        except CosmosResourceNotFoundError:
            return None
        if not is_definition(doc, user_id=user_id, kind=WORKFLOW_DEFINITION_KIND, name=name):
            return None
        workflow = Workflow.model_validate(doc)
        # Defense in depth: the partition already scopes to the user, but never
        # return a record whose denormalized owner doesn't match.
        if workflow.userId != user_id:
            return None
        return workflow

    async def put(self, workflow: Workflow) -> None:
        await self._container.upsert_item(workflow.model_dump(mode="json"))

    async def create_if_absent(self, workflow: Workflow) -> bool:
        return await replace_definition(
            self.records, workflow.model_dump(mode="json"), expected_revision=None, create=True,
        )

    async def replace_if_revision(self, workflow: Workflow, expected_revision: int) -> bool:
        return await replace_definition(
            self.records, workflow.model_dump(mode="json"), expected_revision=expected_revision,
        )

    async def delete(self, user_id: str, name: str) -> None:
        from .models import WorkflowConflictError

        if await self.get(user_id, name) is not None and not await delete_definition(
            self.records, user_id, name,
        ):
            raise WorkflowConflictError("Workflow changed before deletion; refresh and retry.")
