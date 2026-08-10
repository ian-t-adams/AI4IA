"""Tests for the MCP-server models, governance mapping, and service.

Uses the in-memory store + the deterministic ``FakeMcpConnector`` + a stub
resolver so nothing touches real DNS or a live server.
"""
from __future__ import annotations

import pytest

from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpAuth
from ai4ia_api.agents.mcp_secrets import InMemoryMcpSecretStore
from ai4ia_api.agents.mcp_health import QUARANTINE_THRESHOLD, is_quarantined
from ai4ia_api.agents.mcp_servers import (
    MAX_MCP_SERVERS_PER_USER,
    DiscoveredTool,
    McpAuthMode,
    McpConflictError,
    McpConnectionError,
    McpNotFoundError,
    McpToolApproval,
    McpValidationError,
    UserMcpServer,
    UserMcpServerCreate,
    UserMcpServerUpdate,
    discovered_tool_to_spec,
    is_valid_remote_tool_name,
    namespaced_tool_name,
    tool_alias,
)
from ai4ia_api.agents.mcp_store import (
    CosmosUserMcpServerStore,
    InMemoryUserMcpServerStore,
)
from ai4ia_api.agents.mcp_service import McpServerService
from ai4ia_api.agents.tools import ToolRisk

_PUBLIC_RESOLVER = lambda _host: ["93.184.216.34"]  # noqa: E731 - terse test stub
_TOOLS = [
    DiscoveredTool(name="get_forecast", description="Weather forecast"),
    DiscoveredTool(name="get_alerts", description="Severe weather alerts"),
]


def _service(
    *,
    tools=None,
    error=None,
    resolver=None,
    max_servers=MAX_MCP_SERVERS_PER_USER,
    secret_store=None,
):
    return McpServerService(
        InMemoryUserMcpServerStore(),
        connector=FakeMcpConnector(tools if tools is not None else list(_TOOLS), error=error),
        secret_store=secret_store or InMemoryMcpSecretStore(),
        max_servers=max_servers,
        resolver=resolver or _PUBLIC_RESOLVER,
    )


def _create(name="weather", **over):
    body = dict(name=name, endpoint="https://mcp.example.com/rpc")
    body.update(over)
    return UserMcpServerCreate(**body)


# --- Governance mapping (pure) -----------------------------------------------


def test_namespaced_tool_name():
    assert namespaced_tool_name("weather", "get_forecast") == "mcp:weather/get_forecast"


# --- Provider-safe tool alias (deterministic, collision-resistant) -----------


def test_tool_alias_is_deterministic():
    assert tool_alias("weather", "get_forecast") == tool_alias("weather", "get_forecast")


def test_tool_alias_differs_for_different_tools_on_same_server():
    assert tool_alias("weather", "get_forecast") != tool_alias("weather", "get_alerts")


def test_tool_alias_differs_for_same_tool_on_different_servers():
    assert tool_alias("weather", "get_forecast") != tool_alias("radar", "get_forecast")


@pytest.mark.parametrize(
    "server_name,tool_name",
    [
        ("weather", "get_forecast"),
        ("weather", "Get.The Forecast/Now"),  # dots, spaces, slashes: not control chars
        ("weather", "获取天气"),  # unicode
        ("weather", "a" * 500),  # pathological length
        ("weather", "\ud800"),  # aliases stay safe even for rejected legacy data
        ("my-server.v2", "tool"),
        ("s", ""),  # degenerate empty tool name
    ],
)
def test_tool_alias_is_always_provider_safe(server_name, tool_name):
    from ai4ia_api.agents.tools import is_safe_tool_name

    alias = tool_alias(server_name, tool_name)
    assert is_safe_tool_name(alias)
    assert 1 <= len(alias) <= 64
    assert alias.startswith("mcp_")


