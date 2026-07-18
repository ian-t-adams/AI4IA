"""Unit tests for the pgvector store and its AAD token provider.

These run without a live Postgres or the azure SDK: a fake asyncpg pool records
SQL + returns canned rows, and a fake credential drives the token provider.
"""
from __future__ import annotations

import time

import pytest

from ai4ia_api.memory.models import MemoryRecord
from ai4ia_api.memory.pgvector_store import (
    _TOKEN_REFRESH_MARGIN_S,
    PgVectorStore,
    _AadTokenProvider,
)


class FakeRecord(dict):
    """asyncpg Records support __getitem__ by column name; a dict suffices."""


class FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fetched: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []
        self._rows: list[FakeRecord] = []
        self._val: object = 0

    def set_rows(self, rows: list[FakeRecord]) -> None:
        self._rows = rows

    def set_val(self, val: object) -> None:
        self._val = val

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetch(self, sql, *args):
        self.fetched.append((sql, args))
        return self._rows

    async def fetchval(self, sql, *args):
        self.fetchvals.append((sql, args))
        return self._val


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self.closed = False

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)

    async def close(self) -> None:
        self.closed = True


def _store(conn: FakeConn, *, expected_dim: int = 3) -> PgVectorStore:
    return PgVectorStore(
        host="h",
        database="mem0",
        user="api-id",
        expected_dim=expected_dim,
        pool=FakePool(conn),
        token_provider=_noop_token,
    )


async def _noop_token() -> str:
    return "token"


async def test_ensure_ready_runs_ddl_once_across_calls():
    conn = FakeConn()
    store = _store(conn)
    await store.ensure_ready()
    await store.ensure_ready()
    # CREATE EXTENSION / TABLE / INDEX exactly once despite two calls.
    ddl = [sql for sql, _ in conn.executed]
    assert sum("CREATE EXTENSION" in s for s in ddl) == 1
    assert sum("CREATE TABLE IF NOT EXISTS memories" in s for s in ddl) == 1
    assert any("vector(3)" in s for s in ddl)


async def test_add_inserts_with_vector_literal_and_record_fields():
    conn = FakeConn()
    store = _store(conn)
    rec = MemoryRecord(user_id="u1", text="hello world", session_id="s1")
    await store.add(rec, [0.1, 0.2, 0.3])
    insert = next((sql, args) for sql, args in conn.executed if "INSERT INTO memories" in sql)
    sql, args = insert
    assert "ON CONFLICT (id) DO NOTHING" in sql
    # Positional args: id, user_id, session_id, kind, content, vector-literal, created_at
    assert args[0] == rec.id
    assert args[1] == "u1"
    assert args[2] == "s1"
    assert args[4] == "hello world"
    assert args[5] == "[0.1,0.2,0.3]"
    assert args[6] == rec.created_at


async def test_search_maps_rows_to_records_with_score():
    conn = FakeConn()
    created = MemoryRecord(user_id="u1", text="x").created_at
    conn.set_rows([
        FakeRecord(
            id="abc",
            user_id="u1",
            session_id="s1",
            kind="user_message",
            content="remembered thing",
            created_at=created,
            score=0.83,
        )
    ])
    store = _store(conn)
    hits = await store.search("u1", [1.0, 0.0, 0.0], top_k=5)
    assert len(hits) == 1
    assert hits[0].text == "remembered thing"
    assert hits[0].user_id == "u1"
    assert hits[0].id == "abc"
    assert hits[0].score == pytest.approx(0.83)
    # The query filters by user and limits.
    sql, args = conn.fetched[0]
    assert "WHERE user_id = $1" in sql
    assert args[0] == "u1"
    assert args[2] == 5


async def test_search_clamps_negative_top_k_to_zero():
    conn = FakeConn()
    store = _store(conn)
    await store.search("u1", [1.0, 0.0, 0.0], top_k=-3)
    _sql, args = conn.fetched[0]
    assert args[2] == 0


