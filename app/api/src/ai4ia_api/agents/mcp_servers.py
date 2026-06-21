"""User-registered MCP servers — "bring your own" tools.

A *user MCP server* is a durable, per-user record pointing at a remote
Model-Context-Protocol server (Streamable HTTP transport) the user owns or
trusts. On save we connect, list the server's tools, and cache their schemas.
Each discovered tool is then projected onto the *existing* tool-governance seam
(:class:`~ai4ia_api.agents.tools.ToolSpec`) as an ``external``-risk tool whose
egress is scoped to the server's host and which (unless the user marks the
server *trusted*) requires human approval — so a remote tool is governed by the
exact same registry/redaction machinery as the built-ins.

This module owns the durable record, the client payloads (which deliberately
exclude server-owned fields), the typed errors, and the projection helpers.
Secrets are **never** stored in the record: an authenticated server's secret is
supplied per request and used only transiently for that connection (durable
Key-Vault-backed secrets + per-turn execution are a later sub-phase).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .tools import ToolRisk, ToolSpec

# Server name grammar: a valid Cosmos item id and a clean, URL-safe handle —
# starts with a lowercase letter, ends alphanumeric/underscore, interior
# ``._-`` allowed. Mirrors the user-agent grammar for consistency.
NAME_RE = re.compile(r"^[a-z](?:[a-z0-9_.-]{0,30}[a-z0-9_])?$")

# Generous defaults — the owner wanted the *ability* to cap, not tight limits.
# The service accepts an override so a deployment can tighten them via settings.
MAX_MCP_SERVERS_PER_USER = 20
MAX_TOOLS_PER_SERVER = 50
MAX_NAME_LEN = 32
MAX_DISPLAY_NAME_LEN = 80
MAX_DESCRIPTION_LEN = 280
MAX_ENDPOINT_LEN = 2048
MAX_SECRET_LEN = 8192
MAX_TOOL_NAME_LEN = 128
MAX_TOOL_DESCRIPTION_LEN = 1024

# The namespaced-tool-name prefix; keeps remote tools from colliding with the
# built-ins and makes their origin obvious in traces.
TOOL_NAME_PREFIX = "mcp"


class McpAuthMode(str, Enum):
    none = "none"  # public server, no credential
    api_key = "api_key"  # static key sent as ``X-API-Key``
    bearer = "bearer"  # bearer token (also the seam a future OAuth flow fills)


class McpTransport(str, Enum):
    streamable_http = "streamable_http"


class McpToolApproval(str, Enum):
    """Per-tool human-approval posture, overriding the server-level default.

    ``default`` inherits the server's posture (approval required unless the server
    is *trusted*); ``always`` forces approval even on a trusted server; ``never``
    pre-approves the tool even on an untrusted server. The default keeps the
    established *approval-unless-trusted* behavior byte-for-byte.
    """

    default = "default"
    always = "always"
    never = "never"


class McpServerError(Exception):
    """Base class for MCP-server service errors."""


class McpValidationError(McpServerError):
    """A field failed validation (-> HTTP 422)."""


class McpConflictError(McpServerError):
    """Name reserved/taken or the per-user cap is reached (-> 409)."""


class McpNotFoundError(McpServerError):
    """No MCP server with that name for this user (-> 404)."""


class McpConnectionError(McpServerError):
    """Connecting to / listing tools from the remote server failed (-> 502)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DiscoveredTool(BaseModel):
    """A tool advertised by a remote MCP server, as cached on the record."""

    name: str
    description: str = ""
    inputSchema: dict[str, Any] = Field(default_factory=dict)


