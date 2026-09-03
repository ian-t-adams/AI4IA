"""Tests for the curated "official" MCP plane (APIM-fronted servers).

Covers the four moving parts the feature adds, each in isolation and without any
network or live server:

* **Catalog** load (default-empty packaged file + explicit-path projection).
* **APIM auth** — the ``apim_subscription`` mode maps to the
  ``Ocp-Apim-Subscription-Key`` header.
* **Projection + service** — ``build_official_servers`` shapes catalog entries
  into trusted ``UserMcpServer`` records, and ``OfficialMcpService`` discovers
  their tools lazily, caches them, throttles retries, and serves the app-global
  key.
* **Config** fail-closed validation (enabled requires gateway URL + key).
* **Multi-plane merge** — official-plane-wins de-dup and the shared per-turn
  call budget in ``build_mcp_turn_tools_multi``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpAuth, McpToolResult
from ai4ia_api.agents.mcp_execution import McpPlane, build_mcp_turn_tools_multi
from ai4ia_api.agents.mcp_servers import (
    DiscoveredResource,
    DiscoveredTool,
    McpAuthMode,
    McpTransport,
    UserMcpServer,
    tool_alias,
)
from ai4ia_api.agents.official_mcp_service import (
    OFFICIAL_USER_ID,
    OfficialMcpService,
    build_official_servers,
)
from ai4ia_api.agents.tool_exec import ToolExecutionError
from ai4ia_api.config import Settings
from ai4ia_api.official_mcp_catalog import (
    OfficialMcpCatalog,
    OfficialMcpServer,
    load_official_mcp_catalog,
)

_PUBLIC_RESOLVER = lambda _host: ["93.184.216.34"]  # noqa: E731 - terse test stub


def _tool(name: str = "go", schema: dict | None = None) -> DiscoveredTool:
    return DiscoveredTool(name=name, description=f"{name} tool", inputSchema=schema or {})


def _catalog(*entries: dict) -> OfficialMcpCatalog:
    return OfficialMcpCatalog(servers=[OfficialMcpServer(**e) for e in entries])


def _byo_server(
    name: str,
    *,
    trusted: bool = False,
    tools: list[DiscoveredTool] | None = None,
    auth_mode: McpAuthMode = McpAuthMode.none,
) -> UserMcpServer:
    """A BYO-shaped record for the multi-plane merge tests."""
    host = f"{name}.example.com"
    return UserMcpServer(
        id=name,
        userId="u1",
        name=name,
        displayName=name,
        endpoint=f"https://{host}/rpc",
        host=host,
        authMode=auth_mode,
        trusted=trusted,
        enabled=True,
        secretRef=None,
        discoveredTools=tools or [_tool()],
    )


class _Secrets:
    """Minimal SecretResolver mapping server name -> secret."""

    def __init__(self, by_name: dict[str, str] | None = None) -> None:
        self._by_name = by_name or {}

    async def secret_for(self, server: UserMcpServer) -> str | None:
        return self._by_name.get(server.name)


class _FlakyConnector:
    """Connector whose ``discover`` replays a script: an Exception raises, a list returns."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.attempts = 0

    async def discover(self, *, endpoint: str, auth: McpAuth) -> list[DiscoveredTool]:
        self.attempts += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return list(step)

    async def call_tool(self, *, endpoint, auth, tool, arguments):  # pragma: no cover
        raise AssertionError("call_tool is not exercised by discovery tests")


class _FlakyResourceConnector(FakeMcpConnector):
    def __init__(self) -> None:
        super().__init__(tools=[_tool("search")])
        self.resource_attempts = 0

    async def list_resources(self, *, endpoint, auth):
        self.resource_attempts += 1
        if self.resource_attempts == 1:
            raise RuntimeError("resource list unavailable")
        return [
            DiscoveredResource(
                uri="skill://evidence-review/SKILL.md",
                name="evidence-review",
            )
        ]


