"""Per-turn MCP tool **execution**.

A user's remote MCP tools are discovered and projected onto the tool
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
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from . import mcp_observability as obs
from .mcp_client import McpAuth, McpConnector
from .mcp_health import is_quarantined, quarantine_reason
from .mcp_servers import (
    DiscoveredTool,
    UserMcpServer,
    discovered_tool_to_spec,
    is_mcp_tool_name,
    namespaced_tool_name,
    tool_alias,
    tool_requires_approval,
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


class HealthReporter(Protocol):
    """The slice of the service the execution path needs to report tool-call
    health, so repeated failures can quarantine a server.

    ``ok`` records a reachable server (even if the tool itself returned an error);
    a transport/connection failure records ``ok=False`` with the raised error so
    the durable failure count advances (and may quarantine). Implementations must
    persist only on a material change and must never raise.
    """

    async def record_health(
        self, server: UserMcpServer, *, ok: bool, error: object | None = None
    ) -> None: ...


@dataclass(frozen=True)
class McpPlane:
    """One independent MCP source for a turn: its servers + how to reach them.

    A *plane* bundles a set of servers with the seam used to execute their tools —
    its own ``secrets`` resolver, ``connector``, ``resolver`` and ``health``
    reporter. This exists because the two MCP sources are reached differently and
    must NOT share a credential resolver: the **BYO** plane resolves a *per-user*
    secret from Key Vault, while the **official** plane presents one *app-global*
    APIM subscription key. Since :func:`build_mcp_tool_definitions` binds a single
    ``secrets`` resolver into every handler it builds, the planes cannot be merged
    into one call — each is built with its own seam and the results combined by
    :func:`build_mcp_turn_tools_multi`.
    """

    servers: Sequence[UserMcpServer]
    secrets: SecretResolver
    connector: McpConnector
    resolver: Resolver | None = None
    health: HealthReporter | None = None


async def _safe_report(
    health: HealthReporter | None,
    server: UserMcpServer,
    *,
    ok: bool,
    error: object | None = None,
) -> None:
    """Report health best-effort: a telemetry/persistence failure never breaks a turn."""
    if health is None:
        return
    try:
        await health.record_health(server, ok=ok, error=error)
    except Exception:  # noqa: BLE001 - health reporting must never break a turn
        logger.warning("mcp health report failed", exc_info=True)


def _make_handler(
    server: UserMcpServer,
    tool: DiscoveredTool,
    *,
    alias: str,
    secrets: SecretResolver,
    connector: McpConnector,
    resolver: Resolver | None,
    budget: dict[str, int],
    max_calls: int,
    health: HealthReporter | None = None,
):
    """Build the async handler for one (server, tool), closure-bound to one endpoint.

    ``alias`` is the provider-safe, deterministic identifier for this tool (see
    :func:`~ai4ia_api.agents.mcp_servers.tool_alias`) — it is what reaches
    observability output. The *raw* ``tool.name`` advertised by the remote server
    is used only for the actual outbound ``tools/call`` dispatch, so a hostile or
    malformed remote name can dispatch correctly but can never forge a log or
    telemetry line.
    """
    endpoint = server.endpoint
    tool_name = tool.name

    async def handler(args: dict, ctx: ToolContext) -> dict:
        if budget["used"] >= max_calls:
            raise ToolExecutionError("MCP tool-call budget exhausted for this turn.")
        budget["used"] += 1

        timer = obs.Timer()
        try:
            # DNS-rebinding defense: re-validate the server host at call time with
            # the same resolver discovery used. A host that resolved public at
            # registration could now resolve to an internal address.
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
        except Exception as exc:  # noqa: BLE001 - record health, then re-raise mapped
            # A reachable host that re-validates internal, a transport failure, or a
            # protocol error: all count against the server's health so a persistently
            # failing server gets quarantined instead of retried every turn.
            await _safe_report(health, server, ok=False, error=exc)
            obs.emit(
                event=obs.EVENT_TOOL_CALL,
                server=server.name,
                host=server.host,
                tool=alias,
                outcome=obs.OUTCOME_ERROR,
                latency_ms=timer.ms,
                detail=str(exc),
            )
            raise

        # Reachable: the server connected and responded. ``isError`` is a tool-level
        # result (e.g. bad arguments), NOT a connectivity failure, so it does NOT
        # count against health — only transport failures quarantine a server.
        await _safe_report(health, server, ok=True)
        obs.emit(
            event=obs.EVENT_TOOL_CALL,
            server=server.name,
            host=server.host,
            tool=alias,
            outcome=obs.OUTCOME_TOOL_ERROR if result.is_error else obs.OUTCOME_OK,
            latency_ms=timer.ms,
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
    health: HealthReporter | None = None,
    now: datetime | None = None,
) -> list[ToolDefinition]:
    """Governed :class:`ToolDefinition`s for the attached, owned MCP tools.

    Only tools whose namespaced name is in ``attached_tool_names`` AND owned by the
    caller (present in ``servers``' cached ``discoveredTools``) are built — so the
    merged executor advertises exactly what the agent attached, nothing more. That
    attachment/ownership check is keyed by the durable, persisted governance name
    (:func:`~ai4ia_api.agents.mcp_servers.namespaced_tool_name`), which never
    changes shape. The *registered* :class:`ToolSpec` name — what the model's
    function-calling schema advertises, and the identifier that reaches
    ``ctx.approvals`` and MCP observability/telemetry — is instead the
    provider-safe deterministic alias (:func:`~ai4ia_api.agents.mcp_servers.tool_alias`):
    a remote server's raw tool name is never trusted as a schema/log-safe
    identifier, only as the dispatch argument inside the one handler that calls it.
    Aliases are de-duplicated separately from governance names (a 64-bit hash
    collision is astronomically unlikely but is rejected outright, never silently
    overwritten, if it ever occurs). ``budget`` is shared across all returned
    handlers to cap total MCP calls for the turn.

    A **quarantined** server is skipped wholesale (its tools are not built, so the
    model never sees them and the turn does not pay its connect timeout) until the
    quarantine window elapses; the skip is logged with a clear reason.
    """
    attached = {n for n in attached_tool_names if is_mcp_tool_name(n)}
    if not attached:
        return []
    defs: list[ToolDefinition] = []
    seen: set[str] = set()
    seen_aliases: set[str] = set()
    for server in servers:
        if is_quarantined(server, now=now):
            obs.emit_skip(
                server=server.name,
                host=server.host,
                reason=quarantine_reason(server, now=now) or "quarantined",
            )
            continue
        for tool in server.discoveredTools:
            name = namespaced_tool_name(server.name, tool.name)
            if name not in attached or name in seen:
                continue
            seen.add(name)
            alias = tool_alias(server.name, tool.name)
            if alias in seen_aliases:
                logger.warning(
                    "mcp tool alias collision; tool skipped for this turn",
                    extra={"server": server.name},
                )
                continue
            seen_aliases.add(alias)
            defs.append(
                ToolDefinition(
                    spec=replace(discovered_tool_to_spec(server, tool), name=alias),
                    parameters=tool.inputSchema or dict(_EMPTY_OBJECT_SCHEMA),
                    handler=_make_handler(
                        server,
                        tool,
                        alias=alias,
                        secrets=secrets,
                        connector=connector,
                        resolver=resolver,
                        budget=budget,
                        max_calls=max_calls,
                        health=health,
                    ),
                )
            )
    return defs


def auto_approved_tool_names(
    servers: Sequence[UserMcpServer], attached_tool_names: Collection[str]
) -> frozenset[str]:
    """Attached MCP tool **aliases** that run WITHOUT a human-approval gate.

    A tool is pre-approved when :func:`~ai4ia_api.agents.mcp_servers.tool_requires_approval`
    is false for it — i.e. its server is ``trusted`` (and the tool is not overridden
    to ``always``), or the tool is explicitly overridden to ``never``. The
    attachment/ownership check is keyed by the governance name (unchanged, durable
    contract), but the returned set contains each matched tool's alias, since that
    is the identifier :func:`build_mcp_tool_definitions` registers and the runtime
    compares ``ctx.approvals`` against.
    """
    attached = {n for n in attached_tool_names if is_mcp_tool_name(n)}
    out: set[str] = set()
    for server in servers:
        for tool in server.discoveredTools:
            name = namespaced_tool_name(server.name, tool.name)
            if name in attached and not tool_requires_approval(server, tool.name):
                out.add(tool_alias(server.name, tool.name))
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
    health: HealthReporter | None = None,
    now: datetime | None = None,
) -> tuple[ToolRegistry, ToolExecutor, ToolContext] | None:
    """Build a merged (registry, executor, ctx) for a turn that attaches MCP tools.

    Single-plane convenience wrapper over :func:`build_mcp_turn_tools_multi` (one
    plane built from the given seam), preserved for the common BYO-only path.

    Returns ``None`` when the agent attaches no owned MCP tools, so the caller can
    cheaply keep using the shared app singletons. When tools are present, the
    registry+executor are **fresh** (built-ins + the MCP defs) so the app singletons
    are never mutated, and the returned ctx carries:

    * ``target_hosts = frozenset()`` — skip the registry egress check (see the module
      docstring; real egress is enforced per-handler via the SSRF re-validation), and
    * ``approvals`` = pre-approved attached tool names (trusted/``never`` per
      :func:`auto_approved_tool_names`, plus any explicitly ``approved`` names
      supplied by the caller, e.g. a per-turn approval UI).

    Quarantined servers are skipped (see :func:`build_mcp_tool_definitions`); if that
    leaves no runnable tools the function returns ``None``.
    """
    return build_mcp_turn_tools_multi(
        planes=[
            McpPlane(
                servers=servers,
                secrets=secrets,
                connector=connector,
                resolver=resolver,
                health=health,
            )
        ],
        attached_tool_names=attached_tool_names,
        correlation_id=correlation_id,
        approved=approved,
        max_calls=max_calls,
        now=now,
    )


def build_mcp_turn_tools_multi(
    *,
    planes: Sequence[McpPlane],
    attached_tool_names: Collection[str],
    correlation_id: str | None = None,
    approved: Collection[str] = (),
    max_calls: int = MAX_MCP_TOOL_CALLS_PER_TURN,
    now: datetime | None = None,
) -> tuple[ToolRegistry, ToolExecutor, ToolContext] | None:
    """Merge multiple MCP planes into one turn's (registry, executor, ctx).

    Each plane is built with its **own** seam (``secrets``/``connector``/
    ``resolver``/``health``) via :func:`build_mcp_tool_definitions`, then the defs
    are unioned. Key invariants:

    * **Precedence / collision = earlier plane wins.** Tool names are de-duplicated
      across planes in order, so a name registered by an earlier plane is kept and a
      later plane's same-named tool is dropped. Callers pass the **official plane
      first** so a curated/trusted official tool can never be shadowed by a
      user's BYO server reusing its namespaced name.
    * **Shared budget.** A single :data:`MAX_MCP_TOOL_CALLS_PER_TURN`-bounded budget
      dict is threaded into every handler across all planes, so the cap is on total
      MCP calls for the turn, not per-plane.
    * **Unioned approvals.** Pre-approved names from every plane's
      :func:`auto_approved_tool_names` are unioned with the caller's ``approved``.

    Returns ``None`` when no plane contributes a runnable tool (none attached, or all
    quarantined), so the caller keeps the shared app singletons.
    """
    budget: dict[str, int] = {"used": 0}
    defs: list[ToolDefinition] = []
    seen: set[str] = set()
    approvals: set[str] = set(approved)
    for plane in planes:
        plane_defs = build_mcp_tool_definitions(
            plane.servers,
            attached_tool_names=attached_tool_names,
            secrets=plane.secrets,
            connector=plane.connector,
            resolver=plane.resolver,
            budget=budget,
            max_calls=max_calls,
            health=plane.health,
            now=now,
        )
        for d in plane_defs:
            # Earlier-plane-wins de-dup: keep the first registration of a name.
            if d.spec.name in seen:
                continue
            seen.add(d.spec.name)
            defs.append(d)
        approvals |= auto_approved_tool_names(plane.servers, attached_tool_names)
    if not defs:
        return None
    registry, executor = build_tools(extra=defs)
    ctx = ToolContext(
        approvals=frozenset(approvals),
        target_hosts=frozenset(),
        correlation_id=correlation_id,
    )
    return registry, executor, ctx
