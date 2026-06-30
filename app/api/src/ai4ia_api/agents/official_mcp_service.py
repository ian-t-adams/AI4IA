"""Curated "official" MCP plane — servers reached through the MCP APIM front door.

This is the official-server analogue of :class:`~ai4ia_api.agents.mcp_service.
McpServerService`. Where that service owns a *per-user* registry of BYO remote
MCP servers (called directly behind the SSRF guard, with per-user Key Vault
secrets), this one owns a small, *admin-curated* set of servers reached
**through the dedicated MCP APIM gateway** (``infra/modules/mcpgateway.bicep``)
and gated on a single app-global APIM subscription key.

It deliberately exposes the **same execution seam** the per-turn tool builders
consume — ``connector``, ``resolver``, ``secret_for`` (a
:class:`~ai4ia_api.agents.mcp_execution.SecretResolver`) and ``record_health``
(a :class:`~ai4ia_api.agents.mcp_execution.HealthReporter`) — so the official
plane plugs into ``build_mcp_turn_tools_multi`` exactly like the BYO plane, with
no special-casing in the hot path.

Design notes:

* **Projection.** Each catalog entry becomes a :class:`UserMcpServer` with
  ``userId="__official__"``, ``authMode=apim_subscription``, ``trusted=True``
  (curated ⇒ pre-approved, no human gate), ``host`` = the APIM gateway host (so
  the projected tools' egress allowlist is scoped to APIM), and ``endpoint`` =
  ``<gateway_url>/<path>``. No per-server secret is stored — the subscription key
  is app-global and supplied by :meth:`secret_for`.
* **Lazy, cached discovery.** There is no registration step, so tools are
  discovered the first time the plane is used and cached on the (in-memory)
  records for the process lifetime. A server that fails discovery contributes
  **zero** tools and never breaks a turn; it is retried at most once per
  ``retry_interval_s`` so a transient APIM blip self-heals without re-discovering
  healthy servers every turn. Once every server has discovered successfully the
  hot path is lock-free and network-free.
* **Default-OFF / empty.** With the feature flag off (the default) this service
  is never constructed; with an empty catalog (also the default) ``list_all``
  returns ``[]`` and nothing is wired into a turn.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

from . import mcp_health
from .mcp_client import McpAuth, McpConnector
from .mcp_servers import McpAuthMode, McpTransport, UserMcpServer, _now
from .ssrf import Resolver
from ..official_mcp_catalog import OfficialMcpCatalog

logger = logging.getLogger(__name__)

# Synthetic owner partition for projected official records. Not a real user; it
# only keeps the records shaped like BYO records so they flow through the shared
# governance + execution seam unchanged.
OFFICIAL_USER_ID = "__official__"

# Default minimum spacing between discovery re-attempts for a server that has not
# yet discovered successfully, so a persistently failing server does not get
# hammered every turn while a transient failure still self-heals.
DEFAULT_RETRY_INTERVAL_S = 60.0


def build_official_servers(
    catalog: OfficialMcpCatalog, *, gateway_url: str
) -> list[UserMcpServer]:
    """Project the official catalog onto durable-shaped :class:`UserMcpServer` records.

    Pure + side-effect-free (no discovery), so it is unit-testable on its own. The
    absolute endpoint is ``<gateway_url>/<path>`` and ``host`` is the gateway host,
    which scopes each projected tool's egress allowlist to the APIM front door.
    """
    base = gateway_url.rstrip("/")
    host = urlparse(base).hostname or ""
    servers: list[UserMcpServer] = []
    for entry in catalog.servers:
        path = entry.path.lstrip("/")
        servers.append(
            UserMcpServer(
                id=entry.id,
                userId=OFFICIAL_USER_ID,
                name=entry.id,
                displayName=entry.displayName or entry.id,
                description=entry.description,
                endpoint=f"{base}/{path}",
                host=host,
                transport=McpTransport.streamable_http,
                authMode=McpAuthMode.apim_subscription,
                # Curated/admin-vetted ⇒ trusted ⇒ tools are pre-approved (no
                # per-call human gate). BYO servers are untrusted by default.
                trusted=True,
                enabled=True,
                # No per-server secret: the APIM subscription key is app-global
                # and supplied by ``secret_for`` at call time.
                secretRef=None,
            )
        )
    return servers


class OfficialMcpService:
    """Owns the projected official servers, their lazy discovery, and the app-global key.

    Mirrors the subset of :class:`~ai4ia_api.agents.mcp_service.McpServerService`
    the execution seam depends on, so the official plane is a drop-in second plane
    for ``build_mcp_turn_tools_multi``.
    """

    def __init__(
        self,
        catalog: OfficialMcpCatalog,
        *,
        gateway_url: str,
        subscription_key: str,
        connector: McpConnector,
        resolver: Resolver | None = None,
        retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    ) -> None:
        self._connector = connector
        self._resolver = resolver
        self._subscription_key = subscription_key
        self._retry_interval_s = retry_interval_s
        # The app-global credential presented to APIM on every official call.
        self._auth = McpAuth(
            mode=McpAuthMode.apim_subscription, secret=subscription_key
        )
        # Built once; the SAME instances are reused across turns so in-memory
        # health/quarantine + discovered-tool caches persist for the process.
        self._servers = build_official_servers(catalog, gateway_url=gateway_url)
        self._discovered_ok: set[str] = set()
        self._last_attempt: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """No persistent resources to release.

        The connector creates a short-lived, IP-pinned client per call (it owns no
        pooled connection of its own), and the records are in-memory. Present for
        lifecycle symmetry with :class:`McpServerService`.
        """
        return None

    @property
    def connector(self) -> McpConnector:
        """The MCP connector (shared by discovery and per-turn execution)."""
        return self._connector

    @property
    def resolver(self) -> Resolver | None:
        """The DNS resolver used for SSRF egress re-validation (``None`` = system).

        APIM is a public host, so the SSRF guard passes; exposing the resolver lets
        the execution path re-validate with the same resolver discovery used.
        """
        return self._resolver

    async def list_all(self) -> list[UserMcpServer]:
        """Return the official servers with tools discovered (best-effort, cached).

        Fast path (every server already discovered, or every undiscovered server
        attempted within ``retry_interval_s``) is lock-free and network-free. Only
        when a server is due for a (re)discovery attempt is the lock taken.
        """
        if not self._servers:
            return []
        now = time.monotonic()
        if not self._pending(now):
            return self._servers
        async with self._lock:
            pending = self._pending(time.monotonic())
            if pending:
                await self._discover_many(pending)
        return self._servers

    def refresh(self) -> None:
        """Drop the discovery cache so the next :meth:`list_all` re-discovers.

        An explicit escape hatch (e.g. for a future admin endpoint) so a server
        whose tools changed — or one that failed its first discovery — can be
        re-attempted without a process restart. In-memory health state is left
        intact.
        """
        self._discovered_ok.clear()
        self._last_attempt.clear()

    async def secret_for(self, server: UserMcpServer) -> str | None:
        """Resolve the credential for an official server — the app-global APIM key.

        A :class:`~ai4ia_api.agents.mcp_execution.SecretResolver`. The key is the
        same for every official server (it authenticates to the one MCP APIM front
        door), so the per-server record is irrelevant.
        """
        return self._subscription_key

    async def record_health(
        self, server: UserMcpServer, *, ok: bool, error: object | None = None
    ) -> None:
        """Record a per-turn tool-call outcome against the in-memory record.

        A :class:`~ai4ia_api.agents.mcp_execution.HealthReporter`. Mutates the
        shared record's health fields (powering the same quarantine circuit breaker
        BYO uses) but performs no persistence — official records are in-memory.
        Best-effort: never raises, so a tool call's own result is unaffected.
        """
        try:
            if ok:
                mcp_health.record_success(server)
            else:
                mcp_health.record_failure(server, error)
        except Exception:  # noqa: BLE001 - health must never break a turn
            logger.warning("official mcp record_health failed", exc_info=True)

    # --- Discovery -----------------------------------------------------------

    def _pending(self, now: float) -> list[UserMcpServer]:
        """Servers not yet discovered whose retry window has elapsed."""
        out: list[UserMcpServer] = []
        for server in self._servers:
            if server.name in self._discovered_ok:
                continue
            last = self._last_attempt.get(server.name, 0.0)
            if now - last >= self._retry_interval_s:
                out.append(server)
        return out

    async def _discover_many(self, servers: list[UserMcpServer]) -> None:
        for server in servers:
            self._last_attempt[server.name] = time.monotonic()
            try:
                tools = await self._connector.discover(
                    endpoint=server.endpoint, auth=self._auth
                )
            except Exception as exc:  # noqa: BLE001 - a bad server must not break the app
                logger.warning(
                    "official mcp discovery failed for %s", server.name, exc_info=True
                )
                mcp_health.record_failure(server, exc)
                continue
            server.discoveredTools = tools
            server.lastConnectedAt = _now()
            server.lastError = None
            mcp_health.record_success(server)
            self._discovered_ok.add(server.name)
