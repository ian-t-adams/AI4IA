"""Loads the curated "official" MCP server catalog (APIM-fronted servers).

These are admin-curated MCP servers reached **through the shared active APIM
front door** (service: ``apimcore.bicep``; MCP children: ``mcpgateway.bicep``), gated on an
APIM subscription key. This is distinct from per-user BYO remote MCP servers,
which the backend calls directly behind the SSRF guard.

Code/Bicep defaults are still safe-off, and an empty catalog wires no tools,
but this repo's packaged catalog is activated and contains the portable
``ai4ia-toolbox`` Foundry toolbox entry generated from ``infra/mcp-servers.json``.

Precedence mirrors ``catalog.py``: explicit path -> packaged
``data/official_mcp_catalog.json`` -> repo ``infra/mcp-servers.json`` fallback
(dev only, projected on the fly).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .agents.mcp_servers import McpProtocolVersion

_PACKAGED = Path(__file__).resolve().parent / "data" / "official_mcp_catalog.json"
_REPO_ROOT = Path(__file__).resolve().parents[4]

# Provenance and validated-SDK bookkeeping. Every other manifest key, including
# any added later, describes what the live toolbox executes and is bound.
_NON_EXECUTABLE_MANIFEST_KEYS = frozenset({
    "$schema", "_comment", "manifestVersion", "lifecycle", "owner", "sdkContract",
})


def toolbox_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Digest of a toolbox manifest's executable content.

    Keep in step with ``scripts/gen-mcp-catalog.py``, which records it in the
    packaged catalog.
    """
    executable = {key: value for key, value in manifest.items() if key not in _NON_EXECUTABLE_MANIFEST_KEYS}
    canonical = json.dumps(executable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OfficialMcpServer(BaseModel):
    """One curated MCP server reachable through the MCP APIM front door.

    ``path`` is the APIM route suffix (``<name>/mcp``); the backend composes the
    absolute endpoint as ``<official_mcp_gateway_url>/<path>`` at call time and
    presents the global APIM subscription key. No per-server secret is stored —
    the subscription key is app-global.
    """

    id: str
    displayName: str
    description: str = ""
    path: str
    resourcesEnabled: bool = False
    protocolVersion: McpProtocolVersion = McpProtocolVersion.legacy
    # The reviewed Foundry toolbox content. It is part of the server's
    # configuration revision, so a toolbox change renews tool consent even when
    # a generic dispatcher tool such as tool search's call_tool keeps its schema.
    toolboxManifestSha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class OfficialMcpCatalog(BaseModel):
    servers: list[OfficialMcpServer] = []

    def get(self, server_id: str) -> OfficialMcpServer | None:
        return next((s for s in self.servers if s.id == server_id), None)


def _project_infra_catalog(raw: dict[str, Any]) -> dict[str, Any]:
    """Project infra/mcp-servers.json -> runtime shape (dev fallback only)."""
    manifest_path = _REPO_ROOT / "foundry" / "toolbox.manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    )
    servers = []
    for entry in raw.get("servers", []):
        name = entry["name"]
        server = {
            "id": name,
            "displayName": entry.get("displayName", name),
            "description": entry.get("description", ""),
            "path": f"{name}/mcp",
            "resourcesEnabled": bool(entry.get("foundryToolbox")),
            "protocolVersion": entry.get("protocolVersion", McpProtocolVersion.legacy.value),
        }
        if entry.get("foundryToolbox") and isinstance(manifest, dict) and manifest.get("name") == name:
            server["toolboxManifestSha256"] = toolbox_manifest_sha256(manifest)
        servers.append(server)
    return {"servers": servers}


def _load_raw(explicit_path: str | None) -> dict[str, Any]:
    if explicit_path:
        return json.loads(Path(explicit_path).read_text(encoding="utf-8"))
    if _PACKAGED.exists():
        return json.loads(_PACKAGED.read_text(encoding="utf-8"))
    # Dev fallback: project infra/mcp-servers.json if running from a source checkout.
    infra = _REPO_ROOT / "infra" / "mcp-servers.json"
    if infra.exists():
        return _project_infra_catalog(json.loads(infra.read_text(encoding="utf-8")))
    # Absent catalog is not fatal: safe-off consumers can run with no official servers.
    return {"servers": []}


@lru_cache
def load_official_mcp_catalog(explicit_path: str | None = None) -> OfficialMcpCatalog:
    raw = _load_raw(explicit_path)
    return OfficialMcpCatalog(servers=raw.get("servers", []))