class UserMcpServer(BaseModel):
    """Durable, server-side record of a user-registered MCP server.

    ``id`` equals ``name`` and is unique within the user's ``/userId`` partition.
    No secret is ever persisted here — only ``authMode`` (and the fact that a
    credential is expected). ``host`` is the validated egress host used to scope
    the projected tools' allowlist.
    """

    id: str
    userId: str
    name: str
    displayName: str
    description: str = ""
    endpoint: str
    host: str
    transport: McpTransport = McpTransport.streamable_http
    authMode: McpAuthMode = McpAuthMode.none
    trusted: bool = False
    enabled: bool = True
    # Opaque pointer to the durable connection secret in the secret store.
    # ``None`` for public (``authMode=none``) servers. The raw secret is
    # NEVER stored on this record — only its reference; the value is resolved from
    # the secret store at connect/execute time.
    secretRef: str | None = None
    discoveredTools: list[DiscoveredTool] = Field(default_factory=list)
    # Per-tool human-approval overrides, keyed by the *bare* discovered tool name
    # (not the namespaced name). Absent/``default`` -> inherit the server posture.
    # Persisted independently of ``discoveredTools`` so it survives re-discovery.
    toolApprovals: dict[str, McpToolApproval] = Field(default_factory=dict)
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)
    lastConnectedAt: datetime | None = None
    lastError: str | None = None
    # --- Health / quarantine -----------------------------------------------
    # Per-server health used to skip a persistently failing server instead of
    # hammering it every turn. ``consecutiveFailures`` counts connect/execute
    # transport failures since the last success; ``quarantinedUntil`` (when set
    # and in the future) means the server is skipped until it elapses (auto
    # recovery); ``lastHealthCheck`` is when health was last observed and
    # ``lastHealthError`` is a bounded, redacted summary of the latest failure.
    consecutiveFailures: int = 0
    quarantinedUntil: datetime | None = None
    lastHealthCheck: datetime | None = None
    lastHealthError: str | None = None

    def tool_specs(self) -> list[ToolSpec]:
        """Project the cached discovered tools onto the governance seam."""
        return [discovered_tool_to_spec(self, t) for t in self.discoveredTools]

    def public_dict(self) -> dict[str, Any]:
        """A management-view projection (no secrets exist to strip, but keep the
        shape explicit and stable for the API)."""
        return self.model_dump(mode="json")


class UserMcpServerCreate(BaseModel):
    """Client payload for registering an MCP server. ``secret`` is transient —
    used for the initial connection and never stored."""

    name: str
    displayName: str | None = None
    description: str = ""
    endpoint: str
    authMode: McpAuthMode = McpAuthMode.none
    secret: str | None = None
    trusted: bool = False
    enabled: bool = True


class UserMcpServerUpdate(BaseModel):
    """Client payload for replacing an MCP server (name comes from the path)."""

    displayName: str | None = None
    description: str = ""
    endpoint: str
    authMode: McpAuthMode = McpAuthMode.none
    secret: str | None = None
    trusted: bool = False
    enabled: bool = True
    # Per-tool approval overrides (bare tool name -> posture). Omitted (``None``)
    # leaves the stored overrides unchanged; an explicit map (possibly empty)
    # replaces them. Unknown tool names are pruned against the rediscovered set.
    toolApprovals: dict[str, McpToolApproval] | None = None


class UserMcpServerTest(BaseModel):
    """Optional payload for the ``/test`` endpoint: an authed re-discovery may
    need the secret re-supplied (we never stored it)."""

    secret: str | None = None


def namespaced_tool_name(server_name: str, tool_name: str) -> str:
    """``mcp:<server>/<tool>`` — the governed, collision-proof tool name."""
    return f"{TOOL_NAME_PREFIX}:{server_name}/{tool_name}"


def is_mcp_tool_name(name: str) -> bool:
    """True if ``name`` is a namespaced MCP tool (``mcp:<server>/<tool>``)."""
    return name.startswith(f"{TOOL_NAME_PREFIX}:")


def effective_tool_approval(server: UserMcpServer, tool_name: str) -> McpToolApproval:
    """The per-tool approval posture in force for ``tool_name`` on ``server``.

    Falls back to :attr:`McpToolApproval.default` (inherit the server posture) when
    the user has set no explicit override for that tool.
    """
    posture = server.toolApprovals.get(tool_name)
    return posture if posture is not None else McpToolApproval.default


def tool_requires_approval(server: UserMcpServer, tool_name: str) -> bool:
    """Whether a remote tool needs human approval on each use.

    ``always`` -> required; ``never`` -> not required; ``default`` -> the existing
    rule (required unless the server is marked *trusted*). This is the single
    source of truth shared by the governance projection and the per-turn
    pre-approval set, so the schema the model sees and the runtime gate agree.
    """
    posture = effective_tool_approval(server, tool_name)
    if posture is McpToolApproval.always:
        return True
    if posture is McpToolApproval.never:
        return False
    return not server.trusted


def discovered_tool_to_spec(server: UserMcpServer, tool: DiscoveredTool) -> ToolSpec:
    """Map one discovered remote tool onto a governed :class:`ToolSpec`.

    Remote tools are always ``external`` risk and egress-scoped to the server's
    single host. They require human approval per :func:`tool_requires_approval`
    — i.e. unless the owner marked the server *trusted*, with an optional per-tool
    override (``always``/``never``). Remote tools are classified external, not
    destructive, since we cannot know their side effects.
    """
    return ToolSpec(
        name=namespaced_tool_name(server.name, tool.name),
        description=tool.description or f"{tool.name} (via MCP server '{server.name}')",
        risk=ToolRisk.external,
        requires_approval=tool_requires_approval(server, tool.name),
        egress_allowlist=frozenset({server.host}),
        enabled=server.enabled,
    )
