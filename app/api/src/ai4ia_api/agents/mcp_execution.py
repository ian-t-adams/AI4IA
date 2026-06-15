"""Per-turn MCP tool **execution** (Phase 12B Increment B).

Phase 12A discovered a user's remote MCP tools and projected each onto the tool
governance seam (:class:`~ai4ia_api.agents.tools.ToolSpec`). This module makes those
projected tools *runnable* inside a chat turn — but governed identically to the
built-ins: each attached MCP tool becomes a :class:`~ai4ia_api.agents.tool_exec.ToolDefinition`
(``spec`` + ``parameters`` + ``handler``) registered onto a **fresh** registry+executor
(via :func:`~ai4ia_api.agents.tool_exec.build_tools`, so the shared app singletons are
never mutated), and the standard :func:`~ai4ia_api.agents.runtime.run_agent_turn`
re-authorizes and redacts every call.

Each tool's handler is a closure bound to exactly one server endpoint. At call time it:

1. **re-validates** the server host through the SSRF guard (DNS-rebinding defense:
   a host that resolved public at registration could later resolve internal), then
2. resolves the server's durable secret, builds the transient :class:`McpAuth`, and
3. issues a single ``tools/call`` via the injected connector, returning a bounded
   ``{"content", "isError"}`` dict the runtime feeds back to the model.

**Egress / shared-context decision.** :func:`run_agent_turn` threads ONE
:class:`~ai4ia_api.agents.tool_exec.ToolContext` (hence one ``target_hosts`` set)
through the whole turn, while the registry's egress check denies any host in
``target_hosts`` that is not in a given tool's ``egress_allowlist``. If two MCP
servers (hosts A and B) are attached and we set ``target_hosts={A, B}``, server-A's
tool (allowlist ``{A}``) would be wrongly denied because of B — a cross-denial bug.
We therefore set ``ctx.target_hosts = frozenset()`` so the registry egress check is
**skipped**, and rely on the structurally-sound real control: each handler is
closure-bound to one endpoint and re-validates that exact host via the SSRF guard at
call time, so it physically cannot egress anywhere else. Each tool keeps
``egress_allowlist={host}`` for governance/documentation, and approval gating still
applies via ``ctx.approvals``.
"""
from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from typing import Protocol

from .mcp_client import McpAuth, McpConnector
from .mcp_servers import (
    DiscoveredTool,
    UserMcpServer,
    discovered_tool_to_spec,
    is_mcp_tool_name,
    namespaced_tool_name,
)
from .ssrf import Resolver, SsrfError, validate_public_https_url
from .tool_exec import (
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolExecutor,
    build_tools,
)
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

# Per-turn cap on remote MCP tool invocations, independent of (and tighter-scoped
# than) the runtime's overall per-turn tool-call budget. Bounds how many outbound
# calls a single turn can make to user-registered servers even if the model loops.
MAX_MCP_TOOL_CALLS_PER_TURN = 8

_EMPTY_OBJECT_SCHEMA = {"type": "object", "properties": {}}


class SecretResolver(Protocol):
    """The slice of :class:`~ai4ia_api.agents.mcp_service.McpServerService` the
    execution path needs: resolve a server's durable connection secret."""

    async def secret_for(self, server: UserMcpServer) -> str | None: ...


def _make_handler(
    server: UserMcpServer,
    tool: DiscoveredTool,
    *,
    secrets: SecretResolver,
    connector: McpConnector,
    resolver: Resolver | None,
    budget: dict[str, int],
    max_calls: int,
):
    """Build the async handler for one (server, tool), closure-bound to one endpoint."""
    endpoint = server.endpoint
    tool_name = tool.name

    async def handler(args: dict, ctx: ToolContext) -> dict:
        if budget["used"] >= max_calls:
            raise ToolExecutionError("MCP tool-call budget exhausted for this turn.")
        budget["used"] += 1

        # DNS-rebinding defense: re-validate the server host at call time with the
        # same resolver discovery used. A host that resolved public at registration
        # could now resolve to an internal address.
        try:
            validate_public_https_url(endpoint, resolver=resolver)
        except SsrfError as exc:
            raise ToolExecutionError(
                f"MCP endpoint is not a permitted egress target: {exc}"
            ) from exc

        secret = await secrets.secret_for(server)
        auth = McpAuth(mode=server.authMode, secret=secret)
        result = await connector.call_tool(
            endpoint=endpoint,
            auth=auth,
            tool=tool_name,
            arguments=args or {},
        )
        return {"content": result.content, "isError": result.is_error}

    return handler


