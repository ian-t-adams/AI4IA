"""Guard the private tool catalog (API Center) registration script (docs/foundry-toolbox.md P6).

Pins the *pure* projection logic without any Azure SDK/network: each curated MCP server in
infra/mcp-servers.json is projected to an API Center MCP asset whose URL is the **APIM
consumer URL** (https://<gateway>/<name>/mcp) -- i.e. the governed front door, so discovery
and governance stay on the proxy. Also verifies fail-closed input resolution and the emitted
`az apic api create` command shape.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "provision-private-tool-catalog.py"
_CATALOG = _REPO_ROOT / "infra" / "mcp-servers.json"

_GATEWAY = "https://apim-mcp.example.com"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cat = _load("provision_private_tool_catalog", _SCRIPT)


def _servers() -> list[dict]:
    return [
        {"name": "foundry-toolbox", "displayName": "Foundry Toolbox", "description": "Bridge"},
        {"name": "learn", "displayName": "MS Learn"},
    ]


def test_consumer_url_is_apim_fronted_mcp_route():
    assert _cat.consumer_url(_GATEWAY, "learn") == "https://apim-mcp.example.com/learn/mcp"
    # trailing slash on the gateway is normalized
    assert _cat.consumer_url(_GATEWAY + "/", "learn") == "https://apim-mcp.example.com/learn/mcp"


def test_build_asset_projects_name_title_and_mcp_url():
    asset = _cat.build_asset(_servers()[0], _GATEWAY)
    assert asset["apiId"] == "foundry-toolbox"
    assert asset["title"] == "Foundry Toolbox"
    assert asset["kind"] == "mcp"
    assert asset["description"] == "Bridge"
    assert asset["mcpUrl"] == "https://apim-mcp.example.com/foundry-toolbox/mcp"


def test_build_asset_falls_back_to_name_for_title():
    asset = _cat.build_asset({"name": "bare"}, _GATEWAY)
    assert asset["title"] == "bare"
    assert asset["description"] == ""


def test_plan_assets_preserves_order_and_count():
    assets = _cat.plan_assets(_servers(), _GATEWAY)
    assert [a["apiId"] for a in assets] == ["foundry-toolbox", "learn"]


def test_to_az_command_registers_mcp_asset_in_default_workspace():
    asset = _cat.build_asset(_servers()[0], _GATEWAY)
    cmd = _cat.to_az_command(asset, "apic-ai4ia-dev", "rg-ai4ia")
    assert cmd.startswith("az apic api create")
    assert "--service-name \"apic-ai4ia-dev\"" in cmd
    assert "--resource-group \"rg-ai4ia\"" in cmd
    assert "--workspace-name default" in cmd
    assert "--api-id \"foundry-toolbox\"" in cmd
    assert "--type mcp" in cmd
    # the APIM URL is carried as a custom property (round-trippable JSON)
    assert "https://apim-mcp.example.com/foundry-toolbox/mcp" in cmd


def test_resolve_fails_closed_without_arg_or_env(monkeypatch):
    monkeypatch.delenv("AZURE_API_CENTER_NAME", raising=False)
    with pytest.raises(SystemExit):
        _cat.resolve(None, "AZURE_API_CENTER_NAME", "API Center name")
    assert _cat.resolve("explicit", "AZURE_API_CENTER_NAME", "API Center name") == "explicit"


def test_resolve_prefers_env_when_no_arg(monkeypatch):
    monkeypatch.setenv("AZURE_OFFICIAL_MCP_GATEWAY_URL", _GATEWAY)
    assert _cat.resolve(None, "AZURE_OFFICIAL_MCP_GATEWAY_URL", "gateway") == _GATEWAY


def test_checked_in_catalog_is_empty_so_registration_is_a_noop():
    # The shipped catalog is intentionally empty; the script must no-op cleanly.
    servers = _cat.load_servers(_CATALOG)
    assert servers == []