def test_tool_alias_never_contains_raw_tool_name_content():
    # A raw tool name embedding something log-hostile must never survive into the
    # alias verbatim -- the alias is a fixed-charset hash-derived identifier.
    hostile = "forecast\nSpoofedEvent=admin_login outcome=ok"
    alias = tool_alias("weather", hostile)
    assert "\n" in hostile
    assert "\n" not in alias
    assert "SpoofedEvent" not in alias
    from ai4ia_api.agents.tools import is_safe_tool_name

    assert is_safe_tool_name(alias)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "bad\nname",
        "bad\u200bformat",
        "\ud800",
        "x" * 129,
    ],
)
def test_invalid_remote_tool_names_are_rejected(name):
    assert not is_valid_remote_tool_name(name)


@pytest.mark.parametrize(
    "name",
    ["weather.get", "namespace/tool", "获取天气", " tool with spaces "],
)
def test_valid_remote_tool_names_are_preserved(name):
    assert is_valid_remote_tool_name(name)


def test_discovered_tool_to_spec_untrusted_requires_approval():
    server = UserMcpServer(
        id="weather",
        userId="u1",
        name="weather",
        displayName="Weather",
        endpoint="https://mcp.example.com/rpc",
        host="mcp.example.com",
        trusted=False,
        enabled=True,
        discoveredTools=list(_TOOLS),
    )
    spec = discovered_tool_to_spec(server, _TOOLS[0])
    assert spec.name == "mcp:weather/get_forecast"
    assert spec.risk is ToolRisk.external
    assert spec.requires_approval is True
    assert spec.egress_allowlist == frozenset({"mcp.example.com"})
    assert spec.enabled is True


def test_discovered_tool_to_spec_trusted_skips_approval_and_disabled_propagates():
    server = UserMcpServer(
        id="weather",
        userId="u1",
        name="weather",
        displayName="Weather",
        endpoint="https://mcp.example.com/rpc",
        host="mcp.example.com",
        trusted=True,
        enabled=False,
        discoveredTools=list(_TOOLS),
    )
    specs = server.tool_specs()
    assert len(specs) == 2
    assert all(s.requires_approval is False for s in specs)
    assert all(s.enabled is False for s in specs)


# --- Create ------------------------------------------------------------------


async def test_create_persists_and_caches_tools():
    svc = _service()
    server = await svc.create("u1", _create(displayName="Weather"))
    assert server.name == "weather"
    assert server.host == "mcp.example.com"
    assert [t.name for t in server.discoveredTools] == ["get_forecast", "get_alerts"]
    assert server.lastConnectedAt is not None
    assert server.lastError is None
    # No secret field exists on the durable record at all.
    assert "secret" not in server.model_dump()

    again = await svc.get("u1", "weather")
    assert again.name == "weather"


async def test_create_normalizes_name_case():
    svc = _service()
    server = await svc.create("u1", _create(name="Weather"))
    assert server.name == "weather"


async def test_create_rejects_invalid_name():
    svc = _service()
    with pytest.raises(McpValidationError):
        await svc.create("u1", _create(name="1bad"))


async def test_create_rejects_duplicate():
    svc = _service()
    await svc.create("u1", _create())
    with pytest.raises(McpConflictError):
        await svc.create("u1", _create())


async def test_create_enforces_cap():
    svc = _service(max_servers=1)
    await svc.create("u1", _create(name="one"))
    with pytest.raises(McpConflictError):
        await svc.create("u1", _create(name="two"))


async def test_create_rejects_ssrf_endpoint():
    svc = _service(resolver=lambda _h: ["10.0.0.1"])
    with pytest.raises(McpValidationError):
        await svc.create("u1", _create())


async def test_create_requires_secret_for_authed_server():
    svc = _service()
    with pytest.raises(McpValidationError):
        await svc.create("u1", _create(authMode=McpAuthMode.bearer))


