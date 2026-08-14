from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

if __package__:
    from ._loader import load_script
else:
    from _loader import load_script

SCRIPT = Path(__file__).parents[1] / "migrate-memory-to-cosmos.py"
migration = load_script("memory_cosmos_migration", SCRIPT, register=True)


def _row(
    *,
    source_id: str = "79a0",
    user_id: str | None = "user-1",
    text: str = "Prefers concise answers",
    embedding: list[float] | str = "[1.0,0.0]",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "data": text,
        "run_id": "session-1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    if user_id is not None:
        payload["user_id"] = user_id
    return {
        "id": source_id,
        "embedding": embedding,
        "payload": payload,
    }


class FakeTarget:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.safe_checks: list[list[str]] = []

    async def assert_safe(self, user_ids: list[str]) -> None:
        self.safe_checks.append(user_ids)

    async def create_or_verify(self, item: dict[str, Any]) -> bool:
        key = (item["userId"], item["id"])
        if key in self.items:
            await self.verify(item)
            return False
        self.items[key] = item
        return True

    async def verify(self, item: dict[str, Any]) -> None:
        assert self.items[(item["userId"], item["id"])] == item

    async def count_user(self, user_id: str) -> int:
        return sum(key[0] == user_id for key in self.items)

    async def recall_contains(self, item: dict[str, Any], *, top_k: int) -> bool:
        assert top_k == 10
        return (item["userId"], item["id"]) in self.items


class FakeSafetyContainer:
    def __init__(
        self, *, state_exists: bool, not_found_error: type[Exception]
    ) -> None:
        self.state_exists = state_exists
        self.not_found_error = not_found_error

    async def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        assert item == "state"
        assert partition_key == "user-1"
        if not self.state_exists:
            raise self.not_found_error
        return {
            "id": "state",
            "type": "state",
            "userId": partition_key,
            "epoch": 0,
            "cutoffs": [],
        }


def test_parse_source_row_preserves_owner_text_vector_and_scope() -> None:
    parsed = migration.parse_source_row(_row(), expected_dimensions=2)
    assert isinstance(parsed, migration.SourceMemory)
    assert parsed.user_id == "user-1"
    assert parsed.text == "Prefers concise answers"
    assert parsed.embedding == (1.0, 0.0)
    assert parsed.session_id == "session-1"
    item = migration.build_cosmos_item(parsed, embedding_model="embed")
    assert item["userId"] == "user-1"
    assert item["migration"]["source"] == "mem0"
    assert item["migration"]["textHash"] != item["text"]


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_row(user_id=None), "missing_user_id"),
        (_row(embedding="[1.0]"), "wrong_embedding_dimensions"),
        (_row(embedding="[NaN,0.0]"), "non_finite_embedding"),
    ],
)
def test_invalid_or_unowned_rows_are_quarantined_without_plaintext(
    row: dict[str, Any], reason: str
) -> None:
    parsed = migration.parse_source_row(row, expected_dimensions=2)
    assert isinstance(parsed, migration.QuarantinedRow)
    assert parsed.reason == reason
    assert "Prefers concise answers" not in repr(parsed)


def test_migration_is_dry_run_by_default_and_idempotent_on_apply() -> None:
    async def exercise() -> None:
        target = FakeTarget()
        counters, quarantine = await migration.migrate(
            [_row(), _row(source_id="bad", user_id=None)],
            target=None,
            apply=False,
            verify=False,
            expected_dimensions=2,
            embedding_model="embed",
            recall_samples=0,
        )
        assert counters == {"scanned": 2, "valid": 1, "quarantined": 1}
        assert len(quarantine) == 1
        assert target.items == {}

        first, _ = await migration.migrate(
            [_row()],
            target=target,
            apply=True,
            verify=True,
            expected_dimensions=2,
            embedding_model="embed",
            recall_samples=1,
        )
        second, _ = await migration.migrate(
            [_row()],
            target=target,
            apply=True,
            verify=True,
            expected_dimensions=2,
            embedding_model="embed",
            recall_samples=1,
        )
        assert first["created"] == 1
        assert first["verified"] == 1
        assert first["recallSamples"] == 1
        assert second["unchanged"] == 1
        assert len(target.items) == 1
        assert target.safe_checks == [["user-1"], ["user-1"]]

    asyncio.run(exercise())


def test_apply_refuses_any_partition_observed_by_cosmos_runtime(monkeypatch) -> None:
    class FakeNotFoundError(Exception):
        pass

    exceptions = types.ModuleType("azure.cosmos.exceptions")
    exceptions.CosmosResourceNotFoundError = FakeNotFoundError
    cosmos = types.ModuleType("azure.cosmos")
    cosmos.exceptions = exceptions
    azure = types.ModuleType("azure")
    azure.cosmos = cosmos
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.cosmos", cosmos)
    monkeypatch.setitem(sys.modules, "azure.cosmos.exceptions", exceptions)

    async def exercise() -> None:
        untouched = migration.CosmosMigrationTarget(
            FakeSafetyContainer(
                state_exists=False, not_found_error=FakeNotFoundError
            )
        )
        await untouched.assert_safe(["user-1"])

        observed = migration.CosmosMigrationTarget(
            FakeSafetyContainer(
                state_exists=True, not_found_error=FakeNotFoundError
            )
        )
        with pytest.raises(RuntimeError, match="runtime memory state"):
            await observed.assert_safe(["user-1"])

    asyncio.run(exercise())
