"""Conditional owner-partition records shared by drafts and publication evidence."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..workflows.record_types import CONTROL_RECORD_PREFIX, RECORD_KIND_FIELD


@dataclass(frozen=True)
class RecordSnapshot:
    body: dict[str, Any]
    etag: str


@dataclass(frozen=True)
class RecordQuery:
    kind: str
    owner_id: str | None = None
    tenant_id: str | None = None
    handle: str | None = None
    viewer_id: str | None = None
    email: str | None = None
    groups: tuple[str, ...] = ()
    active_only: bool = False
    review_for: str | None = None
    operator_only: bool = False
    limit: int = 101


class RecordStore(Protocol):
    async def read(self, owner: str, identifier: str) -> RecordSnapshot | None: ...
    async def query(self, query: RecordQuery) -> list[RecordSnapshot]: ...
    async def atomic(
        self, owner: str, expected: Mapping[str, RecordSnapshot | None],
        changes: Mapping[str, dict[str, Any] | None],
    ) -> bool: ...
    async def put(self, owner: str, body: dict[str, Any]) -> None: ...


def _validate_batch(
    owner: str, expected: Mapping[str, RecordSnapshot | None],
    changes: Mapping[str, dict[str, Any] | None],
) -> None:
    if not owner or not 1 <= len(expected) <= 100 or changes.keys() - expected.keys():
        raise ValueError("Invalid owner-partition batch.")
    for identifier, snapshot in expected.items():
        if snapshot is not None and (
            not snapshot.etag or snapshot.body.get("id") != identifier
            or snapshot.body.get("userId") != owner
        ):
            raise ValueError("Invalid conditional record ownership.")
        body = changes.get(identifier)
        if body is not None and (body.get("id") != identifier or body.get("userId") != owner):
            raise ValueError("Invalid replacement ownership.")
        if snapshot is None and body is None:
            raise ValueError("An absent record precondition requires a create.")


class InMemoryRecordStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], RecordSnapshot] = {}
        self._sequence = 0

    async def read(self, owner: str, identifier: str) -> RecordSnapshot | None:
        return copy.deepcopy(self.items.get((owner, identifier)))

    async def query(self, query: RecordQuery) -> list[RecordSnapshot]:
        if not 1 <= query.limit <= 1001:
            raise ValueError("Invalid record query bound.")
        rows = [
            value for value in self.items.values()
            if value.body.get(RECORD_KIND_FIELD) == query.kind
            and (query.owner_id is None or value.body.get("userId") == query.owner_id)
            and (query.tenant_id is None or value.body.get("tenantId") == query.tenant_id)
            and (query.handle is None or value.body.get("handle") == query.handle)
            and (not query.active_only or value.body.get("activeVersion") is not None)
            and (
                query.viewer_id is None
                or value.body.get("userId") == query.viewer_id
                or value.body.get("visibility") == "public"
                or (
                    value.body.get("visibility") == "shared"
                    and (
                        (query.email is not None and query.email in value.body.get("acl", []))
                        or bool(set(query.groups) & set(value.body.get("groupAcl", [])))
                    )
                )
            )
            and (
                query.review_for is None or (
                    value.body.get("pendingVersion") is not None
                    and value.body.get("userId") != query.review_for
                    and value.body.get("reviewConsent") is True
                    and value.body.get("reviewerUserId") in {None, query.review_for}
                    and (not query.operator_only or value.body.get("operatorReviewConsent") is True)
                )
            )
        ]
        rows.sort(key=lambda row: (row.body["userId"], row.body["id"]))
        return copy.deepcopy(rows[:query.limit])

    async def atomic(
        self, owner: str, expected: Mapping[str, RecordSnapshot | None],
        changes: Mapping[str, dict[str, Any] | None],
    ) -> bool:
        _validate_batch(owner, expected, changes)
        for identifier, before in expected.items():
            current = self.items.get((owner, identifier))
            if (before is None) != (current is None) or (
                before is not None and current is not None and before.etag != current.etag
            ):
                return False
        # No await between validation and writes: one atomic event-loop operation.
        for identifier, before in expected.items():
            body = changes.get(identifier, before.body if before is not None else None)
            if body is None:
                self.items.pop((owner, identifier), None)
            else:
                self._sequence += 1
                self.items[(owner, identifier)] = RecordSnapshot(
                    copy.deepcopy(body), str(self._sequence),
                )
        return True

    async def put(self, owner: str, body: dict[str, Any]) -> None:
        if body.get("userId") != owner or not isinstance(body.get("id"), str):
            raise ValueError("Invalid record owner.")
        self._sequence += 1
        self.items[(owner, body["id"])] = RecordSnapshot(copy.deepcopy(body), str(self._sequence))

    def definitions(self, owner: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(row.body) for (uid, identifier), row in self.items.items()
            if uid == owner and not identifier.startswith(CONTROL_RECORD_PREFIX)
        ]


class CosmosRecordStore:
    def __init__(self, container: Any) -> None:
        self.container = container

    @staticmethod
    def snapshot(body: dict[str, Any]) -> RecordSnapshot:
        etag = body.get("_etag")
        if not isinstance(etag, str) or not etag:
            raise ValueError("Conditional record has no ETag.")
        return RecordSnapshot({
            name: value for name, value in body.items()
            if name not in {"_etag", "_rid", "_self", "_attachments", "_ts"}
        }, etag)

    async def read(self, owner: str, identifier: str) -> RecordSnapshot | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            body = await self.container.read_item(item=identifier, partition_key=owner)
        except CosmosResourceNotFoundError:
            # Publication storage must distinguish an absent item from an absent
            # container, unlike legacy best-effort catalog reads.
            await self.container.read()
            return None
        snapshot = self.snapshot(body)
        if body.get("userId") != owner or body.get("id") != identifier:
            raise ValueError("Conditional record owner mismatch.")
        return snapshot

    async def query(self, query: RecordQuery) -> list[RecordSnapshot]:
        if not 1 <= query.limit <= 1001:
            raise ValueError("Invalid record query bound.")
        filters = ["c.recordKind = @kind"]
        parameters: list[dict[str, Any]] = [
            {"name": "@kind", "value": query.kind}, {"name": "@limit", "value": query.limit},
        ]
        for field, value in (
            ("userId", query.owner_id), ("tenantId", query.tenant_id), ("handle", query.handle),
        ):
            if value is not None:
                filters.append(f"c.{field} = @{field}")
                parameters.append({"name": f"@{field}", "value": value})
        if query.active_only:
            filters.append("IS_NUMBER(c.activeVersion)")
        if query.viewer_id is not None:
            filters.append(
                "(c.userId = @viewer OR c.visibility = 'public' OR "
                "(c.visibility = 'shared' AND (ARRAY_CONTAINS(c.acl, @email) OR "
                "EXISTS(SELECT VALUE g FROM g IN c.groupAcl WHERE ARRAY_CONTAINS(@groups, g)))))"
            )
            parameters.extend([
                {"name": "@viewer", "value": query.viewer_id},
                {"name": "@email", "value": query.email or ""},
                {"name": "@groups", "value": list(query.groups)},
            ])
        if query.review_for is not None:
            filters.extend([
                "IS_NUMBER(c.pendingVersion)", "c.userId != @reviewer", "c.reviewConsent = true",
                "(NOT IS_DEFINED(c.reviewerUserId) OR IS_NULL(c.reviewerUserId) OR c.reviewerUserId = @reviewer)",
            ])
            parameters.append({"name": "@reviewer", "value": query.review_for})
            if query.operator_only:
                filters.append("c.operatorReviewConsent = true")
        sql = "SELECT TOP @limit * FROM c WHERE " + " AND ".join(filters)
        return [
            self.snapshot(body) async for body in self.container.query_items(
                query=sql, parameters=parameters, partition_key=query.owner_id,
            )
        ]

    async def atomic(
        self, owner: str, expected: Mapping[str, RecordSnapshot | None],
        changes: Mapping[str, dict[str, Any] | None],
    ) -> bool:
        from azure.cosmos.exceptions import CosmosBatchOperationError

        _validate_batch(owner, expected, changes)
        operations = []
        for identifier, before in expected.items():
            body = changes.get(identifier, before.body if before is not None else None)
            if before is None:
                operations.append(("create", (copy.deepcopy(body),)))
            elif body is None:
                operations.append(("delete", (identifier,), {"if_match_etag": before.etag}))
            else:
                operations.append((
                    "replace", (identifier, copy.deepcopy(body)), {"if_match_etag": before.etag},
                ))
        try:
            await self.container.execute_item_batch(
                batch_operations=operations, partition_key=owner,
            )
        except CosmosBatchOperationError as exc:
            responses = getattr(exc, "operation_responses", None) or []
            if getattr(exc, "status_code", None) in {404, 409, 412} or any(
                isinstance(item, dict) and item.get("statusCode") in {404, 409, 412}
                for item in responses
            ):
                return False
            raise
        return True

    async def put(self, owner: str, body: dict[str, Any]) -> None:
        if body.get("userId") != owner:
            raise ValueError("Invalid record owner.")
        await self.container.upsert_item(copy.deepcopy(body))


async def replace_definition(
    records: RecordStore, body: dict[str, Any], *, expected_revision: int | None,
    create: bool = False,
) -> bool:
    owner, identifier = body["userId"], body["id"]
    before = await records.read(owner, identifier)
    if create:
        if before is not None:
            return False
    elif (
        expected_revision is None
        or before is None
        or before.body.get("revision", 0) != expected_revision
        or body.get("revision") != expected_revision + 1
    ):
        return False
    return await records.atomic(owner, {identifier: before}, {identifier: body})


def publication_head_id(name: str) -> str:
    return f"{CONTROL_RECORD_PREFIX}publication:head:{name}"


async def delete_definition(records: RecordStore, owner: str, name: str) -> bool:
    before = await records.read(owner, name)
    if before is None:
        return True
    head_id = publication_head_id(name)
    head = await records.read(owner, head_id)
    expected = {name: before}
    changes: dict[str, dict[str, Any] | None] = {name: None}
    if head is not None:
        expected[head_id] = head
        changes[head_id] = {
            **head.body, "activeVersion": None, "pendingVersion": None,
            "deleted": True, "revision": head.body["revision"] + 1,
        }
    return await records.atomic(owner, expected, changes)