async def test_create_persists_secret_durably_not_on_record():
    connector = FakeMcpConnector(list(_TOOLS))
    secret_store = InMemoryMcpSecretStore()
    svc = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=connector,
        secret_store=secret_store,
        resolver=_PUBLIC_RESOLVER,
    )
    server = await svc.create(
        "u1", _create(authMode=McpAuthMode.api_key, secret="s3cr3t")
    )
    # The connector saw the secret transiently...
    assert connector.calls
    _endpoint, auth = connector.calls[-1]
    assert isinstance(auth, McpAuth)
    assert auth.secret == "s3cr3t"
    # ...the raw secret is nowhere on the persisted record...
    assert "s3cr3t" not in server.model_dump_json()
    assert server.authMode is McpAuthMode.api_key
    # ...but it is durably stored under an opaque reference and resolvable.
    assert server.secretRef
    assert await secret_store.get_secret(server.secretRef) == "s3cr3t"
    assert await svc.secret_for(server) == "s3cr3t"


async def test_create_public_server_stores_no_secret():
    secret_store = InMemoryMcpSecretStore()
    svc = _service(secret_store=secret_store)
    server = await svc.create("u1", _create())
    assert server.secretRef is None
    assert await svc.secret_for(server) is None


async def test_create_connection_failure_raises_and_persists_nothing():
    svc = _service(error=McpConnectionError("boom"))
    with pytest.raises(McpConnectionError):
        await svc.create("u1", _create())
    assert await svc.list_for("u1") == []


async def test_create_wraps_unexpected_connector_error():
    svc = _service(error=RuntimeError("kaboom"))
    with pytest.raises(McpConnectionError):
        await svc.create("u1", _create())


# --- Update / delete / list --------------------------------------------------


async def test_update_replaces_and_preserves_created_at():
    svc = _service()
    created = await svc.create("u1", _create(description="old"))
    updated = await svc.update(
        "u1",
        "weather",
        UserMcpServerUpdate(
            endpoint="https://mcp.example.com/rpc", description="new", trusted=True
        ),
    )
    assert updated.description == "new"
    assert updated.trusted is True
    assert updated.createdAt == created.createdAt


async def test_update_missing_raises_not_found():
    svc = _service()
    with pytest.raises(McpNotFoundError):
        await svc.update(
            "u1", "nope", UserMcpServerUpdate(endpoint="https://mcp.example.com/rpc")
        )


async def test_update_reuses_stored_secret_without_reentry():
    connector = FakeMcpConnector(list(_TOOLS))
    secret_store = InMemoryMcpSecretStore()
    svc = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=connector,
        secret_store=secret_store,
        resolver=_PUBLIC_RESOLVER,
    )
    created = await svc.create(
        "u1", _create(authMode=McpAuthMode.bearer, secret="t0ken")
    )
    # Update without re-supplying the secret: it is reused to reconnect, and the
    # reference is preserved.
    updated = await svc.update(
        "u1",
        "weather",
        UserMcpServerUpdate(
            endpoint="https://mcp.example.com/rpc",
            authMode=McpAuthMode.bearer,
            description="new",
        ),
    )
    assert updated.description == "new"
    assert updated.secretRef == created.secretRef
    _endpoint, auth = connector.calls[-1]
    assert auth.secret == "t0ken"
    assert await secret_store.get_secret(updated.secretRef) == "t0ken"


async def test_update_rotates_secret_when_resupplied():
    secret_store = InMemoryMcpSecretStore()
    svc = _service(secret_store=secret_store)
    created = await svc.create(
        "u1", _create(authMode=McpAuthMode.bearer, secret="old")
    )
    updated = await svc.update(
        "u1",
        "weather",
        UserMcpServerUpdate(
            endpoint="https://mcp.example.com/rpc",
            authMode=McpAuthMode.bearer,
            secret="new",
        ),
    )
    assert updated.secretRef == created.secretRef  # reference is stable across rotation
    assert await secret_store.get_secret(updated.secretRef) == "new"


async def test_update_to_public_clears_stored_secret():
    secret_store = InMemoryMcpSecretStore()
    svc = _service(secret_store=secret_store)
    created = await svc.create(
        "u1", _create(authMode=McpAuthMode.bearer, secret="t0ken")
    )
    ref = created.secretRef
    updated = await svc.update(
        "u1",
        "weather",
        UserMcpServerUpdate(
            endpoint="https://mcp.example.com/rpc", authMode=McpAuthMode.none
        ),
    )
    assert updated.secretRef is None
    assert await secret_store.get_secret(ref) is None


