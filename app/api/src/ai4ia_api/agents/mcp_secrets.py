"""Durable secret storage for user-registered MCP servers.

MCP connection secrets can be supplied transiently: a credential is supplied on
each request, used to connect, and never stored. That is fine for discovery but
cannot back **per-turn execution** (a chat turn can't re-prompt the user for an
API key). This module adds a durable secret seam so an authenticated server's
credential is persisted once and resolved at connect/execute time.

The owner chose **Azure Key Vault** (keyless, managed-identity) over an encrypted
Cosmos field. Only an opaque *reference* (the Key Vault secret name) is stored on
the :class:`~ai4ia_api.agents.mcp_servers.UserMcpServer` record; the secret value
itself never lives in Cosmos. A narrow :class:`McpSecretStore` Protocol keeps the
service storage-agnostic: an in-memory store backs local/dev + unit tests, and
:class:`KeyVaultMcpSecretStore` (lazy SDK import) backs deployments.

The reference is a fresh random name (``mcp-<uuid4>``) generated the first time a
server's secret is persisted, then reused for in-place rotation. Using a fresh
name per registration (rather than a deterministic ``hash(user, name)``) sidesteps
Key Vault soft-delete name reuse: deleting a server and re-registering one with the
same display name never collides with the prior, soft-deleted secret.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol, runtime_checkable

from ..config import Environment, Settings

logger = logging.getLogger(__name__)


def new_secret_ref() -> str:
    """A fresh, Key-Vault-safe secret name (``^[0-9a-zA-Z-]+$``, well under 127)."""
    return f"mcp-{uuid.uuid4().hex}"


@runtime_checkable
class McpSecretStore(Protocol):
    async def set_secret(self, ref: str, secret: str) -> None: ...

    async def get_secret(self, ref: str) -> str | None: ...

    async def delete_secret(self, ref: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryMcpSecretStore:
    """Process-local secret store for local/dev + tests (not durable)."""

    def __init__(self) -> None:
        self._by_ref: dict[str, str] = {}

    async def set_secret(self, ref: str, secret: str) -> None:
        self._by_ref[ref] = secret

    async def get_secret(self, ref: str) -> str | None:
        return self._by_ref.get(ref)

    async def delete_secret(self, ref: str) -> None:
        self._by_ref.pop(ref, None)

    async def close(self) -> None:
        return None


class KeyVaultMcpSecretStore:
    """Durable secret store on an Azure Key Vault (AAD; no keys/connection strings).

    ``SecretClient`` + ``DefaultAzureCredential`` are imported lazily so the app
    and tests run without the ``azure-keyvault-secrets`` package or a live vault.
    A missing secret resolves to ``None``; a delete on a missing secret is a no-op
    (best-effort), so re-registration and idempotent cleanup never raise.
    """

    def __init__(
        self,
        vault_uri: str,
        *,
        client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self._vault_uri = vault_uri
        self._client = client
        self._owns_client = client is None
        self._credential = credential
        self._owns_credential = credential is None

    def _secret_client(self) -> Any:
        if self._client is None:
            from azure.identity.aio import DefaultAzureCredential
            from azure.keyvault.secrets.aio import SecretClient

            if self._credential is None:
                self._credential = DefaultAzureCredential()
            self._client = SecretClient(
                vault_url=self._vault_uri, credential=self._credential
            )
        return self._client

    async def set_secret(self, ref: str, secret: str) -> None:
        await self._secret_client().set_secret(ref, secret)

    async def get_secret(self, ref: str) -> str | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            kv_secret = await self._secret_client().get_secret(ref)
        except ResourceNotFoundError:
            return None
        return kv_secret.value

    async def delete_secret(self, ref: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            await self._secret_client().delete_secret(ref)
        except ResourceNotFoundError:
            return None
        except Exception:  # noqa: BLE001 - cleanup is best-effort, never fatal
            # Do not interpolate ``ref`` (a Key Vault secret reference) into the log:
            # it is flagged by CodeQL py/clear-text-logging-sensitive-data. The
            # traceback is enough to diagnose a failed best-effort cleanup.
            logger.warning("mcp secret delete failed", exc_info=True)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.close()
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()


def build_mcp_secret_store(settings: Settings) -> McpSecretStore:
    """Pick the Key Vault store when a vault URI is configured, else in-memory.

    Mirrors the other durable-store factories: a loud warning is emitted when the
    in-memory store is selected outside ``local`` (registered secrets would not
    survive a restart, so authenticated servers would break after a rollover).
    """
    if settings.custom_tools_secret_vault_uri:
        return KeyVaultMcpSecretStore(settings.custom_tools_secret_vault_uri)
    if settings.env != Environment.local:
        logger.warning(
            "mcp secrets: using the in-memory secret store outside local; "
            "registered MCP credentials will not survive a restart or rollover."
        )
    return InMemoryMcpSecretStore()
