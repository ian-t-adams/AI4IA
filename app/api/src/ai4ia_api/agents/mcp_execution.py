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

**Approval posture.** Every tool this module builds is ``external`` risk, so the
turn builders default to :attr:`~ai4ia_api.agents.approvals.ApprovalPolicy.always`:
each call additionally needs a fresh, per-invocation human approval bound to its
exact arguments (see :mod:`ai4ia_api.agents.approvals`). Marking a server *trusted*
or a tool ``requireApproval: never`` still removes the *standing* gate — which is
what decides whether the model is offered the tool at all — but no longer decides
what leaves the network, because that posture is exactly what an indirect prompt
injection would otherwise inherit. Callers may relax this per deployment
(``AI4IA_TOOL_APPROVAL_MODE``), but they must do so deliberately: the default here
is secure, so a future call site that forgets the argument gets the safe behavior.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from . import mcp_observability as obs
from .approvals import ApprovalPolicy, ApprovalSink
from .consent import ConsentChecker, ConsentRejected, contract_hash, tool_contract_hash
from .mcp_client import McpAuth, McpConnector
from .mcp_health import is_quarantined, quarantine_reason
from .mcp_servers import (
    DiscoveredTool,
    UserMcpServer,
    discovered_tool_to_spec,
    is_mcp_tool_name,
    is_valid_remote_tool_name,
    namespaced_tool_name,
    tool_alias,
)
from .ssrf import (
    DnsCapacityError,
    Resolver,
    SsrfError,
    async_validate_public_https_url,
)
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
    # Stable source identity (for example ``official`` or ``byo``). It is part
    # of every alias and approval binding, preventing one plane's accepted
    # definition from authorizing a colliding definition on another plane.
    plane_id: str = "default"
    resolver: Resolver | None = None
    health: HealthReporter | None = None
    current_server: Callable[[str], Awaitable[UserMcpServer | None]] | None = None