async def test_update_first_time_secret_is_persisted():
    secret_store = InMemoryMcpSecretStore()
    svc = _service(secret_store=secret_store)
    await svc.create("u1", _create())  # public, no secret
    updated = await svc.update(
        "u1",
        "weather",
        UserMcpServerUpdate(
            endpoint="https://mcp.example.com/rpc",
            authMode=McpAuthMode.bearer,
            secret="fresh",
        ),
    )
    assert updated.secretRef
    assert await secret_store.get_secret(updated.secretRef) == "fresh"


async def test_delete_is_idempotent():
    svc = _service()
    await svc.create("u1", _create())
    await svc.delete("u1", "weather")
    with pytest.raises(McpNotFoundError):
        await svc.get("u1", "weather")
    # Deleting again does not raise.
    await svc.delete("u1", "weather")


async def test_delete_removes_stored_secret():
    secret_store = InMemoryMcpSecretStore()
    svc = _service(secret_store=secret_store)
    created = await svc.create(
        "u1", _create(authMode=McpAuthMode.bearer, secret="t0ken")
    )
    ref = created.secretRef
    assert await secret_store.get_secret(ref) == "t0ken"
    await svc.delete("u1", "weather")
    assert await secret_store.get_secret(ref) is None


async def test_list_is_per_user():
    svc = _service()
    await svc.create("u1", _create(name="a"))
    await svc.create("u2", _create(name="b"))
    assert {s.name for s in await svc.list_for("u1")} == {"a"}
    assert {s.name for s in await svc.list_for("u2")} == {"b"}


# --- Test (re-discovery) -----------------------------------------------------


async def test_test_refreshes_tools():
    connector = FakeMcpConnector(list(_TOOLS))
    svc = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=connector,
        secret_store=InMemoryMcpSecretStore(),
        resolver=_PUBLIC_RESOLVER,
    )
    await svc.create("u1", _create())
    connector._tools = [DiscoveredTool(name="new_tool")]  # server now offers a new tool
    refreshed = await svc.test("u1", "weather")
    assert [t.name for t in refreshed.discoveredTools] == ["new_tool"]
    assert refreshed.lastError is None


async def test_test_reuses_stored_secret_for_authed_server():
    connector = FakeMcpConnector(list(_TOOLS))
    svc = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=connector,
        secret_store=InMemoryMcpSecretStore(),
        resolver=_PUBLIC_RESOLVER,
    )
    await svc.create("u1", _create(authMode=McpAuthMode.bearer, secret="t0ken"))
    # No secret re-supplied: the durably stored one is resolved and reused.
    refreshed = await svc.test("u1", "weather")
    assert refreshed.lastError is None
    _endpoint, auth = connector.calls[-1]
    assert auth.secret == "t0ken"


async def test_test_connection_failure_records_last_error():
    connector = FakeMcpConnector(list(_TOOLS))
    svc = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=connector,
        secret_store=InMemoryMcpSecretStore(),
        resolver=_PUBLIC_RESOLVER,
    )
    await svc.create("u1", _create())
    connector._error = McpConnectionError("down")
    with pytest.raises(McpConnectionError):
        await svc.test("u1", "weather")
    stored = await svc.get("u1", "weather")
    assert stored.lastError == "connection_error"



# --- Per-tool approval persistence + health/quarantine ------------------------


class _CountingStore(InMemoryUserMcpServerStore):
    """In-memory store that counts ``put`` calls (to prove write-free hot paths)."""

    def __init__(self) -> None:
        super().__init__()
        self.put_count = 0
        self.health_write_count = 0

    async def put(self, server: UserMcpServer) -> None:
        self.put_count += 1
        await super().put(server)

    async def update_health(self, user_id, name, *, ok, error):
        updated, changed, became_quarantined = await super().update_health(
            user_id, name, ok=ok, error=error
        )
        if changed:
            self.health_write_count += 1
        return updated, changed, became_quarantined


