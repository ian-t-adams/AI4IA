"""AAD-authenticated psycopg3 connection pool for the real-mem0 backend.

mem0's pgvector store accepts a pre-built ``connection_pool`` that overrides all
other connection settings. Our Azure Postgres Flexible Server has SQL-password
auth disabled (AAD only), so we inject a :mod:`psycopg_pool` pool whose
connections authenticate with a fresh AAD access token used *as the password*.

Why a ``connection_class`` subclass (not a static password): psycopg, unlike
asyncpg, has no password-callable. The pool calls
``connection_class.connect(conninfo, **kwargs)`` to create every *new* physical
connection — initial fill, ``max_lifetime`` recycles, and reconnects after a
dropped connection — so overriding ``connect`` to fetch a token at that moment
guarantees each physical connection is born with a currently-valid token. Once a
connection is authenticated it stays valid (Postgres checks the token only at
connect time), so token expiry on long-lived connections is a non-issue.

Everything here is synchronous: mem0 runs its vector-store calls (and the pool's
own maintenance workers) in worker threads with no event loop, so the credential
and token cache must be thread-safe, not asyncio-based.

psycopg / psycopg_pool / the azure SDK are imported lazily so importing this
module never requires them (the mem0 backend is opt-in; tests inject fakes).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Scope for an Azure Database for PostgreSQL access token.
_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
# Refresh the cached token this many seconds before it actually expires.
_TOKEN_REFRESH_MARGIN_S = 300


def normalize_azure_openai_endpoint(url: str) -> str:
    """Reduce a model-gateway URL to the bare host the AzureOpenAI SDK expects.

    The openai SDK builds request paths as ``{endpoint}/openai/deployments/...``,
    so the endpoint must NOT already include the ``/openai`` suffix our gateway
    base URL carries. Strips a trailing ``/openai`` (with or without a trailing
    slash); leaves an endpoint that lacks it unchanged.

    >>> normalize_azure_openai_endpoint("https://x.azure-api.net/openai")
    'https://x.azure-api.net'
    >>> normalize_azure_openai_endpoint("https://x.azure-api.net/openai/")
    'https://x.azure-api.net'
    >>> normalize_azure_openai_endpoint("https://x.azure-api.net")
    'https://x.azure-api.net'
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    if path.lower().endswith("/openai"):
        path = path[: -len("/openai")]
    elif path.lower() == "openai":
        path = ""
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


class SyncAadTokenProvider:
    """Thread-safe cache of an AAD access token for Postgres (used as password).

    Mirrors ``_AadTokenProvider`` from the custom pgvector store but synchronous:
    it is invoked from psycopg pool worker threads, not the event loop. A test
    may inject ``credential`` to avoid the azure SDK; we close only a credential
    we created ourselves.
    """

    def __init__(self, credential: Any | None = None) -> None:
        self._credential = credential
        self._owns_credential = credential is None
        self._token: Any | None = None
        self._lock = threading.Lock()

    def _fresh(self) -> bool:
        tok = self._token
        return tok is not None and (tok.expires_on - time.time()) > _TOKEN_REFRESH_MARGIN_S

    def get_token(self) -> str:
        if self._fresh():
            return self._token.token  # type: ignore[union-attr]
        with self._lock:
            if self._fresh():
                return self._token.token  # type: ignore[union-attr]
            if self._credential is None:
                from azure.identity import DefaultAzureCredential

                self._credential = DefaultAzureCredential()
            self._token = self._credential.get_token(_PG_SCOPE)
            return self._token.token

    def close(self) -> None:
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 - never let shutdown raise
                    logger.warning("closing aad credential failed", exc_info=True)


def _build_connection_class(provider: SyncAadTokenProvider) -> type:
    """A psycopg ``Connection`` subclass that injects a fresh token per connect."""
    import psycopg

    class _AadConnection(psycopg.Connection):  # type: ignore[type-arg]
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs: Any):  # type: ignore[override]
            kwargs["password"] = provider.get_token()
            kwargs.setdefault("sslmode", "require")
            return super().connect(conninfo, **kwargs)

    return _AadConnection


def build_aad_pool(
    *,
    host: str,
    database: str,
    user: str,
    port: int = 5432,
    provider: SyncAadTokenProvider | None = None,
    min_size: int = 1,
    max_size: int = 4,
) -> Any:
    """Build (and open) a psycopg3 pool that authenticates with rotating AAD tokens.

    Blocks while the initial connections are opened, so call it off the event
    loop (``asyncio.to_thread``). The caller owns the returned pool and the
    provider and must close both on shutdown.
    """
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import ConnectionPool

    token_provider = provider or SyncAadTokenProvider()
    conn_cls = _build_connection_class(token_provider)
    # Build the libpq conninfo safely (escapes any special chars in params);
    # password is injected per-connect by the connection class.
    conninfo = make_conninfo(
        host=host, port=int(port), dbname=database, user=user, sslmode="require"
    )
    pool = ConnectionPool(
        conninfo=conninfo,
        connection_class=conn_cls,
        min_size=min_size,
        max_size=max_size,
        # Keep the default 1h recycle: each recycled/new connection is reborn
        # with a fresh token, so connections never outlive their credential.
        max_lifetime=3600.0,
        timeout=30.0,
        open=True,
    )
    return pool