def build_mcp_tool_definitions(
    servers: Sequence[UserMcpServer],
    *,
    attached_tool_names: Collection[str],
    secrets: SecretResolver,
    connector: McpConnector,
    resolver: Resolver | None = None,
    budget: dict[str, int],
    max_calls: int = MAX_MCP_TOOL_CALLS_PER_TURN,
) -> list[ToolDefinition]:
    """Governed :class:`ToolDefinition`s for the attached, owned MCP tools.

    Only tools whose namespaced name is in ``attached_tool_names`` AND owned by the
    caller (present in ``servers``' cached ``discoveredTools``) are built — so the
    merged executor advertises exactly what the agent attached, nothing more. Names
    are de-duplicated defensively so a malformed server record can never raise on
    double registration. ``budget`` is shared across all returned handlers to cap
    total MCP calls for the turn.
    """
    attached = {n for n in attached_tool_names if is_mcp_tool_name(n)}
    if not attached:
        return []
    defs: list[ToolDefinition] = []
    seen: set[str] = set()
    for server in servers:
        for tool in server.discoveredTools:
            name = namespaced_tool_name(server.name, tool.name)
            if name not in attached or name in seen:
                continue
            seen.add(name)
            defs.append(
                ToolDefinition(
                    spec=discovered_tool_to_spec(server, tool),
                    parameters=tool.inputSchema or dict(_EMPTY_OBJECT_SCHEMA),
                    handler=_make_handler(
                        server,
                        tool,
                        secrets=secrets,
                        connector=connector,
                        resolver=resolver,
                        budget=budget,
                        max_calls=max_calls,
                    ),
                )
            )
    return defs


def trusted_tool_names(
    servers: Sequence[UserMcpServer], attached_tool_names: Collection[str]
) -> frozenset[str]:
    """Attached MCP tool names whose server is marked ``trusted``.

    These go into ``ToolContext.approvals`` so a trusted server's tools run without
    a human-approval gate (untrusted servers' tools stay approval-gated)."""
    attached = {n for n in attached_tool_names if is_mcp_tool_name(n)}
    out: set[str] = set()
    for server in servers:
        if not server.trusted:
            continue
        for tool in server.discoveredTools:
            name = namespaced_tool_name(server.name, tool.name)
            if name in attached:
                out.add(name)
    return frozenset(out)


def build_mcp_turn_tools(
    *,
    servers: Sequence[UserMcpServer],
    attached_tool_names: Collection[str],
    secrets: SecretResolver,
    connector: McpConnector,
    resolver: Resolver | None = None,
    correlation_id: str | None = None,
    approved: Collection[str] = (),
    max_calls: int = MAX_MCP_TOOL_CALLS_PER_TURN,
) -> tuple[ToolRegistry, ToolExecutor, ToolContext] | None:
    """Build a merged (registry, executor, ctx) for a turn that attaches MCP tools.

    Returns ``None`` when the agent attaches no owned MCP tools, so the caller can
    cheaply keep using the shared app singletons. When tools are present, the
    registry+executor are **fresh** (built-ins + the MCP defs) so the app singletons
    are never mutated, and the returned ctx carries:

    * ``target_hosts = frozenset()`` — skip the registry egress check (see the module
      docstring; real egress is enforced per-handler via the SSRF re-validation), and
    * ``approvals`` = trusted servers' attached tool names (plus any explicitly
      ``approved`` names supplied by the caller, e.g. a future per-turn approval UI).
    """
    budget: dict[str, int] = {"used": 0}
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=attached_tool_names,
        secrets=secrets,
        connector=connector,
        resolver=resolver,
        budget=budget,
        max_calls=max_calls,
    )
    if not defs:
        return None
    registry, executor = build_tools(extra=defs)
    approvals = trusted_tool_names(servers, attached_tool_names) | set(approved)
    ctx = ToolContext(
        approvals=frozenset(approvals),
        target_hosts=frozenset(),
        correlation_id=correlation_id,
    )
    return registry, executor, ctx
