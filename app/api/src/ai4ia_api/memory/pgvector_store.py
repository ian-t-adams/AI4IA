"""Durable per-user vector store on Azure Postgres Flexible Server + pgvector.

Design notes (see the Phase 5 increment-B review):

- **AAD-only auth.** The server has password auth disabled; the api managed
  identity is registered as a Postgres AAD role. We authenticate by passing a
  fresh AAD access token as the connection password. asyncpg invokes the
  ``password`` callable for every new connection, so token rotation is handled
  by :class:`_AadTokenProvider` (cached, refreshed before expiry, single-flight
  under a lock so concurrent connection setup can't stampede the credential).

- **Exact KNN (no ANN index).** ``text-embedding-3-large`` is 3072-dim, which
  exceeds pgvector's 2000-dim ceiling for ``hnsw``/``ivfflat`` indexes. We rely
  on an exact cosine scan filtered by ``user_id`` (a btree index keeps each
  user's slice small). At personal/demo scale this is correct and fast; an ANN
  path (``halfvec`` or dimensionality reduction) is a future migration.

- **Lazy, idempotent init.** The pool + schema are created on first use under a
  lock. Store ops are best-effort at the service layer, so a transient DB
  problem degrades to "no memory" and is retried on the next call (``forget`` is
  the exception — it surfaces failures, by design).

asyncpg and the azure SDK are imported lazily so the app and tests run without a
live database (tests inject a fake pool + token provider).

Hardening follow-up (not blocking for bootstrap): the api managed identity is
currently the Postgres AAD *admin*, which also lets it run the bootstrap DDL
above. Before production, provision a separate least-privilege AAD role for the
app (INSERT/SELECT/DELETE on ``memories`` only) and keep the admin for
migrations/bootstrap.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Sequence
from typing import Any, Protocol

from .models import MemoryRecord

logger = logging.getLogger(__name__)

# Scope for an Azure Database for PostgreSQL access token.
_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
# Refresh the cached token this many seconds before it actually expires.
_TOKEN_REFRESH_MARGIN_S = 300
# Bound DB connect + per-command time so a slow/unreachable database degrades
# memory to "no recall" quickly instead of stalling the (best-effort) chat path.
_CONNECT_TIMEOUT_S = 10.0
_COMMAND_TIMEOUT_S = 10.0
# Bound graceful pool close on shutdown before falling back to terminate().
_CLOSE_TIMEOUT_S = 5.0

_DDL_EXTENSION = "CREATE EXTENSION IF NOT EXISTS vector"
_DDL_TABLE = (
    "CREATE TABLE IF NOT EXISTS memories ("
    "id text PRIMARY KEY, "
    "user_id text NOT NULL, "
    "session_id text, "
    "kind text NOT NULL DEFAULT 'user_message', "
    "content text NOT NULL, "
    "embedding vector({dim}) NOT NULL, "
    "created_at timestamptz NOT NULL DEFAULT now()"
    ")"
)
_DDL_USER_INDEX = "CREATE INDEX IF NOT EXISTS memories_user_idx ON memories (user_id)"

_INSERT = (
    "INSERT INTO memories (id, user_id, session_id, kind, content, embedding, created_at) "
    "VALUES ($1, $2, $3, $4, $5, $6::vector, $7) "
    "ON CONFLICT (id) DO NOTHING"
)
# Cosine distance via <=> in [0, 2]; similarity = 1 - distance in [-1, 1] to
# match the in-memory cosine contract. Deterministic tie-break for stable tests.
_SEARCH = (
    "SELECT id, user_id, session_id, kind, content, created_at, "
    "1 - (embedding <=> $2::vector) AS score "
    "FROM memories WHERE user_id = $1 "
    "ORDER BY embedding <=> $2::vector, created_at DESC, id "
    "LIMIT $3"
)
_ERASE_USER = (
    "WITH deleted AS (DELETE FROM memories WHERE user_id = $1 RETURNING 1) "
    "SELECT count(*) FROM deleted"
)
_ERASE_SESSION = (
    "WITH deleted AS ("
    "DELETE FROM memories WHERE user_id = $1 AND session_id = $2 RETURNING 1"
    ") SELECT count(*) FROM deleted"
)


class TokenProvider(Protocol):
    """Returns a valid bearer token string (used as the Postgres password)."""

    async def __call__(self) -> str: ...


class _AadTokenProvider:
    """Caches + refreshes an AAD access token for Postgres, single-flight.

    The credential is created lazily on first call so importing this module never
    requires the azure SDK. A test may inject ``credential`` to avoid the SDK
    entirely. We close only a credential we created ourselves.
    """

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
            return self._token.token  # type: ignore[union-attr]
        async with self._lock:
            # Double-check: another waiter may have refreshed while we blocked.
            if self._fresh():
                return self._token.token  # type: ignore[union-attr]
            if self._credential is None:
                from azure.identity.aio import DefaultAzureCredential

                self._credential = DefaultAzureCredential()
            self._token = await self._credential.get_token(_PG_SCOPE)
            return self._token.token

    async def close(self) -> None:
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()


class PgVectorStore:
    """A durable, per-user vector store backed by Postgres + pgvector."""

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
        # Injected pool/token-provider (tests) are NOT owned: we don't close them.
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
        """Close the pool gracefully, terminating if it doesn't close in time."""
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
        """Create the pool + schema once (idempotent, concurrency-safe)."""
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
                    await conn.execute(_DDL_USER_INDEX)
            except BaseException:
                # Don't leave a half-initialized pool we created behind (covers
                # DDL errors and warmup cancellation via asyncio.wait_for).
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

    async def add(self, record: MemoryRecord, vector: Sequence[float]) -> None:
        await self.ensure_ready()
        literal = self._vector_literal(vector)
        async with self._pool.acquire() as conn:
            await conn.execute(
                _INSERT,
                record.id,
                record.user_id,
                record.session_id,
                record.kind,
                record.text,
                literal,
                record.created_at,
            )

    async def search(
        self, user_id: str, query_vector: Sequence[float], top_k: int
    ) -> list[MemoryRecord]:
        await self.ensure_ready()
        literal = self._vector_literal(query_vector)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_SEARCH, user_id, literal, max(0, top_k))
        return [
            MemoryRecord(
                user_id=row["user_id"],
                text=row["content"],
                session_id=row["session_id"],
                kind=row["kind"],
                id=row["id"],
                created_at=row["created_at"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def erase_user(self, user_id: str) -> int:
        await self.ensure_ready()
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(_ERASE_USER, user_id)
        return int(count or 0)

    async def erase_session(self, user_id: str, session_id: str) -> int:
        await self.ensure_ready()
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(_ERASE_SESSION, user_id, session_id)
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
