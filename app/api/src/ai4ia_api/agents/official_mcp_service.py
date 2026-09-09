"""Curated "official" MCP plane — servers reached through the MCP APIM front door.

This is the official-server analogue of :class:`~ai4ia_api.agents.mcp_service.
McpServerService`. Where that service owns a *per-user* registry of BYO remote
MCP servers (called directly behind the SSRF guard, with per-user Key Vault
secrets), this one owns a small, *admin-curated* set of servers reached
**through the shared active APIM gateway** (service: ``apimcore.bicep``;
MCP children: ``mcpgateway.bicep``)
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
  (curated for discovery/attachment, not invocation approval), ``host`` = the
  APIM gateway host (so the projected tools' egress allowlist is scoped to APIM),
  and ``endpoint`` = ``<gateway_url>/<path>``. Interactive external/destructive
  invocations still pass through the exact-argument approval policy used by BYO
  tools. No per-server secret is stored — the subscription key is app-global and
  supplied by :meth:`secret_for`.
* **Lazy, cached discovery.** There is no registration step, so tools and
  explicitly enabled MCP resources are discovered the first time the plane is
  used and cached on the in-memory records. A server that fails tool discovery
  contributes **zero** tools; a resource-only failure leaves its tools available
  and retries later. Resource-enabled servers refresh periodically so a toolbox
  reconciled just after an app deploy becomes visible without restarting; calls
  between refreshes remain lock-free and network-free.
* **Default-OFF / empty.** With the feature flag off (the default) this service
  is never constructed; with an empty catalog (also the default) ``list_all``
  returns ``[]`` and nothing is wired into a turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from urllib.parse import urlparse

from . import mcp_health
from .mcp_health import is_quarantined
from .mcp_client import McpAuth, McpConnector, McpResourceResult
from .mcp_protocol import McpRequestContext
from .mcp_servers import (
    McpAuthMode, McpConnectionError, McpProtocolVersion, McpTransport, UserMcpServer,
    _now,
)
from .ssrf import Resolver, async_validate_public_https_url
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
DEFAULT_RESOURCE_REFRESH_INTERVAL_S = 300.0


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
                protocolVersion=entry.protocolVersion,
                authMode=McpAuthMode.apim_subscription,
                # Curated/admin-vetted for discovery. Interactive invocation
                # approval remains independent and covers both MCP planes.
                trusted=True,
                enabled=True,
                # No per-server secret: the APIM subscription key is app-global
                # and supplied by ``secret_for`` at call time.
                secretRef=None,
                resourcesEnabled=entry.resourcesEnabled,
                # Replicas/restarts must agree on consent identity. Discovery
                # timestamps and per-process randomness are not configuration.
                configurationRevision=hashlib.sha256(json.dumps(
                    {"endpoint": f"{base}/{path}", "catalog": entry.model_dump(mode="json")},
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest(),
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
        resource_refresh_interval_s: float = DEFAULT_RESOURCE_REFRESH_INTERVAL_S,
    ) -> None:
        self._connector = connector
        self._resolver = resolver
        self._subscription_key = subscription_key
        self._retry_interval_s = retry_interval_s
        self._resource_refresh_interval_s = resource_refresh_interval_s
        # The app-global credential presented to APIM on every official call.
        self._auth_fingerprint = hashlib.sha256(subscription_key.encode()).digest()
        # Built once; the SAME instances are reused across turns so in-memory
        # health/quarantine + discovered-tool caches persist for the process.
        self._servers = build_official_servers(catalog, gateway_url=gateway_url)
        self._discovered_ok: set[str] = set()
        self._last_attempt: dict[str, float] = {}
        self._last_success: dict[str, float] = {}
        self._discovery_contexts: dict[str, McpRequestContext] = {}
        self._discovery_generation = 0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """No persistent resources to release.

        The connector creates a short-lived, IP-pinned client per call (it owns no
        pooled connection of its own), and the records are in-memory. Present for
        lifecycle symmetry with :class:`McpServerService`.
        """
        return None

    @property
    def _auth(self) -> McpAuth:
        return McpAuth(mode=McpAuthMode.apim_subscription, secret=self._subscription_key)

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
        # Backoff says when to try again, not whether an old snapshot is fresh.
        # A refresh already owns that decision; readers must join it first.
        if self._lock.locked():
            async with self._lock:
                pass
        fingerprint = hashlib.sha256(self._subscription_key.encode()).digest()
        if fingerprint != self._auth_fingerprint:
            self.refresh()
            self._auth_fingerprint = fingerprint
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
        self._discovery_generation += 1
        self._discovered_ok.clear()
        self._last_attempt.clear()
        self._last_success.clear()
        self._discovery_contexts.clear()
        for server in self._servers:
            self._connector.invalidate(McpRequestContext.for_server(server))
            server.discoveredTools = []
            server.discoveredResources = []

    async def secret_for(self, server: UserMcpServer) -> str | None:
        """Resolve the credential for an official server — the app-global APIM key.

        A :class:`~ai4ia_api.agents.mcp_execution.SecretResolver`. The key is the
        same for every official server (it authenticates to the one MCP APIM front
        door), so the per-server record is irrelevant.
        """
        return self._subscription_key

    async def read_resource(
        self, server: UserMcpServer, uri: str
    ) -> McpResourceResult:
        """Read one resource that this curated server previously advertised.

        Both the catalog opt-in and the discovery membership check are enforced at
        execution time so a caller cannot turn the official bridge into a generic
        MCP resource fetch primitive.
        """
        if self._lock.locked():
            async with self._lock:
                pass
        if (
            not any(candidate is server for candidate in self._servers)
            or not server.enabled
            or not server.resourcesEnabled
        ):
            raise ValueError("MCP resources are not enabled for this official server.")
        if is_quarantined(server):
            raise ValueError("MCP resource server is quarantined.")
        if uri not in {resource.uri for resource in server.discoveredResources}:
            raise ValueError("MCP resource was not advertised by this official server.")

        await async_validate_public_https_url(
            server.endpoint,
            resolver=self._resolver,
        )
        return await self._connector.read_resource(
            endpoint=server.endpoint,
            auth=self._auth,
            uri=uri,
            context=McpRequestContext.for_server(server),
        )

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
            if not server.enabled:
                continue
            if is_quarantined(server):
                continue
            context = McpRequestContext.for_server(server)
            if (
                server.name in self._discovery_contexts
                and self._discovery_contexts[server.name] != context
            ):
                self._connector.invalidate(context)
                self._discovered_ok.discard(server.name)
                self._last_attempt.pop(server.name, None)
                server.discoveredTools = []
                server.discoveredResources = []
            if server.name in self._discovered_ok:
                # The connector owns modern TTL freshness. Do not turn a
                # short-lived private hint into this service's legacy cache.
                if server.protocolVersion is McpProtocolVersion.stateless:
                    out.append(server)
                    continue
                last_success = self._last_success.get(server.name, 0.0)
                if (
                    not server.resourcesEnabled
                    or now - last_success < self._resource_refresh_interval_s
                ):
                    continue
                out.append(server)
                continue
            last = self._last_attempt.get(server.name, 0.0)
            if now - last >= self._retry_interval_s:
                out.append(server)
        return out

    async def _discover_many(self, servers: list[UserMcpServer]) -> None:
        for server in servers:
            try:
                await self._discover_one(server)
            except asyncio.CancelledError:
                self._clear_discovery(server, "MCP discovery was cancelled.")
                self._connector.invalidate(McpRequestContext.for_server(server))
                raise

    def _clear_discovery(self, server: UserMcpServer, detail: str) -> None:
        self._discovered_ok.discard(server.name)
        server.discoveredTools = []
        server.discoveredResources = []
        server.lastError = detail

    async def _discover_one(self, server: UserMcpServer) -> None:
        context = McpRequestContext.for_server(server)
        endpoint, auth = server.endpoint, self._auth
        resources_enabled = server.resourcesEnabled
        generation = self._discovery_generation
        self._discovery_contexts[server.name] = context
        self._last_attempt[server.name] = time.monotonic()
        self._discovered_ok.discard(server.name)

        def still_current() -> bool:
            return (
                generation == self._discovery_generation
                and context == McpRequestContext.for_server(server)
                and auth == self._auth
            )

        def discard_changed() -> None:
            self._clear_discovery(server, "MCP configuration changed during discovery.")
            self._connector.invalidate(context)
            self._last_attempt.pop(server.name, None)

        try:
            await async_validate_public_https_url(endpoint, resolver=self._resolver)
            if not still_current():
                discard_changed()
                return
            if context.protocol_version is McpProtocolVersion.stateless:
                description = await self._connector.discover_server(
                    endpoint=endpoint, auth=auth, context=context
                )
                if not still_current():
                    discard_changed()
                    return
                if "tools" not in description.capabilities or (
                    resources_enabled and "resources" not in description.capabilities
                ):
                    raise McpConnectionError(
                        "server/discover: configured server capabilities are unavailable."
                    )
            tools = await self._connector.discover(endpoint=endpoint, auth=auth, context=context)
        except Exception as exc:  # noqa: BLE001 - a bad server must not break the app
            if not still_current():
                discard_changed()
                return
            logger.warning(
                "official mcp discovery failed for %s category=%s",
                server.name, mcp_health.summarize_error(exc),
            )
            self._clear_discovery(server, mcp_health.summarize_error(exc))
            mcp_health.record_failure(server, exc)
            return
        if not still_current():
            discard_changed()
            return
        resources = []
        resources_ok = True
        last_error = None
        if resources_enabled:
            try:
                resources = await self._connector.list_resources(
                    endpoint=endpoint, auth=auth, context=context,
                )
            except Exception as exc:  # noqa: BLE001 - resources are additive to tools
                logger.warning(
                    "official mcp resource discovery failed for %s category=%s",
                    server.name, mcp_health.summarize_error(exc),
                )
                last_error = mcp_health.summarize_error(exc)
                resources_ok = False
        if not still_current():
            discard_changed()
            return
        # Publish the snapshot together, never a partly refreshed tool/resource
        # pair or a response belonging to invalidated configuration/credentials.
        server.discoveredTools = tools
        server.discoveredResources = resources
        server.lastError = last_error
        server.lastConnectedAt = _now()
        mcp_health.record_success(server)
        if resources_ok:
            self._discovered_ok.add(server.name)
            self._last_success[server.name] = time.monotonic()
