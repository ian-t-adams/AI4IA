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

import hashlib
import json
import re
import unicodedata
import uuid
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
MAX_RESOURCES_PER_SERVER = 50
MAX_RESOURCE_URI_LEN = 2048
MAX_RESOURCE_NAME_LEN = 128
MAX_RESOURCE_DESCRIPTION_LEN = 1024
MAX_RESOURCE_CONTENT_BYTES = 128_000

# The namespaced-tool-name prefix; keeps remote tools from colliding with the
# built-ins and makes their origin obvious in traces.
TOOL_NAME_PREFIX = "mcp"


class McpAuthMode(str, Enum):
    none = "none"  # public server, no credential
    api_key = "api_key"  # static key sent as ``X-API-Key``
    bearer = "bearer"  # bearer token (also the seam a future OAuth flow fills)
    # APIM subscription key sent as ``Ocp-Apim-Subscription-Key``. Used by the
    # curated "official" MCP plane, whose servers sit behind the shared active
    # APIM front door; the key is app-global (not per-user) and supplied by the
    # runtime, never user-entered. Not selectable for BYO servers.
    apim_subscription = "apim_subscription"


class McpTransport(str, Enum):
    streamable_http = "streamable_http"


class McpToolApproval(str, Enum):
    """Standing discovery/attachment posture, overriding the server default.

    This controls whether a discovered tool is offered to the model. It is not the
    fresh invocation approval in :mod:`ai4ia_api.agents.approvals`: interactive
    external/destructive calls on both MCP planes still use that exact-argument,
    single-use gate. ``ApprovalPolicy.off`` is reserved for the explicit unattended
    workflow exception.
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

    # ``name`` is the durable governance/attachment component. ``rawName`` is
    # the exact accepted string advertised by the remote server and is used
    # only for tools/call. They are currently equal for newly discovered tools,
    # but keeping both prevents a future canonicalization from corrupting
    # dispatch and lets old records (which lack rawName) migrate lazily.
    name: str
    rawName: str | None = None
    description: str = ""
    inputSchema: dict[str, Any] = Field(default_factory=dict)

    @property
    def raw_name(self) -> str:
        return self.rawName if self.rawName is not None else self.name


class DiscoveredResource(BaseModel):
    """A bounded MCP resource descriptor used for progressive skill discovery."""

    uri: str = Field(min_length=1, max_length=MAX_RESOURCE_URI_LEN)
    name: str = Field(default="", max_length=MAX_RESOURCE_NAME_LEN)
    description: str = Field(default="", max_length=MAX_RESOURCE_DESCRIPTION_LEN)
    mimeType: str | None = Field(default=None, max_length=128)


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
    discoveredResources: list[DiscoveredResource] = Field(default_factory=list)
    # Only repository-curated official endpoints may become instruction sources.
    # User-created MCP records never expose this field in their create/update APIs.
    resourcesEnabled: bool = Field(default=False, exclude=True)
    # Per-tool human-approval overrides, keyed by the *bare* discovered tool name
    # (not the namespaced name). Absent/``default`` -> inherit the server posture.
    # Persisted independently of ``discoveredTools`` so it survives re-discovery.
    toolApprovals: dict[str, McpToolApproval] = Field(default_factory=dict)
    # Replaced whenever execution-affecting configuration changes. Optional only
    # for backward compatibility with records created before this guard.
    configurationRevision: str | None = None
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


def health_config_revision(server: UserMcpServer) -> str:
    """Fingerprint connection identity so stale health cannot taint a replacement."""
    if server.configurationRevision:
        return f"revision:{server.configurationRevision}"
    payload = {
        "userId": server.userId,
        "name": server.name,
        "createdAt": server.createdAt.isoformat(),
        "endpoint": server.endpoint,
        "host": server.host,
        "transport": server.transport.value,
        "authMode": server.authMode.value,
        "secretRef": server.secretRef,
        "trusted": server.trusted,
        "enabled": server.enabled,
        "discoveredTools": [
            tool.model_dump(mode="json") for tool in server.discoveredTools
        ],
        "discoveredResources": [
            resource.model_dump(mode="json") for resource in server.discoveredResources
        ],
        "toolApprovals": {
            name: posture.value for name, posture in server.toolApprovals.items()
        },
        "updatedAt": server.updatedAt.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def new_configuration_revision() -> str:
    return uuid.uuid4().hex


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


_ALIAS_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_ALIAS_HASH_LEN = 32
_ALIAS_SLUG_LEN = 24


def is_valid_remote_tool_name(name: object) -> bool:
    """Whether a remote tool name is safe to accept and persist.

    Names remain otherwise exact (spaces, dots, slashes, and Unicode letters
    are allowed). Empty/blank, oversized, malformed Unicode, and every Unicode
    ``Other`` category (controls, surrogates, format controls, private-use, and
    unassigned code points) are rejected per tool.
    """
    if not isinstance(name, str) or not name or not name.strip():
        return False
    if len(name) > MAX_TOOL_NAME_LEN:
        return False
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if len(encoded) > MAX_TOOL_NAME_LEN * 4:
        return False
    return not any(unicodedata.category(ch).startswith("C") for ch in name)


def tool_alias(server_name: str, tool_name: str, *, plane: str = "default") -> str:
    """A deterministic, provider-safe alias for one tool definition and plane.

    Remote MCP tool names are supplied by the server operator, not authored or
    reviewed by us, and are not guaranteed to satisfy the ``[A-Za-z0-9_-]{1,64}``
    charset that OpenAI/Azure function names -- and our own log/telemetry
    fields -- require (see ``agents.tools.is_safe_tool_name``). The governed
    name from :func:`namespaced_tool_name` (``mcp:<server>/<tool>``) is a
    durable, user-facing contract (persisted tool attachments, the tool
    catalog) and must keep that shape.

    This alias is a second, purely runtime-computed identifier used only for
    the model-facing function-schema name and for structured logs/telemetry
    (see ``mcp_execution.build_mcp_tool_definitions``): it is never persisted,
    so it is cheaply recomputed identically every turn. Collision resistance
    comes entirely from a 128-bit hash of the plane plus the *raw*
    ``(server_name, tool_name)`` pair -- the human-readable slug is cosmetic and may
    collide, so callers combining aliases across tools must still check for a
    duplicate rather than assume uniqueness from the slug alone. ``plane`` is
    part of the digest so an official definition and a BYO definition with the
    same governance/raw names can never share approval or dispatch identity.
    """
    material = f"{plane}\x00{server_name}\x00{tool_name}".encode(
        "utf-8", errors="surrogatepass"
    )
    digest = hashlib.sha256(material).hexdigest()[:_ALIAS_HASH_LEN]
    slug = _ALIAS_UNSAFE_RE.sub("_", server_name).strip("_")[:_ALIAS_SLUG_LEN] or "srv"
    return f"mcp_{slug}_{digest}"


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
