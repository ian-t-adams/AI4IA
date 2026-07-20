#!/usr/bin/env python3
"""Dry-run-first migration of owned mem0 pgvector rows into Cosmos memory."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
_DEFAULT_TIMESTAMP = "1970-01-01T00:00:00Z"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SourceMemory:
    source_id: str
    user_id: str
    text: str
    embedding: tuple[float, ...]
    session_id: str | None
    created_at: str
    updated_at: str

    @property
    def source_hash(self) -> str:
        return hashlib.sha256(self.source_id.encode("utf-8")).hexdigest()

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def target_id(self) -> str:
        return f"m-migrated-{self.source_hash[:32]}"


@dataclass(frozen=True)
class QuarantinedRow:
    source_hash: str
    reason: str


class MigrationTarget(Protocol):
    async def assert_safe(self, user_ids: Sequence[str]) -> None: ...

    async def create_or_verify(self, item: dict[str, Any]) -> bool: ...

    async def verify(self, item: dict[str, Any]) -> None: ...

    async def count_user(self, user_id: str) -> int: ...

    async def recall_contains(self, item: dict[str, Any], *, top_k: int) -> bool: ...


def parse_source_row(
    row: dict[str, Any], *, expected_dimensions: int
) -> SourceMemory | QuarantinedRow:
    source_id = str(row.get("id") or "")
    source_hash = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return QuarantinedRow(source_hash, "invalid_payload_json")
    if not isinstance(payload, dict):
        return QuarantinedRow(source_hash, "invalid_payload")
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return QuarantinedRow(source_hash, "missing_user_id")
    text = payload.get("data")
    if not isinstance(text, str) or not text.strip():
        return QuarantinedRow(source_hash, "missing_text")
    try:
        embedding = _parse_embedding(row.get("embedding"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return QuarantinedRow(source_hash, "invalid_embedding")
    if len(embedding) != expected_dimensions:
        return QuarantinedRow(source_hash, "wrong_embedding_dimensions")
    if not all(math.isfinite(value) for value in embedding):
        return QuarantinedRow(source_hash, "non_finite_embedding")
    session_id = payload.get("run_id")
    if session_id is not None and not isinstance(session_id, str):
        return QuarantinedRow(source_hash, "invalid_session_id")
    created_at = _timestamp(payload.get("created_at"))
    updated_at = _timestamp(payload.get("updated_at"), fallback=created_at)
    return SourceMemory(
        source_id=source_id,
        user_id=user_id.strip(),
        text=text.strip(),
        embedding=tuple(embedding),
        session_id=session_id,
        created_at=created_at,
        updated_at=updated_at,
    )


def build_cosmos_item(
    source: SourceMemory, *, embedding_model: str
) -> dict[str, Any]:
    return {
        "id": source.target_id,
        "type": "memory",
        "userId": source.user_id,
        "text": source.text,
        "sessionId": source.session_id,
        "documentId": None,
        "kind": "fact",
        "origin": "implicit",
        "locked": False,
        "embedding": list(source.embedding),
        "embeddingModel": embedding_model,
        "writeEpoch": 0,
        "version": 1,
        "createdAt": source.created_at,
        "updatedAt": source.updated_at,
        "migration": {
            "source": "mem0",
            "sourceIdHash": source.source_hash,
            "textHash": source.text_hash,
        },
    }


async def migrate(
    rows: Iterable[dict[str, Any]],
    *,
    target: MigrationTarget | None,
    apply: bool,
    verify: bool,
    expected_dimensions: int,
    embedding_model: str,
    recall_samples: int,
) -> tuple[dict[str, int], list[QuarantinedRow]]:
    counters: Counter[str] = Counter()
    quarantine: list[QuarantinedRow] = []
    valid_items: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    for row in rows:
        parsed = parse_source_row(row, expected_dimensions=expected_dimensions)
        counters["scanned"] += 1
        if isinstance(parsed, QuarantinedRow):
            counters["quarantined"] += 1
            quarantine.append(parsed)
            continue
        item = build_cosmos_item(parsed, embedding_model=embedding_model)
        valid_items.append(item)
        source_counts[parsed.user_id] += 1
        counters["valid"] += 1

    if (apply or verify) and target is None:
        raise ValueError("a Cosmos target is required for apply or verify")
    if apply:
        assert target is not None
        await target.assert_safe(sorted(source_counts))
        for item in valid_items:
            if await target.create_or_verify(item):
                counters["created"] += 1
            else:
                counters["unchanged"] += 1
    if verify:
        assert target is not None
        for item in valid_items:
            await target.verify(item)
            counters["verified"] += 1
        for user_id, expected in source_counts.items():
            actual = await target.count_user(user_id)
            if actual != expected:
                raise RuntimeError(
                    f"migrated count mismatch for user partition: {actual} != {expected}"
                )
        for item in valid_items[: max(0, recall_samples)]:
            if not await target.recall_contains(item, top_k=10):
                raise RuntimeError("sampled vector recall did not return the source memory")
            counters["recallSamples"] += 1
    return dict(counters), quarantine


class CosmosMigrationTarget:
    def __init__(self, container: Any) -> None:
        self._container = container

    async def assert_safe(self, user_ids: Sequence[str]) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        for user_id in user_ids:
            try:
                await self._container.read_item(
                    item="state", partition_key=user_id
                )
            except CosmosResourceNotFoundError:
                continue
            # The runtime creates/touches state before every recall or mutation.
            # Refuse any partition it has observed, even at epoch zero: a user may
            # have explicitly deleted a migrated item without running /forget.
            raise RuntimeError(
                "target partition has runtime memory state; refusing to resurrect "
                "legacy data after cutover"
            )

    async def create_or_verify(self, item: dict[str, Any]) -> bool:
        from azure.cosmos.exceptions import CosmosResourceExistsError

        try:
            await self._container.create_item(item)
            return True
        except CosmosResourceExistsError:
            await self.verify(item)
            return False

    async def verify(self, item: dict[str, Any]) -> None:
        existing = await self._container.read_item(
            item=item["id"], partition_key=item["userId"]
        )
        expected_migration = item["migration"]
        existing_text = existing.get("text")
        existing_text_hash = (
            hashlib.sha256(existing_text.encode("utf-8")).hexdigest()
            if isinstance(existing_text, str)
            else None
        )
        if (
            existing.get("type") != "memory"
            or existing.get("userId") != item["userId"]
            or existing.get("migration") != expected_migration
            or existing_text_hash != expected_migration["textHash"]
            or len(existing.get("embedding") or []) != len(item["embedding"])
        ):
            raise RuntimeError("existing Cosmos migration item does not match its source")

    async def count_user(self, user_id: str) -> int:
        query = (
            "SELECT VALUE COUNT(1) FROM c WHERE c.userId = @uid "
            "AND c.type = 'memory' AND c.migration.source = 'mem0'"
        )
        values = [
            item
            async for item in self._container.query_items(
                query=query,
                parameters=[{"name": "@uid", "value": user_id}],
                partition_key=user_id,
            )
        ]
        return int(values[0]) if values else 0

    async def recall_contains(self, item: dict[str, Any], *, top_k: int) -> bool:
        query = (
            "SELECT TOP @limit c.id FROM c WHERE c.userId = @uid "
            "AND c.type = 'memory' "
            "ORDER BY VectorDistance(c.embedding, @embedding)"
        )
        rows = [
            row
            async for row in self._container.query_items(
                query=query,
                parameters=[
                    {"name": "@limit", "value": top_k},
                    {"name": "@uid", "value": item["userId"]},
                    {"name": "@embedding", "value": item["embedding"]},
                ],
                partition_key=item["userId"],
            )
        ]
        return item["id"] in {str(row["id"]) for row in rows}


def _parse_embedding(value: Any) -> list[float]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray | str):
        raise TypeError("embedding must be a sequence")
    return [float(item) for item in value]


def _timestamp(value: Any, *, fallback: str = _DEFAULT_TIMESTAMP) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-host", default=os.getenv("AI4IA_POSTGRES_HOST"))
    parser.add_argument(
        "--postgres-database", default=os.getenv("AI4IA_POSTGRES_DATABASE", "mem0")
    )
    parser.add_argument("--postgres-user", default=os.getenv("AI4IA_POSTGRES_USER"))
    parser.add_argument("--postgres-port", type=int, default=5432)
    parser.add_argument("--source-table", default="mem0_memories")
    parser.add_argument("--cosmos-endpoint", default=os.getenv("AI4IA_COSMOS_ENDPOINT"))
    parser.add_argument(
        "--cosmos-database", default=os.getenv("AI4IA_COSMOS_DATABASE", "ai4ia")
    )
    parser.add_argument("--embedding-model", default="text-embedding-3-large")
    parser.add_argument("--dimensions", type=int, default=3072)
    parser.add_argument("--quarantine-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--recall-samples", type=int, default=5)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if not args.postgres_host or not args.postgres_user:
        raise ValueError("--postgres-host and --postgres-user are required")
    if not _IDENTIFIER.fullmatch(args.source_table):
        raise ValueError("--source-table must be a plain SQL identifier")
    if (args.apply or args.verify) and not args.cosmos_endpoint:
        raise ValueError("--cosmos-endpoint is required for apply or verify")

    import asyncpg
    from azure.cosmos.aio import CosmosClient
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    connection = None
    cosmos = None
    try:
        token = await credential.get_token(_PG_SCOPE)
        connection = await asyncpg.connect(
            host=args.postgres_host,
            database=args.postgres_database,
            user=args.postgres_user,
            password=token.token,
            port=args.postgres_port,
            ssl="require",
        )
        rows = await connection.fetch(
            f"SELECT id::text AS id, vector::text AS embedding, payload "
            f"FROM {args.source_table} ORDER BY id"
        )
        target: CosmosMigrationTarget | None = None
        if args.apply or args.verify:
            cosmos = CosmosClient(args.cosmos_endpoint, credential=credential)
            container = cosmos.get_database_client(
                args.cosmos_database
            ).get_container_client("memories")
            target = CosmosMigrationTarget(container)
        counters, quarantine = await migrate(
            [dict(row) for row in rows],
            target=target,
            apply=args.apply,
            verify=args.verify,
            expected_dimensions=args.dimensions,
            embedding_model=args.embedding_model,
            recall_samples=args.recall_samples,
        )
        if args.quarantine_file is not None:
            args.quarantine_file.write_text(
                "".join(
                    json.dumps(
                        {
                            "sourceIdHash": item.source_hash,
                            "reason": item.reason,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in quarantine
                ),
                encoding="utf-8",
            )
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "verified": bool(args.verify),
                    **counters,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if connection is not None:
            await connection.close()
        if cosmos is not None:
            await cosmos.close()
        await credential.close()


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
