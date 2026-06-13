"""McpServerService (Phase 12A): per-user MCP-server CRUD + discovery.

Owns validation, the SSRF egress check on every endpoint, tool discovery via an
injected :class:`~ai4ia_api.agents.mcp_client.McpConnector`, the per-user cap,
and the projection of a saved server's cached tools onto the governance seam.

Secrets are transient: an authenticated server's credential is supplied on the
create/update/test request, used only to connect, and never stored on the
record. Durable (Key-Vault-backed) secrets + per-turn execution are a later
sub-phase; this service stops at "register a server and cache what it offers."
"""
from __future__ import annotations

import logging

from .mcp_client import McpAuth, McpConnector
from .mcp_servers import (
    MAX_DESCRIPTION_LEN,
    MAX_DISPLAY_NAME_LEN,
    MAX_ENDPOINT_LEN,
    MAX_MCP_SERVERS_PER_USER,
    MAX_SECRET_LEN,
    NAME_RE,
    McpAuthMode,
    McpConflictError,
    McpConnectionError,
    McpNotFoundError,
    McpServerError,
    McpValidationError,
    UserMcpServer,
    UserMcpServerCreate,
    UserMcpServerUpdate,
    _now,
)
from .mcp_store import UserMcpServerStore
from .ssrf import Resolver, SsrfError, validate_public_https_url

logger = logging.getLogger(__name__)


