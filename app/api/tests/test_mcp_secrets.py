"""Tests for the durable MCP secret store (Phase 12B Increment A).

Covers the in-memory store, the Key-Vault-safe reference generator, the factory's
store selection (+ the loud out-of-local warning), and the Key Vault store driven
by an injected fake async ``SecretClient`` (so nothing touches a live vault).
"""
from __future__ import annotations

import re

from ai4ia_api.agents.mcp_secrets import (
    InMemoryMcpSecretStore,
    KeyVaultMcpSecretStore,
    build_mcp_secret_store,
    new_secret_ref,
)
from tests.conftest import make_settings


# --- Reference generator -----------------------------------------------------


def test_new_secret_ref_is_key_vault_safe():
    ref = new_secret_ref()
    # Key Vault secret names: ^[0-9a-zA-Z-]+$, max 127 chars.
    assert re.fullmatch(r"[0-9a-zA-Z-]+", ref)
    assert ref.startswith("mcp-")
    assert len(ref) <= 127


def test_new_secret_ref_is_unique():
    assert new_secret_ref() != new_secret_ref()


# --- In-memory store ---------------------------------------------------------


async def test_in_memory_roundtrip_and_delete():
    store = InMemoryMcpSecretStore()
    assert await store.get_secret("missing") is None
    await store.set_secret("ref1", "s3cr3t")
    assert await store.get_secret("ref1") == "s3cr3t"
    await store.delete_secret("ref1")
    assert await store.get_secret("ref1") is None
    # Deleting a missing ref is a no-op.
    await store.delete_secret("ref1")
    await store.close()


# --- Key Vault store (injected fake client) ----------------------------------


class _FakeSecret:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeKvClient:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.closed = False

    async def set_secret(self, name: str, value: str):
        self.secrets[name] = value
        return _FakeSecret(value)

    async def get_secret(self, name: str):
        from azure.core.exceptions import ResourceNotFoundError

        if name not in self.secrets:
            raise ResourceNotFoundError("not found")
        return _FakeSecret(self.secrets[name])

    async def delete_secret(self, name: str):
        from azure.core.exceptions import ResourceNotFoundError

        if name not in self.secrets:
            raise ResourceNotFoundError("not found")
        del self.secrets[name]

    async def close(self) -> None:
        self.closed = True


class _FakeCredential:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _kv_store(client: _FakeKvClient) -> KeyVaultMcpSecretStore:
    return KeyVaultMcpSecretStore("https://vault.example.net/", client=client)


async def test_keyvault_set_get_delete():
    client = _FakeKvClient()
    store = _kv_store(client)
    await store.set_secret("mcp-abc", "v1")
    assert client.secrets == {"mcp-abc": "v1"}
    assert await store.get_secret("mcp-abc") == "v1"
    await store.delete_secret("mcp-abc")
    assert "mcp-abc" not in client.secrets


async def test_keyvault_get_missing_returns_none():
    store = _kv_store(_FakeKvClient())
    assert await store.get_secret("nope") is None


async def test_keyvault_delete_missing_is_noop():
    store = _kv_store(_FakeKvClient())
    await store.delete_secret("nope")  # must not raise


async def test_keyvault_delete_swallows_unexpected_errors():
    class _Boom(_FakeKvClient):
        async def delete_secret(self, name: str):
            raise RuntimeError("kv down")

    store = _kv_store(_Boom())
    # Best-effort cleanup: an unexpected error is logged, not raised.
    await store.delete_secret("mcp-abc")


async def test_keyvault_does_not_close_injected_client():
    client = _FakeKvClient()
    store = _kv_store(client)
    await store.close()
    assert client.closed is False  # caller owns an injected client


async def test_keyvault_closes_owned_client_and_credential():
    store = KeyVaultMcpSecretStore("https://vault.example.net/")
    client = _FakeKvClient()
    cred = _FakeCredential()
    store._client = client
    store._owns_client = True
    store._credential = cred
    store._owns_credential = True
    await store.close()
    assert client.closed is True
    assert cred.closed is True


# --- Factory -----------------------------------------------------------------


def test_factory_uses_keyvault_when_uri_set():
    settings = make_settings(
        custom_tools_secret_vault_uri="https://vault.example.net/"
    )
    assert isinstance(build_mcp_secret_store(settings), KeyVaultMcpSecretStore)


def test_factory_uses_in_memory_locally_without_warning(caplog):
    with caplog.at_level("WARNING"):
        store = build_mcp_secret_store(make_settings(env="local"))
    assert isinstance(store, InMemoryMcpSecretStore)
    assert "in-memory secret store" not in caplog.text


def test_factory_warns_for_in_memory_outside_local(caplog):
    with caplog.at_level("WARNING"):
        store = build_mcp_secret_store(
            make_settings(env="dev", session_store="cosmos")
        )
    assert isinstance(store, InMemoryMcpSecretStore)
    assert "in-memory secret store" in caplog.text