def mcp_contract_metadata(server: UserMcpServer, tool: DiscoveredTool) -> dict:
    """No credentials: only execution-affecting identity and configuration."""
    return {
        "server": server.name, "owner": server.userId, "endpoint": server.endpoint,
        "host": server.host, "transport": server.transport.value,
        "authMode": server.authMode.value, "revision": server.configurationRevision,
        "credentialRefHash": contract_hash(server.secretRef),
        "rawName": tool.raw_name, "trusted": server.trusted, "enabled": server.enabled,
        "approval": server.toolApprovals.get(tool.name),
    }


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
    current_server: Callable[[str], Awaitable[UserMcpServer | None]] | None = None,
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
    raw_tool_name = tool.raw_name
    metadata = mcp_contract_metadata(server, tool)
    spec = replace(discovered_tool_to_spec(server, tool), name=alias)
    implemented_contract = tool_contract_hash(
        spec, tool.inputSchema or dict(_EMPTY_OBJECT_SCHEMA), metadata=metadata
    )

    async def handler(args: dict, ctx: ToolContext) -> dict:
        if current_server is not None:
            current = await current_server(server.name)
            live_tool = next(
                (item for item in current.discoveredTools if item.name == tool.name), None
            ) if current is not None else None
            if (
                current is None or not current.enabled or is_quarantined(current)
                or live_tool is None
                or contract_hash(mcp_contract_metadata(current, live_tool)) != contract_hash(metadata)
                or live_tool.inputSchema != tool.inputSchema
                or live_tool.description != tool.description
            ):
                raise ToolExecutionError("MCP configuration changed; refresh and renew approval.")
        if budget["used"] >= max_calls:
            raise ToolExecutionError("MCP tool-call budget exhausted for this turn.")
        budget["used"] += 1

        timer = obs.Timer()
        try:
            # DNS-rebinding defense: re-validate the server host at call time with
            # the same resolver discovery used. A host that resolved public at
            # registration could now resolve to an internal address.
            try:
                await async_validate_public_https_url(endpoint, resolver=resolver)
            except SsrfError as exc:
                raise ToolExecutionError(
                    f"MCP endpoint is not a permitted egress target: {exc}"
                ) from exc

            secret = await secrets.secret_for(server)
            auth = McpAuth(mode=server.authMode, secret=secret)
            if ctx.consent_checker is not None:
                decision = await ctx.consent_checker(alias, implemented_contract)
                if decision.reason is not None and decision.reason != "consent_not_granted":
                    raise ConsentRejected(decision.reason)
            result = await connector.call_tool(
                endpoint=endpoint,
                auth=auth,
                tool=raw_tool_name,
                arguments=args or {},
            )
        except ConsentRejected:
            raise
        except DnsCapacityError as exc:
            obs.emit(
                event=obs.EVENT_TOOL_CALL,
                server=server.name,
                host=server.host,
                tool=alias,
                outcome=obs.OUTCOME_ERROR,
                latency_ms=timer.ms,
                detail="local_dns_capacity",
            )
            raise ToolExecutionError(
                "MCP DNS capacity is temporarily unavailable; retry the tool call."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - record health, then re-raise mapped
            # A reachable host that re-validates internal, a transport failure, or a
            # protocol error: all count against the server's health so a persistently
            # failing server gets quarantined instead of retried every turn.
            category = (
                "egress_blocked" if isinstance(exc, (SsrfError, ToolExecutionError))
                else "transport_error"
            )
            await _safe_report(health, server, ok=False, error=category)
            obs.emit(
                event=obs.EVENT_TOOL_CALL,
                server=server.name,
                host=server.host,
                tool=alias,
                outcome=obs.OUTCOME_ERROR,
                latency_ms=timer.ms,
                detail=category,
            )
            raise ToolExecutionError("MCP tool execution failed.") from exc

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


@dataclass(frozen=True)
class McpToolBinding:
    """Exact governance/model/dispatch identities for one accepted definition."""

    governance_name: str
    alias: str
    raw_name: str
    plane_id: str
    definition: ToolDefinition
    auto_approved: bool


def _alias_for(plane_id: str, server_name: str, raw_name: str) -> str:
    """Keep the legacy/default plane stable while binding named planes."""
    if plane_id == "default":
        return tool_alias(server_name, raw_name)
    return tool_alias(server_name, raw_name, plane=plane_id)


def _build_mcp_tool_bindings(
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
    plane_id: str = "default",
    current_server: Callable[[str], Awaitable[UserMcpServer | None]] | None = None,
) -> list[McpToolBinding]:
    """Build exact bindings for attached, owned MCP tools in one plane.

    A binding keeps the durable selected governance name, provider-safe alias,
    and exact accepted raw dispatch name separate. Invalid legacy names,
    duplicate servers/tools, and alias collisions are rejected locally; no
    dictionary overwrite can silently retarget a tool.
    """
    attached = {n for n in attached_tool_names if is_mcp_tool_name(n)}
    if not attached:
        return []
    bindings: list[McpToolBinding] = []
    seen_servers: set[str] = set()
    seen_governance: set[str] = set()
    seen_aliases: set[str] = set()
    for server in servers:
        if server.name in seen_servers:
            logger.warning(
                "duplicate mcp server rejected for this turn",
                extra={"server": server.name, "plane": plane_id},
            )
            continue
        seen_servers.add(server.name)
        if is_quarantined(server, now=now):
            obs.emit_skip(
                server=server.name,
                host=server.host,
                reason=quarantine_reason(server, now=now) or "quarantined",
            )
            continue
        for tool in server.discoveredTools:
            raw_name = tool.raw_name
            if not is_valid_remote_tool_name(tool.name) or not is_valid_remote_tool_name(
                raw_name
            ):
                logger.warning(
                    "invalid mcp tool definition rejected for this turn",
                    extra={"server": server.name, "plane": plane_id},
                )
                continue
            governance_name = namespaced_tool_name(server.name, tool.name)
            if governance_name not in attached:
                continue
            if governance_name in seen_governance:
                logger.warning(
                    "duplicate mcp tool rejected for this turn",
                    extra={"server": server.name, "plane": plane_id},
                )
                continue
            seen_governance.add(governance_name)
            alias = _alias_for(plane_id, server.name, raw_name)
            if alias in seen_aliases:
                logger.warning(
                    "mcp tool alias collision rejected for this turn",
                    extra={"server": server.name, "plane": plane_id},
                )
                continue
            seen_aliases.add(alias)
            definition = ToolDefinition(
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
                    current_server=current_server,
                ),
                consent_metadata=mcp_contract_metadata(server, tool),
            )
            bindings.append(
                McpToolBinding(
                    governance_name=governance_name,
                    alias=alias,
                    raw_name=raw_name,
                    plane_id=plane_id,
                    definition=definition,
                    auto_approved=not definition.spec.needs_approval,
                )
            )
    return bindings


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
    plane_id: str = "default",
    current_server: Callable[[str], Awaitable[UserMcpServer | None]] | None = None,
) -> list[ToolDefinition]:
    """Governed definitions for one plane; identity mapping stays internal."""
    return [
        binding.definition
        for binding in _build_mcp_tool_bindings(
            servers,
            attached_tool_names=attached_tool_names,
            secrets=secrets,
            connector=connector,
            resolver=resolver,
            budget=budget,
            max_calls=max_calls,
            health=health,
            now=now,
            plane_id=plane_id,
            current_server=current_server,
        )
    ]


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
    approval_policy: ApprovalPolicy = ApprovalPolicy.always,
    untrusted_context: bool = False,
    invocation_approvals: Collection[str] = (),
    approval_sink: ApprovalSink | None = None,
    consent_checker: ConsentChecker | None = None,
) -> tuple[ToolRegistry, ToolExecutor, ToolContext] | None:
    """Build a merged (registry, executor, ctx) for a turn that attaches MCP tools.

    Single-plane convenience wrapper over :func:`build_mcp_turn_tools_multi` (one
    plane built from the given seam). **No production caller** -- ``routers/chat.py``
    calls ``build_mcp_turn_tools_multi`` directly; this is exercised only by
    ``tests/test_mcp_execution.py``, which uses it as the readable single-plane
    entry point for the shared building logic. Kept deliberately rather than
    deleted so that MCP approval/egress coverage is not rewritten in place; do not
    assume it is on a live request path.

    Returns ``None`` when the agent attaches no owned MCP tools, so the caller can
    cheaply keep using the shared app singletons. When tools are present, the
    registry+executor are **fresh** (built-ins + the MCP defs) so the app singletons
    are never mutated, and the returned ctx carries:

    * ``target_hosts = frozenset()`` — skip the registry egress check (see the module
      docstring; real egress is enforced per-handler via the SSRF re-validation), and
    * ``approvals`` = standing discovery grants for attached tool aliases (each
      binding's ``auto_approved`` flag — trusted server without a per-tool
      ``always`` override, or an explicit per-tool ``never`` override; see
      :func:`_build_mcp_tool_bindings`), plus any explicitly ``approved`` names
      supplied by the caller.

    Note that ``approvals`` is now only half the story: when ``approval_policy``
    is not ``off``, the runtime *additionally* requires a per-invocation approval
    bound to the exact arguments (see :mod:`ai4ia_api.agents.approvals`). A
    standing approval only decides whether the model is shown the tool.

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
        approval_policy=approval_policy,
        untrusted_context=untrusted_context,
        invocation_approvals=invocation_approvals,
        approval_sink=approval_sink,
        consent_checker=consent_checker,
    )


def build_mcp_turn_tools_multi(
    *,
    planes: Sequence[McpPlane],
    attached_tool_names: Collection[str],
    correlation_id: str | None = None,
    approved: Collection[str] = (),
    max_calls: int = MAX_MCP_TOOL_CALLS_PER_TURN,
    now: datetime | None = None,
    approval_policy: ApprovalPolicy = ApprovalPolicy.always,
    untrusted_context: bool = False,
    invocation_approvals: Collection[str] = (),
    approval_sink: ApprovalSink | None = None,
    consent_checker: ConsentChecker | None = None,
    extra_definitions: Sequence[ToolDefinition] = (),
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
    * **Unioned approvals.** Each accepted binding's ``auto_approved`` flag (see
      :func:`_build_mcp_tool_bindings`) is unioned across every plane with the
      caller's ``approved``. This is the **standing** set only: it decides what the
      model is offered. What actually *runs* is additionally gated per invocation
      by ``approval_policy`` + ``invocation_approvals`` (see
      :mod:`ai4ia_api.agents.approvals`), which is bound to the exact arguments and
      therefore cannot be satisfied by a server merely being marked *trusted*.

    Returns ``None`` when no plane contributes a runnable tool (none attached, or all
    quarantined), so the caller keeps the shared app singletons.
    """
    budget: dict[str, int] = {"used": 0}
    defs: list[ToolDefinition] = list(extra_definitions)
    aliases: dict[str, str] = {}
    seen_planes: set[str] = set()
    seen_governance: set[str] = set()
    seen_aliases: set[str] = set()
    explicit_approvals = set(approved)
    # Preserve non-MCP approvals for built-ins. MCP approvals are translated
    # only after an exact definition+plane binding has been accepted.
    approvals: set[str] = {
        name for name in explicit_approvals if not is_mcp_tool_name(name)
    }
    for plane in planes:
        if plane.plane_id != "default" and plane.plane_id in seen_planes:
            logger.warning(
                "duplicate mcp plane rejected for this turn",
                extra={"plane": plane.plane_id},
            )
            continue
        if plane.plane_id != "default":
            seen_planes.add(plane.plane_id)
        plane_bindings = _build_mcp_tool_bindings(
            plane.servers,
            attached_tool_names=attached_tool_names,
            secrets=plane.secrets,
            connector=plane.connector,
            resolver=plane.resolver,
            budget=budget,
            max_calls=max_calls,
            health=plane.health,
            now=now,
            plane_id=plane.plane_id,
            current_server=plane.current_server,
        )
        for binding in plane_bindings:
            # Earlier accepted plane wins a governance-name collision. A
            # quarantined plane contributes no binding and therefore no
            # approval, so a later BYO collision is governed by its own spec.
            if binding.governance_name in seen_governance:
                continue
            if binding.alias in seen_aliases:
                logger.warning(
                    "cross-plane mcp alias collision rejected for this turn",
                    extra={"plane": plane.plane_id},
                )
                continue
            seen_governance.add(binding.governance_name)
            seen_aliases.add(binding.alias)
            aliases[binding.governance_name] = binding.alias
            defs.append(binding.definition)
            if (
                binding.auto_approved
                or binding.governance_name in explicit_approvals
                or binding.alias in explicit_approvals
            ):
                approvals.add(binding.alias)
    if not defs:
        return None
    registry, executor = build_tools(extra=defs)
    ctx = ToolContext(
        approvals=frozenset(approvals),
        target_hosts=frozenset(),
        correlation_id=correlation_id,
        tool_aliases=aliases,
        approval_policy=approval_policy,
        untrusted_context=untrusted_context,
        invocation_approvals=frozenset(invocation_approvals),
        approval_sink=approval_sink,
        consent_checker=consent_checker,
    )
    return registry, executor, ctx