class McpServerService:
    def __init__(
        self,
        store: UserMcpServerStore,
        *,
        connector: McpConnector,
        max_servers: int = MAX_MCP_SERVERS_PER_USER,
        resolver: Resolver | None = None,
    ) -> None:
        self._store = store
        self._connector = connector
        self._max_servers = max_servers
        self._resolver = resolver

    async def close(self) -> None:
        await self._store.close()

    # --- Reads ----------------------------------------------------------------

    async def list_for(self, user_id: str) -> list[UserMcpServer]:
        return await self._store.list(user_id)

    async def get(self, user_id: str, name: str) -> UserMcpServer:
        key = _norm(name)
        server = await self._store.get(user_id, key)
        if server is None:
            raise McpNotFoundError(key)
        return server

    # --- Mutations ------------------------------------------------------------

    async def create(self, user_id: str, req: UserMcpServerCreate) -> UserMcpServer:
        name = _norm(req.name)
        self._validate_name(name)
        if await self._store.get(user_id, name) is not None:
            raise McpConflictError(f"You already have an MCP server named '{name}'.")
        existing = await self._store.list(user_id)
        if len(existing) >= self._max_servers:
            raise McpConflictError(
                f"You have reached the maximum of {self._max_servers} MCP servers."
            )

        display, desc, host, endpoint = self._validate_fields(
            name=name,
            display_name=req.displayName,
            description=req.description,
            endpoint=req.endpoint,
            auth_mode=req.authMode,
            secret=req.secret,
        )
        auth = McpAuth(mode=req.authMode, secret=req.secret)
        tools = await self._discover(endpoint, auth)

        now = _now()
        server = UserMcpServer(
            id=name,
            userId=user_id,
            name=name,
            displayName=display,
            description=desc,
            endpoint=endpoint,
            host=host,
            authMode=req.authMode,
            trusted=bool(req.trusted),
            enabled=bool(req.enabled),
            discoveredTools=tools,
            createdAt=now,
            updatedAt=now,
            lastConnectedAt=now,
            lastError=None,
        )
        await self._store.put(server)
        return server

    async def update(
        self, user_id: str, name: str, req: UserMcpServerUpdate
    ) -> UserMcpServer:
        key = _norm(name)
        current = await self._store.get(user_id, key)
        if current is None:
            raise McpNotFoundError(key)

        display, desc, host, endpoint = self._validate_fields(
            name=current.name,
            display_name=req.displayName,
            description=req.description,
            endpoint=req.endpoint,
            auth_mode=req.authMode,
            secret=req.secret,
        )
        auth = McpAuth(mode=req.authMode, secret=req.secret)
        tools = await self._discover(endpoint, auth)

        now = _now()
        server = UserMcpServer(
            id=current.name,
            userId=user_id,
            name=current.name,
            displayName=display,
            description=desc,
            endpoint=endpoint,
            host=host,
            authMode=req.authMode,
            trusted=bool(req.trusted),
            enabled=bool(req.enabled),
            discoveredTools=tools,
            createdAt=current.createdAt,
            updatedAt=now,
            lastConnectedAt=now,
            lastError=None,
        )
        await self._store.put(server)
        return server

    async def delete(self, user_id: str, name: str) -> None:
        await self._store.delete(user_id, _norm(name))

    async def test(
        self, user_id: str, name: str, secret: str | None = None
    ) -> UserMcpServer:
        """Re-connect to a saved server and refresh its cached tools.

        An authenticated server needs its secret re-supplied (we never stored
        it). On failure the server's ``lastError`` is recorded before raising so
        the management view can show why it is unhealthy.
        """
        current = await self.get(user_id, name)
        if current.authMode is not McpAuthMode.none:
            self._validate_secret(current.authMode, secret)
        auth = McpAuth(mode=current.authMode, secret=secret)
        try:
            tools = await self._discover(current.endpoint, auth)
        except McpConnectionError as exc:
            current.lastError = str(exc)
            current.updatedAt = _now()
            await self._store.put(current)
            raise
        current.discoveredTools = tools
        current.lastConnectedAt = _now()
        current.updatedAt = current.lastConnectedAt
        current.lastError = None
        await self._store.put(current)
        return current

    # --- Validation + discovery ----------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name:
            raise McpValidationError("MCP server name is required.")
        if not NAME_RE.match(name):
            raise McpValidationError(
                "MCP server name must be 1-32 chars, start with a letter, end "
                "alphanumeric, and contain only lowercase letters, digits, '_', "
                "'.', or '-'."
            )

    def _validate_fields(
        self,
        *,
        name: str,
        display_name: str | None,
        description: str,
        endpoint: str,
        auth_mode: McpAuthMode,
        secret: str | None,
    ) -> tuple[str, str, str, str]:
        display = (display_name or name).strip() or name
        if len(display) > MAX_DISPLAY_NAME_LEN:
            raise McpValidationError(
                f"Display name must be at most {MAX_DISPLAY_NAME_LEN} characters."
            )
        desc = (description or "").strip()
        if len(desc) > MAX_DESCRIPTION_LEN:
            raise McpValidationError(
                f"Description must be at most {MAX_DESCRIPTION_LEN} characters."
            )
        endpoint = (endpoint or "").strip()
        if not endpoint:
            raise McpValidationError("Endpoint URL is required.")
        if len(endpoint) > MAX_ENDPOINT_LEN:
            raise McpValidationError("Endpoint URL is too long.")
        host = self._validate_endpoint(endpoint)
        self._validate_secret(auth_mode, secret)
        return display, desc, host, endpoint

    def _validate_endpoint(self, endpoint: str) -> str:
        try:
            return validate_public_https_url(endpoint, resolver=self._resolver)
        except SsrfError as exc:
            raise McpValidationError(str(exc)) from exc

    @staticmethod
    def _validate_secret(auth_mode: McpAuthMode, secret: str | None) -> None:
        if auth_mode is McpAuthMode.none:
            return
        if not secret or not secret.strip():
            raise McpValidationError(
                f"A secret is required for '{auth_mode.value}' auth."
            )
        if len(secret) > MAX_SECRET_LEN:
            raise McpValidationError("Secret is too long.")

    async def _discover(self, endpoint: str, auth: McpAuth):
        try:
            return await self._connector.discover(endpoint=endpoint, auth=auth)
        except McpServerError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize any transport error
            logger.warning("mcp discovery failed", exc_info=True)
            raise McpConnectionError(f"Could not connect to the MCP server: {exc}") from exc


def _norm(name: str) -> str:
    return (name or "").strip().lower()