async def test_erase_user_and_session_return_counts():
    conn = FakeConn()
    store = _store(conn)
    conn.set_val(4)
    assert await store.erase_user("u1") == 4
    conn.set_val(2)
    assert await store.erase_session("u1", "s1") == 2
    user_sql = conn.fetchvals[0][0]
    sess_sql, sess_args = conn.fetchvals[1]
    assert "DELETE FROM memories WHERE user_id = $1" in user_sql
    assert "session_id = $2" in sess_sql
    assert sess_args == ("u1", "s1")


async def test_erase_document_is_a_safe_no_op():
    # The ``memories`` table has no document_id column: erase_document must not
    # raise (MemoryStore requires it — callers like MemoryService.remember_document
    # / forget_document call it unconditionally) and must honestly report 0.
    conn = FakeConn()
    store = _store(conn)
    assert await store.erase_document("u1", "doc-1") == 0
    # No DELETE/erase statement is issued — there's nothing to key it on.
    assert conn.fetchvals == []


async def test_vector_literal_rejects_wrong_dimension():
    store = _store(FakeConn(), expected_dim=3)
    with pytest.raises(ValueError, match="dimension"):
        await store.add(MemoryRecord(user_id="u1", text="x"), [1.0, 2.0])


async def test_vector_literal_rejects_non_finite():
    store = _store(FakeConn(), expected_dim=3)
    with pytest.raises(ValueError, match="non-finite"):
        await store.add(MemoryRecord(user_id="u1", text="x"), [1.0, float("nan"), 0.0])


async def test_close_closes_owned_pool_only():
    conn = FakeConn()
    pool = FakePool(conn)
    # Injected pool is NOT owned -> close() must not close it.
    store = PgVectorStore(
        host="h", database="mem0", user="api-id", expected_dim=3,
        pool=pool, token_provider=_noop_token,
    )
    await store.ensure_ready()
    await store.close()
    assert pool.closed is False


# ---- token provider ----

class _Token:
    def __init__(self, token: str, expires_on: float) -> None:
        self.token = token
        self.expires_on = expires_on


class FakeCredential:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self.get_calls = 0
        self.closed = False

    async def get_token(self, scope: str) -> _Token:
        self.get_calls += 1
        return self._tokens.pop(0)

    async def close(self) -> None:
        self.closed = True


async def test_token_provider_caches_until_refresh_margin():
    far = time.time() + _TOKEN_REFRESH_MARGIN_S + 3600
    cred = FakeCredential([_Token("t1", far)])
    provider = _AadTokenProvider(credential=cred)
    assert await provider() == "t1"
    assert await provider() == "t1"  # cached, no second get_token
    assert cred.get_calls == 1


async def test_token_provider_refreshes_after_expiry():
    near = time.time() + 10  # within the refresh margin -> treated as stale
    later = time.time() + _TOKEN_REFRESH_MARGIN_S + 3600
    cred = FakeCredential([_Token("t1", near), _Token("t2", later)])
    provider = _AadTokenProvider(credential=cred)
    assert await provider() == "t1"
    assert await provider() == "t2"
    assert cred.get_calls == 2


async def test_token_provider_does_not_close_injected_credential():
    cred = FakeCredential([_Token("t1", time.time() + 99999)])
    provider = _AadTokenProvider(credential=cred)
    await provider()
    await provider.close()
    assert cred.closed is False


# ---- partial-pool cleanup (owned pool, DDL failure) ----

class FailingConn(FakeConn):
    async def execute(self, sql, *args):
        await super().execute(sql, args)
        raise RuntimeError("ddl boom")


class TerminablePool(FakePool):
    def __init__(self, conn: FakeConn) -> None:
        super().__init__(conn)
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


async def test_ensure_ready_tears_down_owned_pool_on_ddl_failure():
    pool = TerminablePool(FailingConn())
    # pool=None -> store owns the pool it creates; monkeypatch creation.
    store = PgVectorStore(
        host="h", database="mem0", user="api-id", expected_dim=3,
        token_provider=_noop_token,
    )

    async def _fake_create_pool():
        return pool

    store._create_pool = _fake_create_pool  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="ddl boom"):
        await store.ensure_ready()
    # A half-initialized owned pool must be closed and dropped so a retry recreates.
    assert pool.closed is True
    assert store._pool is None
    assert store._initialized is False

