from __future__ import annotations

import copy
import json
import math
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime, timezone
from typing import Any

import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from ai4ia_api.memory.cosmos_service import CosmosMemoryService
from ai4ia_api.memory.cosmos_store import (
    CosmosMemoryStore,
    MemoryConflictError,
    MemoryNotFoundError,
)
from ai4ia_api.memory.models import MemoryRecord
from ai4ia_api.memory.planner import MemoryPlan, MemoryPlanError, MemoryPlanner


class FakeCosmosContainer:
    """Transactional in-memory stand-in for the one-partition store contract."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self._etag = 0
        self.after_query: Callable[[str, list[dict[str, Any]]], None] | None = None

    def _next_etag(self) -> str:
        self._etag += 1
        return f'"etag-{self._etag}"'

    async def read(self) -> dict[str, str]:
        return {"id": "memories"}

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        key = (str(body["userId"]), str(body["id"]))
        if key in self.items:
            raise CosmosResourceExistsError(status_code=409, message="exists")
        stored = {**copy.deepcopy(body), "_etag": self._next_etag()}
        self.items[key] = stored
        return copy.deepcopy(stored)

    async def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self.items[(partition_key, item)])
        except KeyError as exc:
            raise CosmosResourceNotFoundError(
                status_code=404, message="missing"
            ) from exc

    async def replace_item(
        self,
        *,
        item: str,
        body: dict[str, Any],
        etag: str | None = None,
        match_condition: Any | None = None,
    ) -> dict[str, Any]:
        del match_condition
        key = (str(body["userId"]), item)
        current = self.items.get(key)
        if current is None:
            raise CosmosResourceNotFoundError(status_code=404, message="missing")
        if etag is not None and current["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412, message="etag")
        stored = {**copy.deepcopy(body), "_etag": self._next_etag()}
        self.items[key] = stored
        return copy.deepcopy(stored)

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, Any]],
        partition_key: str,
    ) -> AsyncIterator[Any]:
        values = {item["name"]: item["value"] for item in parameters}
        rows = [
            copy.deepcopy(document)
            for (user_id, _item_id), document in self.items.items()
            if user_id == partition_key and document.get("type") == "memory"
        ]
        if "@documentId" in values:
            rows = [
                row for row in rows if row.get("documentId") == values["@documentId"]
            ]
        if "@epoch" in values:
            rows = [
                row
                for row in rows
                if int(row.get("writeEpoch", 0)) < int(values["@epoch"])
            ]
        if "@scopeId" in values:
            field = "sessionId" if "c.sessionId = @scopeId" in query else "documentId"
            rows = [row for row in rows if row.get(field) == values["@scopeId"]]
        if "VectorDistance" in query:
            vector = list(values["@embedding"])
            for row in rows:
                row["similarity"] = _cosine(row["embedding"], vector)
            rows.sort(key=lambda row: row["similarity"], reverse=True)
        else:
            rows.sort(key=lambda row: row.get("updatedAt", ""), reverse=True)
        limit = int(values.get("@limit", len(rows)))
        rows = rows[:limit]
        if "SELECT VALUE COUNT(1)" in query:
            results: list[Any] = [len(rows)]
        elif "c.id, c._etag" in query:
            results = [{"id": row["id"], "_etag": row["_etag"]} for row in rows]
        elif "VectorDistance" in query:
            results = [
                {key: value for key, value in row.items() if key != "embedding"}
                for row in rows
            ]
        else:
            results = rows
        if self.after_query is not None:
            hook = self.after_query
            self.after_query = None
            hook(query, results)

        async def iterate() -> AsyncIterator[Any]:
            for result in results:
                yield copy.deepcopy(result)

        return iterate()

    async def execute_item_batch(
        self,
        *,
        batch_operations: Sequence[tuple[Any, ...]],
        partition_key: str,
    ) -> list[dict[str, int]]:
        pending = copy.deepcopy(self.items)
        responses: list[dict[str, int]] = []
        next_etag = self._etag

        def assign_etag(body: dict[str, Any]) -> dict[str, Any]:
            nonlocal next_etag
            next_etag += 1
            return {**copy.deepcopy(body), "_etag": f'"etag-{next_etag}"'}

        for index, (operation, args, options) in enumerate(batch_operations):
            try:
                if operation == "create":
                    body = args[0]
                    key = (partition_key, str(body["id"]))
                    if key in pending:
                        raise _BatchFailure(409)
                    pending[key] = assign_etag(body)
                    responses.append({"statusCode": 201})
                elif operation == "replace":
                    item_id, body = args
                    key = (partition_key, str(item_id))
                    current = pending.get(key)
                    if current is None:
                        raise _BatchFailure(404)
                    if (
                        options.get("if_match_etag") is not None
                        and current["_etag"] != options["if_match_etag"]
                    ):
                        raise _BatchFailure(412)
                    pending[key] = assign_etag(body)
                    responses.append({"statusCode": 200})
                elif operation == "delete":
                    item_id = str(args[0])
                    key = (partition_key, item_id)
                    current = pending.get(key)
                    if current is None:
                        raise _BatchFailure(404)
                    if (
                        options.get("if_match_etag") is not None
                        and current["_etag"] != options["if_match_etag"]
                    ):
                        raise _BatchFailure(412)
                    del pending[key]
                    responses.append({"statusCode": 204})
                else:
                    raise AssertionError(f"unexpected batch operation {operation}")
            except _BatchFailure as exc:
                responses.append({"statusCode": exc.status})
                raise CosmosBatchOperationError(
                    error_index=index,
                    headers={},
                    status_code=exc.status,
                    operation_responses=responses,
                    message="batch failed",
                ) from exc
        self.items = pending
        self._etag = next_etag
        return responses


class _BatchFailure(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return dot / norm if norm else 0.0


def _record(
    user_id: str,
    text: str,
    *,
    memory_id: str,
    session_id: str | None = None,
    document_id: str | None = None,
    version: int = 1,
) -> MemoryRecord:
    return MemoryRecord(
        id=memory_id,
        user_id=user_id,
        text=text,
        session_id=session_id,
        document_id=document_id,
        version=version,
        embedding_model="embed",
    )


async def _create(
    store: CosmosMemoryStore,
    record: MemoryRecord,
    vector: Sequence[float],
) -> MemoryRecord:
    return await store.commit_create(
        await store.capture_state(record.user_id), record, vector
    )


async def test_cosmos_store_vector_recall_and_partition_isolation() -> None:
    store = CosmosMemoryStore(container=FakeCosmosContainer(), expected_dim=2)
    await _create(store, _record("alice", "cats", memory_id="m1"), [1.0, 0.0])
    await _create(store, _record("alice", "dogs", memory_id="m2"), [0.0, 1.0])
    await _create(store, _record("bob", "private", memory_id="m1"), [1.0, 0.0])

    hits = await store.search("alice", [1.0, 0.0], 2)
    assert [item.text for item in hits] == ["cats", "dogs"]
    assert hits[0].score == pytest.approx(1.0)
    assert [item.text for item in await store.list_memories("bob")] == ["private"]
    with pytest.raises(MemoryNotFoundError):
        await store.get_memory("bob", "m2")


async def test_cosmos_store_enforces_memory_etags_and_verifies_delete() -> None:
    store = CosmosMemoryStore(container=FakeCosmosContainer(), expected_dim=2)
    original = await _create(
        store, _record("alice", "before", memory_id="m1"), [1.0, 0.0]
    )
    assert original.etag
    updated = await store.commit_update(
        await store.capture_state("alice"),
        _record("alice", "after", memory_id="m1", version=2),
        [0.0, 1.0],
        expected_etag=original.etag,
    )
    assert updated.version == 2
    assert updated.etag != original.etag
    with pytest.raises(MemoryConflictError):
        await store.commit_update(
            await store.capture_state("alice"),
            _record("alice", "stale", memory_id="m1", version=2),
            [1.0, 0.0],
            expected_etag=original.etag,
        )
    assert await store.commit_delete(
        await store.capture_state("alice"),
        "m1",
        expected_etag=updated.etag or "",
    )
    with pytest.raises(MemoryNotFoundError):
        await store.get_memory("alice", "m1")


async def test_forget_does_not_delete_a_post_fence_update() -> None:
    container = FakeCosmosContainer()
    store = CosmosMemoryStore(container=container, expected_dim=2)
    await _create(
        store,
        _record("alice", "old", memory_id="m1", session_id="s1"),
        [1.0, 0.0],
    )

    def race(query: str, _results: list[dict[str, Any]]) -> None:
        if "c._etag" not in query:
            return
        item = container.items[("alice", "m1")]
        item["text"] = "new"
        item["writeEpoch"] = 1
        item["version"] = 2
        item["_etag"] = container._next_etag()

    container.after_query = race
    assert await store.erase_session("alice", "s1") == 0
    assert (await store.get_memory("alice", "m1")).text == "new"


async def test_document_replace_is_atomic_and_source_delete_is_permanent() -> None:
    store = CosmosMemoryStore(container=FakeCosmosContainer(), expected_dim=2)
    first = _record("alice", "first", memory_id="m1", document_id="doc")
    assert await store.replace_document("alice", "doc", [first], [[1.0, 0.0]]) == 1
    second = _record("alice", "second", memory_id="m2", document_id="doc")
    assert await store.replace_document("alice", "doc", [second], [[0.0, 1.0]]) == 1
    assert [item.text for item in await store.list_memories("alice")] == ["second"]

    assert await store.delete_document_source("alice", "doc") == 1
    assert await store.list_memories("alice") == []
    with pytest.raises(MemoryNotFoundError):
        await store.replace_document("alice", "doc", [first], [[1.0, 0.0]])


class FakeEmbedder:
    async def embed_one(self, text: str) -> list[float]:
        return [1.0, 0.0] if text else []

    async def embed(self, inputs: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _item in inputs]


class NoopPlanner:
    async def plan(
        self, user_text: str, candidates: Sequence[MemoryRecord]
    ) -> MemoryPlan:
        return MemoryPlan(action="noop")


def _service(container: FakeCosmosContainer) -> CosmosMemoryService:
    return CosmosMemoryService(
        store=CosmosMemoryStore(container=container, expected_dim=2),
        embedder=FakeEmbedder(),
        planner=NoopPlanner(),  # type: ignore[arg-type]
        embedding_model="embed",
    )


async def test_recall_keeps_similar_vectors_and_rejects_unrelated_vectors() -> None:
    container = FakeCosmosContainer()
    store = CosmosMemoryStore(container=container, expected_dim=2)
    await _create(store, _record("alice", "similar", memory_id="m1"), [1.0, 0.0])
    await _create(store, _record("alice", "unrelated", memory_id="m2"), [0.0, 1.0])

    assert [item.text for item in await _service(container).recall("alice", "query")] == [
        "similar"
    ]


async def test_explicit_crud_is_idempotent_and_promotes_user_lock() -> None:
    service = _service(FakeCosmosContainer())
    created = await service.create_memory(
        "alice", "Prefers concise answers", idempotency_key="create-1"
    )
    replay = await service.create_memory(
        "alice", "Prefers concise answers", idempotency_key="create-1"
    )
    assert replay.id == created.id
    assert created.locked is True
    assert created.origin == "user"

    updated = await service.update_memory(
        "alice",
        created.id,
        "Prefers brief answers",
        expected_etag=created.etag or "",
        idempotency_key="update-1",
    )
    replayed_update = await service.update_memory(
        "alice",
        created.id,
        "Prefers brief answers",
        expected_etag=created.etag or "",
        idempotency_key="update-1",
    )
    assert replayed_update.id == updated.id
    assert replayed_update.version == 2
    assert await service.delete_memory(
        "alice",
        created.id,
        expected_etag=updated.etag,
        idempotency_key="delete-1",
    )
    assert await service.delete_memory(
        "alice",
        created.id,
        expected_etag=updated.etag,
        idempotency_key="delete-1",
    )


class FakeGateway:
    def __init__(self, content: str, *, incomplete: bool = False) -> None:
        self.content = content
        self.incomplete = incomplete
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        result = {"choices": [{"message": {"content": self.content}}]}
        if self.incomplete:
            result["_responses_status"] = "incomplete"
        return result


async def test_planner_uses_strict_schema_and_rejects_locked_targets() -> None:
    locked = MemoryRecord(
        id="m1",
        user_id="alice",
        text="Explicit preference",
        origin="user",
        locked=True,
    )
    gateway = FakeGateway(
        '{"action":"update","memoryId":"m1","text":"Overwrite it"}'
    )
    planner = MemoryPlanner(gateway, "planner-deployment")  # type: ignore[arg-type]
    with pytest.raises(MemoryPlanError, match="locked"):
        await planner.plan("new preference", [locked])
    response_format = gateway.calls[0]["params"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True


async def test_planner_schema_is_azure_openai_strict_compatible() -> None:
    gateway = FakeGateway('{"action":"noop","memoryId":null,"text":null}')
    planner = MemoryPlanner(gateway, "planner-deployment")  # type: ignore[arg-type]

    assert (await planner.plan("transient request", [])).action == "noop"
    schema = gateway.calls[0]["params"]["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    serialized = json.dumps(schema)
    assert '"default"' not in serialized
    assert '"maxLength"' not in serialized


async def test_planner_forwards_responses_api_and_rejects_incomplete_plan() -> None:
    gateway = FakeGateway(
        '{"action":"noop","memoryId":null,"text":null}',
        incomplete=True,
    )
    planner = MemoryPlanner(
        gateway,  # type: ignore[arg-type]
        "planner-deployment",
        api="responses",
    )

    with pytest.raises(MemoryPlanError, match="incomplete"):
        await planner.plan("remember this", [])

    assert gateway.calls[0]["api"] == "responses"


async def test_planner_rejects_malformed_output() -> None:
    planner = MemoryPlanner(FakeGateway("not json"), "planner")  # type: ignore[arg-type]
    with pytest.raises(MemoryPlanError, match="invalid JSON"):
        await planner.plan("remember this", [])


def test_memory_record_timestamps_are_timezone_aware() -> None:
    record = _record("alice", "text", memory_id="m1")
    assert record.created_at.tzinfo == timezone.utc
    assert isinstance(record.created_at, datetime)


# --- remember() outcomes are distinguishable in the REAL service ----------------
#
# `CosmosMemoryService.remember` swallows every exception so a memory failure can
# never break a chat turn. That makes the returned outcome the only channel that
# can tell a caller what happened, so these drive the real service against a
# failing store rather than a fake that raises — no real implementation raises,
# so a fake that does would assert a branch production cannot reach.


class _FixedPlanner:
    def __init__(self, plan: MemoryPlan) -> None:
        self._plan = plan

    async def plan(
        self, user_text: str, candidates: Sequence[MemoryRecord]
    ) -> MemoryPlan:
        return self._plan


def _service_with_planner(
    container: FakeCosmosContainer, plan: MemoryPlan
) -> CosmosMemoryService:
    return CosmosMemoryService(
        store=CosmosMemoryStore(container=container, expected_dim=2),
        embedder=FakeEmbedder(),
        planner=_FixedPlanner(plan),  # type: ignore[arg-type]
        embedding_model="embed",
    )


async def test_a_store_outage_reports_unavailable_rather_than_noop() -> None:
    """Regression: a swallowed failure returned the same bare False as a planner
    'noop', so the remember_memory tool told the model "already covered, do not
    retry" during an outage."""
    service = _service(FakeCosmosContainer())

    async def boom(_user_id: str):
        raise RuntimeError("Cosmos 503 ServiceUnavailable")

    service._store.capture_state = boom  # type: ignore[method-assign]
    assert await service.remember("alice", "s1", "a durable fact to keep") == "unavailable"