def _settings(**overrides) -> Settings:
    base = dict(
        env="local",
        auth_provider="dev",
        allow_dev_auth=True,
        session_store="memory",
        model_gateway_url="http://gateway.test",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _svc(
    catalog: OfficialMcpCatalog,
    connector,
    *,
    key: str = "APIM-KEY",
    retry_interval_s: float = 60.0,
    resource_refresh_interval_s: float = 300.0,
) -> OfficialMcpService:
    return OfficialMcpService(
        catalog,
        gateway_url="https://apim-mcp.azure-api.net",
        subscription_key=key,
        connector=connector,
        resolver=_PUBLIC_RESOLVER,
        retry_interval_s=retry_interval_s,
        resource_refresh_interval_s=resource_refresh_interval_s,
    )


# --- Catalog ----------------------------------------------------------------


def test_packaged_catalog_contains_the_activated_toolbox():
    # The Foundry toolbox bridge has been activated: infra/mcp-servers.json registers the
    # ai4ia-toolbox and the generated runtime catalog surfaces it (id + APIM path). If the
    # catalog is later emptied again, flip this back to asserting `.servers == []`.
    servers = load_official_mcp_catalog().servers
    ids = [s.id for s in servers]
    assert "ai4ia-toolbox" in ids
    toolbox = next(s for s in servers if s.id == "ai4ia-toolbox")
    assert toolbox.path == "ai4ia-toolbox/mcp"
    assert toolbox.resourcesEnabled is True


def test_explicit_catalog_loads_and_get(tmp_path):
    p = tmp_path / "official_mcp_catalog.json"
    p.write_text(
        json.dumps(
            {"servers": [
                {"id": "github", "displayName": "GitHub", "description": "d", "path": "github/mcp"}
            ]}
        ),
        encoding="utf-8",
    )
    cat = load_official_mcp_catalog(str(p))
    assert [s.id for s in cat.servers] == ["github"]
    got = cat.get("github")
    assert got is not None and got.path == "github/mcp"
    assert cat.get("missing") is None


# --- APIM auth header -------------------------------------------------------


def test_apim_auth_mode_sets_subscription_key_header():
    headers = McpAuth(mode=McpAuthMode.apim_subscription, secret="K").headers()
    assert headers == {"Ocp-Apim-Subscription-Key": "K"}


def test_apim_auth_mode_without_secret_emits_no_header():
    assert McpAuth(mode=McpAuthMode.apim_subscription, secret=None).headers() == {}


# --- Projection (build_official_servers) ------------------------------------


def test_build_official_servers_projects_endpoint_host_and_flags():
    cat = _catalog(
        {"id": "github", "displayName": "GitHub", "description": "GH", "path": "github/mcp"}
    )
    [s] = build_official_servers(cat, gateway_url="https://apim-mcp.azure-api.net")
    assert s.endpoint == "https://apim-mcp.azure-api.net/github/mcp"
    assert s.host == "apim-mcp.azure-api.net"
    assert s.userId == OFFICIAL_USER_ID
    assert s.name == "github"
    assert s.displayName == "GitHub"
    assert s.authMode is McpAuthMode.apim_subscription
    assert s.transport is McpTransport.streamable_http
    # Curated ⇒ trusted (pre-approved) and no per-server secret (key is app-global).
    assert s.trusted is True
    assert s.enabled is True
    assert s.secretRef is None
    assert s.resourcesEnabled is False


def test_build_official_servers_normalizes_slashes():
    # A trailing slash on the gateway and a leading slash on the path must not
    # produce a double slash in the composed endpoint.
    cat = _catalog({"id": "x", "displayName": "X", "path": "/x/mcp"})
    [s] = build_official_servers(cat, gateway_url="https://g.example.net/")
    assert s.endpoint == "https://g.example.net/x/mcp"


def test_build_official_servers_displayname_falls_back_to_id():
    cat = OfficialMcpCatalog(servers=[OfficialMcpServer(id="y", displayName="", path="y/mcp")])
    [s] = build_official_servers(cat, gateway_url="https://g/")
    assert s.displayName == "y"


def test_build_official_servers_empty_catalog():
    assert build_official_servers(OfficialMcpCatalog(), gateway_url="https://g") == []


# --- OfficialMcpService: discovery, caching, retry, key ----------------------


async def test_service_empty_catalog_lists_nothing():
    svc = _svc(OfficialMcpCatalog(), FakeMcpConnector())
    assert await svc.list_all() == []
    await svc.close()  # no-op, present for lifecycle symmetry


async def test_service_discovers_once_and_caches():
    cat = _catalog({"id": "github", "displayName": "GitHub", "path": "github/mcp"})
    conn = FakeMcpConnector(tools=[_tool("search")])
    svc = _svc(cat, conn)
    servers = await svc.list_all()
    assert [t.name for t in servers[0].discoveredTools] == ["search"]
    # Discovery presented the app-global APIM key against the composed endpoint.
    endpoint, auth = conn.calls[0]
    assert endpoint == "https://apim-mcp.azure-api.net/github/mcp"
    assert auth.mode is McpAuthMode.apim_subscription
    assert auth.secret == "APIM-KEY"
    await svc.list_all()  # cached: the second turn does not re-discover
    assert len(conn.calls) == 1


async def test_service_secret_for_returns_app_global_key():
    cat = _catalog({"id": "g", "displayName": "G", "path": "g/mcp"})
    svc = _svc(cat, FakeMcpConnector(tools=[_tool()]), key="THE-KEY")
    [s] = await svc.list_all()
    assert await svc.secret_for(s) == "THE-KEY"


async def test_service_failed_discovery_is_throttled():
    cat = _catalog({"id": "g", "displayName": "G", "path": "g/mcp"})
    conn = FakeMcpConnector(error=RuntimeError("apim down"))
    svc = _svc(cat, conn)  # default 60s retry window
    [s1] = await svc.list_all()
    assert s1.discoveredTools == []  # a failed server contributes zero tools
    await svc.list_all()  # within the retry window: not re-attempted
    assert len(conn.calls) == 1


async def test_service_retries_until_success_then_caches():
    cat = _catalog({"id": "g", "displayName": "G", "path": "g/mcp"})
    conn = _FlakyConnector([RuntimeError("blip"), [_tool("ok")]])
    svc = _svc(cat, conn, retry_interval_s=0.0)  # no throttle: retry every turn
    assert (await svc.list_all())[0].discoveredTools == []  # attempt 1 fails
    discovered = (await svc.list_all())[0].discoveredTools  # attempt 2 succeeds
    assert [t.name for t in discovered] == ["ok"]
    await svc.list_all()  # now cached: no third attempt (would pop an empty script)
    assert conn.attempts == 2


async def test_service_keeps_tools_and_retries_transient_resource_discovery():
    cat = _catalog(
        {
            "id": "toolbox",
            "displayName": "Toolbox",
            "path": "toolbox/mcp",
            "resourcesEnabled": True,
        }
    )
    conn = _FlakyResourceConnector()
    svc = _svc(cat, conn, retry_interval_s=0.0)

    [first] = await svc.list_all()
    assert [tool.name for tool in first.discoveredTools] == ["search"]
    assert first.discoveredResources == []

    [second] = await svc.list_all()
    assert [resource.name for resource in second.discoveredResources] == [
        "evidence-review"
    ]
    await svc.list_all()
    assert conn.resource_attempts == 2


async def test_resource_enabled_server_periodically_refreshes_discovery():
    cat = _catalog(
        {
            "id": "toolbox",
            "displayName": "Toolbox",
            "path": "toolbox/mcp",
            "resourcesEnabled": True,
        }
    )
    conn = FakeMcpConnector(tools=[_tool("search")])
    svc = _svc(cat, conn, resource_refresh_interval_s=0.0)

    await svc.list_all()
    await svc.list_all()

    assert len(conn.calls) == 2
    assert len(conn.resource_lists) == 2


async def test_resource_refresh_does_not_clear_tool_quarantine():
    cat = _catalog(
        {
            "id": "toolbox",
            "displayName": "Toolbox",
            "path": "toolbox/mcp",
            "resourcesEnabled": True,
        }
    )
    conn = FakeMcpConnector(tools=[_tool("search")])
    svc = _svc(cat, conn, resource_refresh_interval_s=0.0)
    [server] = await svc.list_all()
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    server.consecutiveFailures = 3
    server.quarantinedUntil = until

    await svc.list_all()

    assert server.consecutiveFailures == 3
    assert server.quarantinedUntil == until


async def test_successful_half_open_rediscovery_clears_expired_quarantine():
    cat = _catalog(
        {
            "id": "toolbox",
            "displayName": "Toolbox",
            "path": "toolbox/mcp",
            "resourcesEnabled": True,
        }
    )
    conn = FakeMcpConnector(tools=[_tool("search")])
    svc = _svc(cat, conn, resource_refresh_interval_s=0.0)
    [server] = await svc.list_all()
    server.consecutiveFailures = 3
    server.quarantinedUntil = datetime.now(timezone.utc) - timedelta(seconds=1)

    await svc.list_all()

    assert server.consecutiveFailures == 0
    assert server.quarantinedUntil is None


async def test_service_refresh_forces_rediscovery():
    cat = _catalog({"id": "g", "displayName": "G", "path": "g/mcp"})
    conn = FakeMcpConnector(tools=[_tool("ok")])
    svc = _svc(cat, conn)
    await svc.list_all()
    assert len(conn.calls) == 1
    svc.refresh()  # admin escape hatch: drop the discovery cache
    await svc.list_all()
    assert len(conn.calls) == 2


async def test_service_record_health_is_best_effort():
    cat = _catalog({"id": "g", "displayName": "G", "path": "g/mcp"})
    svc = _svc(cat, FakeMcpConnector(tools=[_tool()]))
    [s] = await svc.list_all()
    # Both outcomes are tolerated and never raise into the turn.
    assert await svc.record_health(s, ok=True) is None
    assert await svc.record_health(s, ok=False, error=RuntimeError("x")) is None


# --- Config: fail-closed validation -----------------------------------------


def test_official_mcp_disabled_by_default_validates():
    s = _settings()
    assert s.official_mcp_enabled is False
    s.validate_runtime()  # no raise


def test_official_mcp_enabled_without_gateway_url_is_rejected():
    s = _settings(official_mcp_enabled=True, official_mcp_subscription_key="k")
    with pytest.raises(RuntimeError, match="OFFICIAL_MCP_GATEWAY_URL"):
        s.validate_runtime()


def test_official_mcp_enabled_without_subscription_key_is_rejected():
    s = _settings(official_mcp_enabled=True, official_mcp_gateway_url="https://apim/")
    with pytest.raises(RuntimeError, match="OFFICIAL_MCP_SUBSCRIPTION_KEY"):
        s.validate_runtime()


def test_official_mcp_enabled_with_url_and_key_validates():
    s = _settings(
        official_mcp_enabled=True,
        official_mcp_gateway_url="https://apim/",
        official_mcp_subscription_key="k",
    )
    s.validate_runtime()  # no raise


def test_official_mcp_enabled_with_empty_catalog_is_rejected(tmp_path):
    # A correctly-configured URL + key with a corrupted/emptied catalog file
    # would silently wire zero official tools; that must fail loud instead.
    empty_catalog = tmp_path / "empty_official_mcp_catalog.json"
    empty_catalog.write_text(json.dumps({"servers": []}), encoding="utf-8")
    s = _settings(
        official_mcp_enabled=True,
        official_mcp_gateway_url="https://apim/",
        official_mcp_subscription_key="k",
        official_mcp_catalog_path=str(empty_catalog),
    )
    with pytest.raises(RuntimeError, match="catalog has no servers"):
        s.validate_runtime()


# --- Multi-plane merge (build_mcp_turn_tools_multi) --------------------------


async def test_official_plane_wins_name_collision():
    # Same server name + tool in both planes ⇒ one namespaced name. The official
    # plane is passed FIRST so its trusted tool can't be shadowed by a BYO server
    # reusing the name.
    official = _byo_server(
        "dup", trusted=True, tools=[_tool("go")], auth_mode=McpAuthMode.apim_subscription
    )
    byo = _byo_server("dup", trusted=False, tools=[_tool("go")])
    off_conn = FakeMcpConnector(call_results={"go": McpToolResult(content="official")})
    byo_conn = FakeMcpConnector(call_results={"go": McpToolResult(content="byo")})
    built = build_mcp_turn_tools_multi(
        planes=[
            McpPlane(
                servers=[official],
                secrets=_Secrets({"dup": "APIM-KEY"}),
                connector=off_conn,
                resolver=_PUBLIC_RESOLVER,
            ),
            McpPlane(
                servers=[byo],
                secrets=_Secrets({"dup": "byo"}),
                connector=byo_conn,
                resolver=_PUBLIC_RESOLVER,
            ),
        ],
        attached_tool_names=["mcp:dup/go"],
    )
    assert built is not None
    _registry, executor, ctx = built
    defn = executor.get(tool_alias("dup", "go"))
    assert defn is not None
    out = await defn.handler({}, ctx)
    assert out["content"] == "official"  # earlier (official) plane won
    assert off_conn.tool_calls and byo_conn.tool_calls == []
    _ep, _t, _a, auth = off_conn.tool_calls[0]
    assert auth.mode is McpAuthMode.apim_subscription and auth.secret == "APIM-KEY"
    assert tool_alias("dup", "go") in ctx.approvals  # trusted official tool is auto-approved


async def test_quarantined_official_does_not_auto_approve_byo_collision():
    official = _byo_server("dup", trusted=True, tools=[_tool("go")])
    official.quarantinedUntil = datetime.now(timezone.utc) + timedelta(minutes=5)
    official.consecutiveFailures = 3
    byo = _byo_server("dup", trusted=False, tools=[_tool("go")])

    built = build_mcp_turn_tools_multi(
        planes=[
            McpPlane(
                servers=[official],
                secrets=_Secrets(),
                connector=FakeMcpConnector(),
                plane_id="official",
            ),
            McpPlane(
                servers=[byo],
                secrets=_Secrets(),
                connector=FakeMcpConnector(),
            ),
        ],
        attached_tool_names=["mcp:dup/go"],
    )

    assert built is not None
    _registry, executor, ctx = built
    byo_alias = tool_alias("dup", "go")
    official_alias = tool_alias("dup", "go", plane="official")
    assert executor.get(byo_alias) is not None
    assert executor.get(official_alias) is None
    assert byo_alias not in ctx.approvals
    assert ctx.tool_aliases == {"mcp:dup/go": byo_alias}


def test_duplicate_named_plane_is_rejected_without_merging_approvals():
    first = _byo_server("first", trusted=False, tools=[_tool("go")])
    second = _byo_server("second", trusted=True, tools=[_tool("go")])
    built = build_mcp_turn_tools_multi(
        planes=[
            McpPlane(
                servers=[first],
                secrets=_Secrets(),
                connector=FakeMcpConnector(),
                plane_id="official",
            ),
            McpPlane(
                servers=[second],
                secrets=_Secrets(),
                connector=FakeMcpConnector(),
                plane_id="official",
            ),
        ],
        attached_tool_names=["mcp:first/go", "mcp:second/go"],
    )

    assert built is not None
    _registry, executor, ctx = built
    first_alias = tool_alias("first", "go", plane="official")
    second_alias = tool_alias("second", "go", plane="official")
    assert executor.get(first_alias) is not None
    assert executor.get(second_alias) is None
    assert second_alias not in ctx.approvals


async def test_multi_plane_unions_distinct_tools_and_approvals():
    official = _byo_server("official-srv", trusted=True, tools=[_tool("go")])
    byo = _byo_server("byo-srv", trusted=False, tools=[_tool("do")])
    built = build_mcp_turn_tools_multi(
        planes=[
            McpPlane(servers=[official], secrets=_Secrets(), connector=FakeMcpConnector()),
            McpPlane(servers=[byo], secrets=_Secrets(), connector=FakeMcpConnector()),
        ],
        attached_tool_names=["mcp:official-srv/go", "mcp:byo-srv/do"],
    )
    assert built is not None
    _registry, executor, ctx = built
    assert executor.get(tool_alias("official-srv", "go")) is not None
    assert executor.get(tool_alias("byo-srv", "do")) is not None
    # Only the trusted official tool is auto-approved; the BYO one stays gated.
    assert ctx.approvals == frozenset({tool_alias("official-srv", "go")})


async def test_shared_budget_caps_total_calls_across_planes():
    a = _byo_server("aaa", trusted=True, tools=[_tool("go")])
    b = _byo_server("bbb", trusted=True, tools=[_tool("go")])
    conn_a = FakeMcpConnector(call_results={"go": McpToolResult(content="A")})
    conn_b = FakeMcpConnector(call_results={"go": McpToolResult(content="B")})
    built = build_mcp_turn_tools_multi(
        planes=[
            McpPlane(servers=[a], secrets=_Secrets(), connector=conn_a, resolver=_PUBLIC_RESOLVER),
            McpPlane(servers=[b], secrets=_Secrets(), connector=conn_b, resolver=_PUBLIC_RESOLVER),
        ],
        attached_tool_names=["mcp:aaa/go", "mcp:bbb/go"],
        max_calls=1,
    )
    assert built is not None
    _registry, executor, ctx = built
    await executor.get(tool_alias("aaa", "go")).handler({}, ctx)  # consumes the single shared call
    with pytest.raises(ToolExecutionError):
        await executor.get(tool_alias("bbb", "go")).handler({}, ctx)  # budget shared across planes
    assert conn_a.tool_calls and conn_b.tool_calls == []


def test_multi_plane_returns_none_when_nothing_attached():
    official = _byo_server("official-srv", trusted=True, tools=[_tool("go")])
    assert (
        build_mcp_turn_tools_multi(
            planes=[McpPlane(servers=[official], secrets=_Secrets(), connector=FakeMcpConnector())],
            attached_tool_names=[],
        )
        is None
    )