def _service_with_store(store, *, tools=None, error=None):
    return McpServerService(
        store,
        connector=FakeMcpConnector(tools if tools is not None else list(_TOOLS), error=error),
        secret_store=InMemoryMcpSecretStore(),
        resolver=_PUBLIC_RESOLVER,
    )


async def test_update_persists_and_prunes_tool_approvals():
    svc = _service()
    await svc.create("u1", _create())  # discovers get_forecast + get_alerts
    updated = await svc.update(
        "u1",
        "weather",
        UserMcpServerUpdate(
            endpoint="https://mcp.example.com/rpc",
            toolApprovals={
                "get_forecast": McpToolApproval.never,
                "get_alerts": McpToolApproval.default,  # baseline -> pruned
                "ghost": McpToolApproval.always,  # vanished tool -> pruned
            },
        ),
    )
    # Only the real, non-default override survives.
    assert updated.toolApprovals == {"get_forecast": McpToolApproval.never}
    # And it round-trips through the store.
    stored = await svc.get("u1", "weather")
    assert stored.toolApprovals == {"get_forecast": McpToolApproval.never}


async def test_update_without_tool_approvals_keeps_existing():
    svc = _service()
    await svc.create("u1", _create())
    await svc.update(
        "u1",
        "weather",
        UserMcpServerUpdate(
            endpoint="https://mcp.example.com/rpc",
            toolApprovals={"get_forecast": McpToolApproval.always},
        ),
    )
    # A later update that omits toolApprovals (None) leaves the stored overrides intact.
    updated = await svc.update(
        "u1", "weather", UserMcpServerUpdate(endpoint="https://mcp.example.com/rpc")
    )
    assert updated.toolApprovals == {"get_forecast": McpToolApproval.always}


async def test_record_health_failure_persists_and_quarantines():
    store = _CountingStore()
    svc = _service_with_store(store)
    server = await svc.create("u1", _create())
    base = store.put_count
    for _ in range(QUARANTINE_THRESHOLD):
        await svc.record_health(server, ok=False, error="connection refused")
    # Every failure advanced the count, so every failure persisted.
    assert store.put_count == base
    assert store.health_write_count == QUARANTINE_THRESHOLD
    stored = await svc.get("u1", "weather")
    assert stored.consecutiveFailures == QUARANTINE_THRESHOLD
    assert is_quarantined(stored) is True
    assert stored.lastHealthError == "execution_error"


async def test_record_health_healthy_is_write_free():
    store = _CountingStore()
    svc = _service_with_store(store)
    server = await svc.create("u1", _create())
    base = store.put_count
    await svc.record_health(server, ok=True)
    # A healthy report on an already-healthy server forces no durable write.
    assert store.put_count == base
    assert store.health_write_count == 0


async def test_record_health_recovery_clears_quarantine_and_persists():
    store = _CountingStore()
    svc = _service_with_store(store)
    server = await svc.create("u1", _create())
    for _ in range(QUARANTINE_THRESHOLD):
        await svc.record_health(server, ok=False, error="boom")
    assert is_quarantined(server) is True
    before = store.health_write_count
    await svc.record_health(server, ok=True)
    assert store.health_write_count == before + 1
    stored = await svc.get("u1", "weather")
    assert stored.consecutiveFailures == 0
    assert stored.quarantinedUntil is None
    assert is_quarantined(stored) is False


async def test_record_health_preserves_concurrent_endpoint_and_auth_edit():
    store = _CountingStore()
    svc = _service_with_store(store)
    execution_snapshot = (
        await svc.create("u1", _create())
    ).model_copy(deep=True)
    edited = await svc.get("u1", "weather")
    edited.endpoint = "https://new.example.com/rpc"
    edited.host = "new.example.com"
    edited.authMode = McpAuthMode.bearer
    edited.secretRef = "mcp/new-secret"
    await store.put(edited)

    await svc.record_health(execution_snapshot, ok=False, error="connection refused")

    stored = await svc.get("u1", "weather")
    assert stored.endpoint == "https://new.example.com/rpc"
    assert stored.host == "new.example.com"
    assert stored.authMode is McpAuthMode.bearer
    assert stored.secretRef == "mcp/new-secret"
    assert stored.consecutiveFailures == 1


