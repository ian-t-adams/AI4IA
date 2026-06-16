"""McpServerService (Phase 12A): per-user MCP-server CRUD + discovery.

Owns validation, the SSRF egress check on every endpoint, tool discovery via an
injected :class:`~ai4ia_api.agents.mcp_client.McpConnector`, the per-user cap,
and the projection of a saved server's cached tools onto the governance seam.

Secrets are durable (Phase 12B): an authenticated server's credential is supplied
on the create/update/test request, used to connect, and — on success — persisted
to an injected :class:`~ai4ia_api.agents.mcp_secrets.McpSecretStore` (Azure Key
Vault in deployments). Only an opaque ``secretRef`` is stored on the record; the
value is resolved from the store at connect/execute time. An update may reuse the
stored secret without re-entering it; a delete best-effort removes it.
"""
from __future__ import annotations

import logging

from .mcp_client import McpAuth, McpConnector
from .mcp_secrets import McpSecretStore, new_secret_ref
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
        secret_store: McpSecretStore,
        max_servers: int = MAX_MCP_SERVERS_PER_USER,
        resolver: Resolver | None = None,
    ) -> None:
        self._store = store
        self._connector = connector
        self._secret_store = secret_store
        self._max_servers = max_servers
        self._resolver = resolver

    async def close(self) -> None:
        await self._store.close()
        await self._secret_store.close()

    @property
    def connector(self) -> McpConnector:
        """The MCP connector (shared by discovery and per-turn execution)."""
        return self._connector

    @property
    def resolver(self) -> Resolver | None:
        """The DNS resolver used for SSRF egress validation (``None`` = system).

        Exposed so the per-turn execution path can re-validate a server's host at
        call time with the *same* resolver discovery used (DNS-rebinding defense).
        """
        return self._resolver

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

        # Persist the secret durably only after a successful connect
        # (fail-fast-persist-nothing: a server we could not reach is never saved,
        # so its secret is never stored either).
        secret_ref: str | None = None
        if req.authMode is not McpAuthMode.none and req.secret:
            secret_ref = new_secret_ref()
            await self._secret_store.set_secret(secret_ref, req.secret)

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
            secretRef=secret_ref,
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

        # An authenticated server may be updated without re-entering its secret:
        # we reuse the durably stored one. A freshly supplied secret rotates it;
        # switching to ``authMode=none`` drops it.
        reuse_stored = (
            req.authMode is not McpAuthMode.none
            and not (req.secret and req.secret.strip())
            and current.secretRef is not None
        )
        display, desc, host, endpoint = self._validate_fields(
            name=current.name,
            display_name=req.displayName,
            description=req.description,
            endpoint=req.endpoint,
            auth_mode=req.authMode,
            secret=req.secret,
            secret_optional=reuse_stored,
        )

        if req.authMode is McpAuthMode.none:
            connect_secret: str | None = None
        elif req.secret and req.secret.strip():
            connect_secret = req.secret
        else:  # reuse the durably stored secret to re-connect
            connect_secret = (
                await self._secret_store.get_secret(current.secretRef)
                if current.secretRef
                else None
            )
            if not connect_secret:
                raise McpValidationError(
                    f"A secret is required for '{req.authMode.value}' auth."
                )
        auth = McpAuth(mode=req.authMode, secret=connect_secret)
        tools = await self._discover(endpoint, auth)

        # Persist/rotate/clear the durable secret only after a successful connect.
        secret_ref = current.secretRef
        if req.authMode is McpAuthMode.none:
            if current.secretRef:
                await self._secret_store.delete_secret(current.secretRef)
            secret_ref = None
        elif req.secret and req.secret.strip():
            secret_ref = current.secretRef or new_secret_ref()
            await self._secret_store.set_secret(secret_ref, req.secret)

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
            secretRef=secret_ref,
            discoveredTools=tools,
            createdAt=current.createdAt,
            updatedAt=now,
            lastConnectedAt=now,
            lastError=None,
        )
        await self._store.put(server)
        return server

    async def delete(self, user_id: str, name: str) -> None:
        key = _norm(name)
        current = await self._store.get(user_id, key)
        if current is None:
            return
        if current.secretRef:
            await self._secret_store.delete_secret(current.secretRef)
        await self._store.delete(user_id, key)

    async def test(
        self, user_id: str, name: str, secret: str | None = None
    ) -> UserMcpServer:
        """Re-connect to a saved server and refresh its cached tools.

        An authenticated server may re-use its durably stored secret (no need to
        re-supply it); an explicit ``secret`` overrides the stored one. On failure
        the server's ``lastError`` is recorded before raising so the management
        view can show why it is unhealthy.
        """
        current = await self.get(user_id, name)
        connect_secret = secret
        if current.authMode is not McpAuthMode.none:
            if not (connect_secret and connect_secret.strip()) and current.secretRef:
                connect_secret = await self._secret_store.get_secret(current.secretRef)
            self._validate_secret(current.authMode, connect_secret)
        auth = McpAuth(mode=current.authMode, secret=connect_secret)
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

    async def secret_for(self, server: UserMcpServer) -> str | None:
        """Resolve a server's durable connection secret (``None`` if public).

        Shared by per-turn execution (Phase 12B Increment B) so a tool call can
        connect with the same credential discovery used.
        """
        if server.authMode is McpAuthMode.none or not server.secretRef:
            return None
        return await self._secret_store.get_secret(server.secretRef)

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
        secret_optional: bool = False,
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
        self._validate_secret(auth_mode, secret, optional=secret_optional)
        return display, desc, host, endpoint

    def _validate_endpoint(self, endpoint: str) -> str:
        try:
            return validate_public_https_url(endpoint, resolver=self._resolver)
        except SsrfError as exc:
            raise McpValidationError(str(exc)) from exc

    @staticmethod
    def _validate_secret(
        auth_mode: McpAuthMode, secret: str | None, *, optional: bool = False
    ) -> None:
        if auth_mode is McpAuthMode.none:
            return
        if not secret or not secret.strip():
            if optional:
                return
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
