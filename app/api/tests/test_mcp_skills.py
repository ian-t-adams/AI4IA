from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpResourceResult
from ai4ia_api.agents.mcp_servers import (
    DiscoveredResource,
    McpAuthMode,
    UserMcpServer,
)
from ai4ia_api.agents.mcp_skills import (
    LOAD_SKILL_NAME,
    build_load_skill_definition,
    discover_skills,
)
from ai4ia_api.agents.official_mcp_service import OfficialMcpService
from ai4ia_api.agents.ssrf import SsrfError
from ai4ia_api.agents.tool_exec import ToolContext, ToolExecutionError
from ai4ia_api.official_mcp_catalog import OfficialMcpCatalog, OfficialMcpServer

_URI = "skill://evidence-review/SKILL.md"
_CONTENT = "# Evidence review\n\nUse cited evidence."


def _server(*resources: DiscoveredResource, enabled: bool = True) -> UserMcpServer:
    return UserMcpServer(
        id="toolbox",
        userId="__official__",
        name="toolbox",
        displayName="Toolbox",
        endpoint="https://mcp.example.com/toolbox/mcp",
        host="mcp.example.com",
        authMode=McpAuthMode.apim_subscription,
        trusted=True,
        resourcesEnabled=enabled,
        discoveredResources=list(resources),
    )


def _resource(
    uri: str = _URI,
    *,
    description: str = "Review evidence",
    mime_type: str | None = "text/markdown",
) -> DiscoveredResource:
    return DiscoveredResource(
        uri=uri,
        name="evidence-review",
        description=description,
        mimeType=mime_type,
    )


def test_discover_skills_accepts_only_opted_in_skill_markdown_resources():
    disabled = _server(_resource(), enabled=False)
    enabled = _server(
        _resource("https://example.com/SKILL.md"),
        _resource("skill://evidence-review/reference.md"),
        _resource("skill://binary/SKILL.md", mime_type="application/octet-stream"),
        _resource(),
    )

    skills = discover_skills([disabled, enabled])

    assert [(skill.name, skill.uri) for skill in skills] == [
        ("evidence-review", _URI)
    ]


def test_discover_skills_first_catalog_entry_wins_duplicate_name():
    first = _server(_resource(description="first"))
    second = _server(_resource(description="second"))

    [skill] = discover_skills([first, second])

    assert skill.description == "first"
    assert skill.server is first


def test_discover_skills_omits_quarantined_server():
    server = _server(_resource())
    server.quarantinedUntil = datetime.now(timezone.utc) + timedelta(minutes=5)

    assert discover_skills([server]) == []


async def test_load_skill_reads_exact_advertised_resource_and_returns_provenance():
    catalog = OfficialMcpCatalog(
        servers=[
            OfficialMcpServer(
                id="toolbox",
                displayName="Toolbox",
                path="toolbox/mcp",
                resourcesEnabled=True,
            )
        ]
    )
    connector = FakeMcpConnector(
        resources=[_resource()],
        resource_results={
            _URI: McpResourceResult(
                uri=_URI,
                text=_CONTENT,
                mime_type="text/markdown",
            )
        },
    )
    service = OfficialMcpService(
        catalog,
        gateway_url="https://mcp.example.com",
        subscription_key="secret",
        connector=connector,
        resolver=lambda _host: ["93.184.216.34"],
    )
    servers = await service.list_all()
    definition = build_load_skill_definition(servers=servers, reader=service)

    assert definition is not None
    assert definition.spec.name == LOAD_SKILL_NAME
    assert definition.parameters["properties"]["name"]["enum"] == [
        "evidence-review"
    ]
    result = await definition.handler(
        {"name": "evidence-review"},
        ToolContext(),
    )

    assert result["instructions"] == _CONTENT
    assert result["source"] == {
        "server": "toolbox",
        "uri": _URI,
        "mimeType": "text/markdown",
    }
    assert result["contentSha256"] == hashlib.sha256(
        _CONTENT.encode("utf-8")
    ).hexdigest()
    assert connector.resource_reads[0][1] == _URI


async def test_load_skill_rejects_unknown_name_without_resource_read():
    server = _server(_resource())
    connector = FakeMcpConnector()

    class Reader:
        async def read_resource(self, server, uri):
            return await connector.read_resource(
                endpoint=server.endpoint,
                auth=None,
                uri=uri,
            )

    definition = build_load_skill_definition(servers=[server], reader=Reader())
    assert definition is not None

    with pytest.raises(ToolExecutionError, match="not available"):
        await definition.handler({"name": "missing"}, ToolContext())
    assert connector.resource_reads == []


async def test_official_service_rechecks_advertised_uri_on_read():
    catalog = OfficialMcpCatalog(
        servers=[
            OfficialMcpServer(
                id="toolbox",
                displayName="Toolbox",
                path="toolbox/mcp",
                resourcesEnabled=True,
            )
        ]
    )
    connector = FakeMcpConnector(resources=[_resource()])
    service = OfficialMcpService(
        catalog,
        gateway_url="https://mcp.example.com",
        subscription_key="secret",
        connector=connector,
        resolver=lambda _host: ["93.184.216.34"],
    )
    [server] = await service.list_all()

    with pytest.raises(ValueError, match="not advertised"):
        await service.read_resource(server, "skill://other/SKILL.md")
    assert connector.resource_reads == []


async def test_official_resource_read_rechecks_dns_for_rebinding():
    resolutions = iter(
        [
            ["93.184.216.34"],
            ["127.0.0.1"],
        ]
    )
    catalog = OfficialMcpCatalog(
        servers=[
            OfficialMcpServer(
                id="toolbox",
                displayName="Toolbox",
                path="toolbox/mcp",
                resourcesEnabled=True,
            )
        ]
    )
    connector = FakeMcpConnector(resources=[_resource()])
    service = OfficialMcpService(
        catalog,
        gateway_url="https://mcp.example.com",
        subscription_key="secret",
        connector=connector,
        resolver=lambda _host: next(resolutions),
    )
    [server] = await service.list_all()

    with pytest.raises(SsrfError, match="non-public"):
        await service.read_resource(server, _URI)
    assert connector.resource_reads == []


async def test_official_resource_read_refuses_quarantined_server():
    catalog = OfficialMcpCatalog(
        servers=[
            OfficialMcpServer(
                id="toolbox",
                displayName="Toolbox",
                path="toolbox/mcp",
                resourcesEnabled=True,
            )
        ]
    )
    connector = FakeMcpConnector(resources=[_resource()])
    service = OfficialMcpService(
        catalog,
        gateway_url="https://mcp.example.com",
        subscription_key="secret",
        connector=connector,
        resolver=lambda _host: ["93.184.216.34"],
    )
    [server] = await service.list_all()
    server.quarantinedUntil = datetime.now(timezone.utc) + timedelta(minutes=5)

    with pytest.raises(ValueError, match="quarantined"):
        await service.read_resource(server, _URI)
    assert connector.resource_reads == []