async def test_record_health_store_failure_does_not_break_tool_result():
    class FailingHealthStore(_CountingStore):
        async def update_health(self, user_id, name, *, ok, error):
            raise RuntimeError("cosmos unavailable")

    store = FailingHealthStore()
    svc = _service_with_store(store)
    server = await svc.create("u1", _create())

    await svc.record_health(server, ok=False, error="connection refused")

    assert (await svc.get("u1", "weather")).consecutiveFailures == 0


async def test_cosmos_health_patch_retries_etag_and_never_writes_config_fields():
    from azure.cosmos.exceptions import CosmosAccessConditionFailedError

    server = UserMcpServer(
        id="weather",
        userId="u1",
        name="weather",
        displayName="Weather",
        endpoint="https://old.example.com/rpc",
        host="old.example.com",
    )

    class RacingContainer:
        def __init__(self) -> None:
            self.item = {**server.model_dump(mode="json"), "_etag": "e1"}
            self.patch_calls: list[list[dict]] = []

        async def read_item(self, *, item, partition_key):
            return dict(self.item)

        async def patch_item(
            self,
            *,
            item,
            partition_key,
            patch_operations,
            etag,
            match_condition,
        ):
            self.patch_calls.append(patch_operations)
            if len(self.patch_calls) == 1:
                self.item["endpoint"] = "https://new.example.com/rpc"
                self.item["host"] = "new.example.com"
                self.item["_etag"] = "e2"
                raise CosmosAccessConditionFailedError(message="etag")
            for operation in patch_operations:
                self.item[operation["path"].lstrip("/")] = operation["value"]

    container = RacingContainer()
    store = object.__new__(CosmosUserMcpServerStore)
    store._container = container

    updated, changed, _ = await store.update_health(
        "u1", "weather", ok=False, error="connection refused"
    )

    assert changed is True
    assert updated is not None and updated.consecutiveFailures == 1
    assert container.item["endpoint"] == "https://new.example.com/rpc"
    assert container.item["host"] == "new.example.com"
    assert len(container.patch_calls) == 2
    assert {
        operation["path"] for call in container.patch_calls for operation in call
    } == {
        "/consecutiveFailures",
        "/quarantinedUntil",
        "/lastHealthCheck",
        "/lastHealthError",
    }


async def test_repeated_test_failures_quarantine_the_server():
    connector = FakeMcpConnector(list(_TOOLS))
    svc = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=connector,
        secret_store=InMemoryMcpSecretStore(),
        resolver=_PUBLIC_RESOLVER,
    )
    await svc.create("u1", _create())
    connector._error = McpConnectionError("down")
    for _ in range(QUARANTINE_THRESHOLD):
        with pytest.raises(McpConnectionError):
            await svc.test("u1", "weather")
    stored = await svc.get("u1", "weather")
    assert stored.consecutiveFailures == QUARANTINE_THRESHOLD
    assert is_quarantined(stored) is True


async def test_successful_test_resets_health():
    connector = FakeMcpConnector(list(_TOOLS))
    svc = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=connector,
        secret_store=InMemoryMcpSecretStore(),
        resolver=_PUBLIC_RESOLVER,
    )
    await svc.create("u1", _create())
    connector._error = McpConnectionError("down")
    for _ in range(QUARANTINE_THRESHOLD):
        with pytest.raises(McpConnectionError):
            await svc.test("u1", "weather")
    assert is_quarantined(await svc.get("u1", "weather")) is True
    # The server comes back: a successful re-discovery clears health.
    connector._error = None
    refreshed = await svc.test("u1", "weather")
    assert refreshed.consecutiveFailures == 0
    assert refreshed.quarantinedUntil is None
    assert refreshed.lastHealthError is None