async def test_a_planner_noop_still_reports_noop() -> None:
    """Control for the test above: the deliberate decline must be unaffected."""
    service = _service(FakeCosmosContainer())
    assert await service.remember("alice", "s1", "a durable fact to keep") == "noop"


async def test_a_planner_delete_reports_removed_not_saved() -> None:
    """A delete changes the store while storing nothing. Reporting it as a save
    named a fact that no later recall could ever find."""
    container = FakeCosmosContainer()
    store = CosmosMemoryStore(container=container, expected_dim=2)
    await _create(store, _record("alice", "Uses Slack daily", memory_id="m1"), [1.0, 0.0])

    service = _service_with_planner(
        container, MemoryPlan(action="delete", memoryId="m1")
    )
    assert await service.remember("alice", "s1", "The user stopped using Slack.") == (
        "removed"
    )
    assert await store.list_memories("alice") == []


async def test_a_planner_add_reports_saved() -> None:
    container = FakeCosmosContainer()
    service = _service_with_planner(
        container, MemoryPlan(action="add", text="The launch is in March.")
    )
    assert await service.remember("alice", "s1", "The launch is in March.") == "saved"
    store = CosmosMemoryStore(container=container, expected_dim=2)
    assert [item.text for item in await store.list_memories("alice")] == [
        "The launch is in March."
    ]
