"""Per-user document chunk vector store.

Mirrors :mod:`ai4ia_api.memory.pgvector_store`: AAD-only Postgres + pgvector, exact
cosine KNN (the 3072-dim embeddings exceed pgvector's ANN ceiling), lazy idempotent
init, injectable pool + token provider for tests. The differences from the memory
store are the schema (a ``doc_chunks`` table carrying ``document_id`` + grounding)
and the search filter: results are always scoped to ``user_id`` and may be further
restricted to a set of ``document_id`` values (retrieval over "these documents").

asyncpg + the azure SDK are imported lazily so the app and tests run without a live
database. An :class:`InMemoryDocChunkStore` backs local/dev + unit tests.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
_TOKEN_REFRESH_MARGIN_S = 300
_CONNECT_TIMEOUT_S = 10.0
_COMMAND_TIMEOUT_S = 10.0
_CLOSE_TIMEOUT_S = 5.0

_DDL_EXTENSION = "CREATE EXTENSION IF NOT EXISTS vector"
_DDL_TABLE = (
    "CREATE TABLE IF NOT EXISTS doc_chunks ("
    "id text PRIMARY KEY, "
    "user_id text NOT NULL, "
    "document_id text NOT NULL, "
    "chunk_index int NOT NULL, "
    "content text NOT NULL, "
    "heading text, "
    "char_start int, "
    "char_end int, "
    "start_ms int, "
    "end_ms int, "
    "speaker text, "
    "embedding vector({dim}) NOT NULL, "
    "created_at timestamptz NOT NULL DEFAULT now()"
    ")"
)
_DDL_USER_INDEX = "CREATE INDEX IF NOT EXISTS doc_chunks_user_idx ON doc_chunks (user_id)"
_DDL_DOC_INDEX = (
    "CREATE INDEX IF NOT EXISTS doc_chunks_doc_idx ON doc_chunks (user_id, document_id)"
)
# Additive migrations so an older doc_chunks table gains the
# audio/video time-grounding columns without a destructive recreate.
_DDL_ALTERS = (
    "ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS start_ms int",
    "ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS end_ms int",
    "ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS speaker text",
)

_INSERT = (
    "INSERT INTO doc_chunks "
    "(id, user_id, document_id, chunk_index, content, heading, char_start, char_end, "
    "start_ms, end_ms, speaker, embedding, created_at) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::vector, $13) "
    "ON CONFLICT (id) DO NOTHING"
)
# Cosine similarity = 1 - distance, scoped to user_id and (optionally) a set of
# document ids. $4 NULL => no document filter. Deterministic tie-break for tests.
_SEARCH = (
    "SELECT id, user_id, document_id, chunk_index, content, heading, char_start, "
    "char_end, start_ms, end_ms, speaker, created_at, "
    "1 - (embedding <=> $2::vector) AS score "
    "FROM doc_chunks "
    "WHERE user_id = $1 AND ($4::text[] IS NULL OR document_id = ANY($4)) "
    "ORDER BY embedding <=> $2::vector, document_id, chunk_index "
    "LIMIT $3"
)
_DELETE_DOCUMENT = (
    "WITH deleted AS ("
    "DELETE FROM doc_chunks WHERE user_id = $1 AND document_id = $2 RETURNING 1"
    ") SELECT count(*) FROM deleted"
)


@dataclass(slots=True)
class DocChunkRecord:
    user_id: str
    document_id: str
    chunk_index: int
    content: str
    heading: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    # Audio/video time grounding: the chunk's media start/end offset
    # in milliseconds and its speaker label, when known. ``None`` for documents.
    start_ms: int | None = None
    end_ms: int | None = None
    speaker: str | None = None
    id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    score: float | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.user_id}:{self.document_id}:{self.chunk_index}"


@runtime_checkable
class DocChunkStore(Protocol):
    async def add_many(
        self, records: Sequence[DocChunkRecord], vectors: Sequence[Sequence[float]]
    ) -> None: ...

    async def search(
        self,
        user_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        document_ids: Sequence[str] | None = None,
        query_text: str | None = None,
    ) -> list[DocChunkRecord]: ...

    async def delete_document(self, user_id: str, document_id: str) -> int: ...

    async def close(self) -> None: ...


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class InMemoryDocChunkStore:
    """Process-local doc-chunk store for local/dev + tests (python cosine scan)."""

    def __init__(self, *, expected_dim: int | None = None) -> None:
        self._expected_dim = expected_dim
        self._records: list[tuple[DocChunkRecord, list[float]]] = []

    def _check_dim(self, vector: Sequence[float]) -> None:
        if self._expected_dim is not None and len(vector) != self._expected_dim:
            raise ValueError(
                f"embedding dimension {len(vector)} != expected {self._expected_dim}"
            )

    async def add_many(
        self, records: Sequence[DocChunkRecord], vectors: Sequence[Sequence[float]]
    ) -> None:
        if len(records) != len(vectors):
            raise ValueError("records and vectors length mismatch")
        existing = {r.id for r, _ in self._records}
        for record, vector in zip(records, vectors):
            self._check_dim(vector)
            if record.id in existing:
                continue
            self._records.append((record, [float(x) for x in vector]))
            existing.add(record.id)

    async def search(
        self,
        user_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        document_ids: Sequence[str] | None = None,
        query_text: str | None = None,
    ) -> list[DocChunkRecord]:
        # ``query_text`` is accepted for protocol parity with the Azure AI Search
        # backend (hybrid + semantic); this in-memory store is pure-vector and
        # ignores it.
        self._check_dim(query_vector)
        allow = set(document_ids) if document_ids is not None else None
        scored: list[tuple[float, DocChunkRecord]] = []
        for record, vector in self._records:
            if record.user_id != user_id:
                continue
            if allow is not None and record.document_id not in allow:
                continue
            score = _cosine(query_vector, vector)
            scored.append((score, record))
        # Sort by score desc, then a deterministic key (document_id, chunk_index).
        scored.sort(key=lambda item: (-item[0], item[1].document_id, item[1].chunk_index))
        out: list[DocChunkRecord] = []
        for score, record in scored[: max(0, top_k)]:
            out.append(
                DocChunkRecord(
                    user_id=record.user_id,
                    document_id=record.document_id,
                    chunk_index=record.chunk_index,
                    content=record.content,
                    heading=record.heading,
                    char_start=record.char_start,
                    char_end=record.char_end,
                    start_ms=record.start_ms,
                    end_ms=record.end_ms,
                    speaker=record.speaker,
                    id=record.id,
                    created_at=record.created_at,
                    score=score,
                )
            )
        return out

    async def delete_document(self, user_id: str, document_id: str) -> int:
        before = len(self._records)
        self._records = [
            item
            for item in self._records
            if not (item[0].user_id == user_id and item[0].document_id == document_id)
        ]
        return before - len(self._records)

    async def close(self) -> None:
        return None


class TokenProvider(Protocol):
    async def __call__(self) -> str: ...


class _AadTokenProvider:
    """Caches + refreshes an AAD access token for Postgres, single-flight."""

    def __init__(self, credential: Any | None = None) -> None:
        self._credential = credential
        self._owns_credential = credential is None
        self._token: Any | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        tok = self._token
        return tok is not None and (tok.expires_on - time.time()) > _TOKEN_REFRESH_MARGIN_S

    async def __call__(self) -> str:
        if self._fresh():
            return self._token.token  # pyright: ignore[reportOptionalMemberAccess]
        async with self._lock:
            if self._fresh():
                return self._token.token  # pyright: ignore[reportOptionalMemberAccess]
            if self._credential is None:
                from azure.identity.aio import DefaultAzureCredential

                self._credential = DefaultAzureCredential()
            self._token = await self._credential.get_token(_PG_SCOPE)
            return self._token.token  # pyright: ignore[reportOptionalMemberAccess]

    async def close(self) -> None:
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()


class PgDocChunkStore:
    """Durable per-user doc-chunk store on Postgres + pgvector."""

    def __init__(
        self,
        *,
        host: str,
        database: str,
        user: str,
        expected_dim: int,
        port: int = 5432,
        pool: Any | None = None,
        token_provider: TokenProvider | None = None,
        min_size: int = 1,
        max_size: int = 4,
    ) -> None:
        self._host = host
        self._database = database
        self._user = user
        self._port = port
        self._expected_dim = expected_dim
        self._min_size = min_size
        self._max_size = max_size
        self._pool = pool
        self._owns_pool = pool is None
        self._token_provider = token_provider
        self._owns_token_provider = token_provider is None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _create_pool(self) -> Any:
        import asyncpg

        if self._token_provider is None:
            self._token_provider = _AadTokenProvider()
        return await asyncpg.create_pool(
            host=self._host,
            port=self._port,
            database=self._database,
            user=self._user,
            password=self._token_provider,
            ssl="require",
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=_CONNECT_TIMEOUT_S,
            command_timeout=_COMMAND_TIMEOUT_S,
        )

    async def _safe_close_pool(self) -> None:
        pool = self._pool
        if pool is None:
            return
        try:
            await asyncio.wait_for(pool.close(), timeout=_CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - never let shutdown hang on the pool
            terminate = getattr(pool, "terminate", None)
            if terminate is not None:
                terminate()

    async def ensure_ready(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            created = False
            if self._pool is None:
                self._pool = await self._create_pool()
                created = True
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(_DDL_EXTENSION)
                    await conn.execute(_DDL_TABLE.format(dim=int(self._expected_dim)))
                    for alter in _DDL_ALTERS:
                        await conn.execute(alter)
                    await conn.execute(_DDL_USER_INDEX)
                    await conn.execute(_DDL_DOC_INDEX)
            except BaseException:
                if created and self._owns_pool:
                    await self._safe_close_pool()
                    self._pool = None
                raise
            self._initialized = True

    def _vector_literal(self, vector: Sequence[float]) -> str:
        vec = list(vector)
        if self._expected_dim is not None and len(vec) != self._expected_dim:
            raise ValueError(
                f"embedding dimension {len(vec)} != expected {self._expected_dim}"
            )
        parts: list[str] = []
        for x in vec:
            f = float(x)
            if not math.isfinite(f):
                raise ValueError("embedding contains a non-finite value")
            parts.append(repr(f))
        return "[" + ",".join(parts) + "]"

    async def add_many(
        self, records: Sequence[DocChunkRecord], vectors: Sequence[Sequence[float]]
    ) -> None:
        if len(records) != len(vectors):
            raise ValueError("records and vectors length mismatch")
        if not records:
            return
        await self.ensure_ready()
        rows = [
            (
                record.id,
                record.user_id,
                record.document_id,
                record.chunk_index,
                record.content,
                record.heading,
                record.char_start,
                record.char_end,
                record.start_ms,
                record.end_ms,
                record.speaker,
                self._vector_literal(vector),
                record.created_at,
            )
            for record, vector in zip(records, vectors)
        ]
        async with self._pool.acquire() as conn:  # pyright: ignore[reportOptionalMemberAccess]
            await conn.executemany(_INSERT, rows)

    async def search(
        self,
        user_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        document_ids: Sequence[str] | None = None,
        query_text: str | None = None,
    ) -> list[DocChunkRecord]:
        # ``query_text`` is accepted for protocol parity with the Azure AI Search
        # backend; pgvector here ranks purely by vector distance and ignores it.
        await self.ensure_ready()
        literal = self._vector_literal(query_vector)
        doc_filter = list(document_ids) if document_ids is not None else None
        async with self._pool.acquire() as conn:  # pyright: ignore[reportOptionalMemberAccess]
            rows = await conn.fetch(_SEARCH, user_id, literal, max(0, top_k), doc_filter)
        return [
            DocChunkRecord(
                user_id=row["user_id"],
                document_id=row["document_id"],
                chunk_index=row["chunk_index"],
                content=row["content"],
                heading=row["heading"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                start_ms=row["start_ms"],
                end_ms=row["end_ms"],
                speaker=row["speaker"],
                id=row["id"],
                created_at=row["created_at"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def delete_document(self, user_id: str, document_id: str) -> int:
        await self.ensure_ready()
        async with self._pool.acquire() as conn:  # pyright: ignore[reportOptionalMemberAccess]
            count = await conn.fetchval(_DELETE_DOCUMENT, user_id, document_id)
        return int(count or 0)

    async def close(self) -> None:
        if self._owns_pool and self._pool is not None:
            await self._safe_close_pool()
        self._pool = None
        self._initialized = False
        if self._owns_token_provider and self._token_provider is not None:
            close = getattr(self._token_provider, "close", None)
            if close is not None:
                await close()
