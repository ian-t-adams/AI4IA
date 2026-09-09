"""Partition/ETag/transaction fake, with no artificial conflict counters."""
from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from ai4ia_api.sessions.cosmos_repo import CosmosSessionRepository
from ai4ia_api.sessions.deletion_models import FENCE_ID


class Response(dict):
    def __init__(self, body, token):
        super().__init__(copy.deepcopy(body))
        self.token = token

    def get_response_headers(self):
        return {"x-ms-session-token": self.token}


class Container:
    def __init__(self, partition: str):
        self.partition = partition
        self.token_id = uuid4().hex
        self.items: dict[tuple[str, str], dict] = {}
        self.version = 0
        self.before_batch: Callable[..., Awaitable[None]] | None = None
        self.before_replace: Callable[..., Awaitable[None]] | None = None
        self.after_create: Callable[..., Awaitable[None]] | None = None
        self.before_query: Callable[..., Awaitable[None]] | None = None
        self.stale_reads: dict[tuple[str, str], dict] = {}
        self.stale_queries = False
        self.queries: list[dict] = []
        self.writes: list[tuple[str, str, str]] = []
        self.properties = {"partitionKey": {"paths": ["/" + partition]}}

    def _put(self, body):
        self.version += 1
        stored = copy.deepcopy(body) | {"_etag": str(self.version), "_lsn": self.version}
        self.items[(body[self.partition], body["id"])] = stored
        return Response(stored, self._token(body[self.partition], self.version))

    def _token(self, partition_key, version):
        return f"{self.token_id}:{partition_key}:{version}"

    async def read(self):
        return copy.deepcopy(self.properties)

    async def read_item(self, *, item, partition_key):
        key = (partition_key, item)
        if key in self.stale_reads:
            raw = self.stale_reads[key]
        else:
            raw = self.items.get(key)
        if raw is None:
            raise CosmosResourceNotFoundError(message="missing")
        return Response(raw, self._token(partition_key, raw["_lsn"]))

    async def create_item(self, body):
        key = (body[self.partition], body["id"])
        if key in self.items:
            raise CosmosResourceExistsError(message="exists")
        saved = self._put(body)
        self.writes.append(("create", *key))
        if self.after_create:
            await self.after_create(body)
        return saved

    async def upsert_item(self, body):
        self.writes.append(("upsert", body[self.partition], body["id"]))
        return self._put(body)

    async def replace_item(self, *, item, body, etag=None, match_condition=None, **kwargs):
        if self.before_replace:
            await self.before_replace(item, body)
        key = (body[self.partition], item)
        current = self.items.get(key)
        if current is None:
            raise CosmosResourceNotFoundError(message="missing")
        if etag is not None and str(current["_etag"]) != etag:
            raise CosmosAccessConditionFailedError(message="etag")
        self.writes.append(("replace", *key))
        return self._put(body)

    @staticmethod
    def _patch(body, operations):
        body = copy.deepcopy(body)
        for operation in operations:
            assert operation["op"] == "set"
            path = operation["path"].lstrip("/").split("/")
            target = body
            for part in path[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            target[path[-1]] = operation["value"]
        return body

    async def patch_item(
        self, *, item, partition_key, patch_operations, etag=None, match_condition=None, **kwargs
    ):
        current = self.items.get((partition_key, item))
        if current is None:
            raise CosmosResourceNotFoundError(message="missing")
        body = self._patch(current, patch_operations)
        return await self.replace_item(
            item=item, body=body, etag=etag, match_condition=match_condition
        )

    async def delete_item(self, *, item, partition_key, **kwargs):
        key = (partition_key, item)
        if key not in self.items:
            raise CosmosResourceNotFoundError(message="missing")
        del self.items[key]
        self.version += 1
        self.writes.append(("delete", *key))

    async def execute_item_batch(self, *, batch_operations, partition_key):
        if self.before_batch:
            await self.before_batch(batch_operations, partition_key)
        staged = copy.deepcopy(self.items)
        version = self.version
        for index, (operation, args, options) in enumerate(batch_operations):
            if operation in {"create", "upsert"}:
                body = args[0]
                item = body["id"]
            else:
                item = args[0]
                body = args[1] if operation == "replace" else None
            key = (partition_key, item)
            current = staged.get(key)
            failure = None
            if operation == "create" and current is not None:
                failure = 409
            elif operation in {"replace", "patch", "delete"} and current is None:
                failure = 404
            elif options.get("if_match_etag") is not None and (
                current is None or current["_etag"] != options["if_match_etag"]
            ):
                failure = 412
            if failure:
                raise CosmosBatchOperationError(
                    error_index=index, status_code=failure, headers={}, message="batch failed",
                    operation_responses=[
                        {"statusCode": failure if position == index else 424}
                        for position in range(len(batch_operations))
                    ],
                )
            if operation == "delete":
                del staged[key]
            else:
                if operation == "patch":
                    body = self._patch(current, args[1])
                assert body[self.partition] == partition_key
                version += 1
                staged[key] = copy.deepcopy(body) | {"_etag": str(version), "_lsn": version}
        self.items = staged
        self.version = version
        self.writes.append(("batch", partition_key, str(len(batch_operations))))
        return Response({}, self._token(partition_key, version))

    async def query_items(
        self, *, query, parameters=None, partition_key=None, session_token=None, **kwargs
    ):
        self.queries.append({
            "query": query, "partition_key": partition_key, "session_token": session_token
        })
        if self.before_query:
            await self.before_query(query, partition_key)
        if self.stale_queries:
            fence = self.items.get((partition_key, FENCE_ID), {})
            prefix = f"{self.token_id}:{partition_key}:"
            if (
                not isinstance(session_token, str)
                or not session_token.startswith(prefix)
                or int(session_token[len(prefix):]) < fence.get("_lsn", self.version)
            ):
                return
        params = {p["name"]: p["value"] for p in parameters or []}
        rows = []
        for (pk, _), raw in self.items.items():
            if partition_key is not None and pk != partition_key:
                continue
            if "@uid" in params and raw.get("userId") != params["@uid"]:
                continue
            if "@sid" in params and raw.get("sessionId") != params["@sid"]:
                continue
            if "@cursor" in params and raw["id"] <= params["@cursor"]:
                continue
            if "@fence" in params and raw["id"] == params["@fence"]:
                continue
            if "c.kind = 'session_tombstone_v1'" in query and raw.get("kind") != "session_tombstone_v1":
                continue
            if "c.kind = 'session_initializing_v1'" in query and raw.get("kind") != "session_initializing_v1":
                continue
            if "c.kind = 'session_upload_v1'" in query and raw.get("kind") != "session_upload_v1":
                continue
            if "c.kind != 'session_upload_v1'" in query and raw.get("kind") == "session_upload_v1":
                continue
            if "OR c.kind = 'session_v1'" in query and raw.get("kind") not in (None, "session_v1"):
                continue
            if "AND NOT IS_DEFINED(c.kind)" in query and "kind" in raw:
                continue
            if "c.settled = false" in query and raw.get("settled") is not False:
                continue
            if "@version" in params and not (
                raw.get("summaryVersion") is not None and raw["summaryVersion"] < params["@version"]
            ):
                continue
            rows.append(copy.deepcopy(raw))
        if "ORDER BY c.id" in query:
            rows.sort(key=lambda row: row["id"])
        match = re.search(r"TOP (\d+)", query)
        if match:
            rows = rows[:int(match[1])]
        for raw in rows:
            yield {"id": raw["id"]} if query.startswith("SELECT c.id ") else raw


class CosmosState:
    def __init__(self):
        self.sessions = Container("userId")
        self.messages = Container("sessionId")
        self.documents = Container("sessionId")

    def repo(self, *, enabled=True):
        repo = object.__new__(CosmosSessionRepository)
        repo._sessions = self.sessions
        repo._messages = self.messages
        repo._documents = self.documents
        repo._deletion_enabled = enabled
        repo._attachment_storage_required = False
        repo._attachment_storage_id = "local"
        return repo

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({
            "sessions": self.sessions.items, "messages": self.messages.items,
            "documents": self.documents.items,
        })
