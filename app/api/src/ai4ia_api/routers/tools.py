"""Display-safe, caller-aware tool governance catalog."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..agents.tool_exec import SELECTABLE_SYNTHETIC_TOOL_NAMES
from ..agents.mcp_servers import namespaced_tool_name
from ..agents.tools import ToolRisk
from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..conversations.policy import resolve_conversation_policy
from ..sessions.repository import SessionNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools", tags=["tools"])

_SYNTHETIC_DESCRIPTIONS = {
    "generate_image": "Generate an image and attach the authenticated artifact to the chat.",
    "generate_video": "Generate a video and attach the authenticated artifact to the chat.",
    "process_document": "Process a ready library document with governed document tools.",
    "recall_memory": "Recall relevant memories owned by the current user.",
    "remember_memory": "Save a short, durable fact to the current user's own memory.",
    "run_workflow": "Run one of the current user's saved safe workflows.",
}


class ToolCatalogItem(BaseModel):
    name: str
    label: str
    description: str
    source: str
    risk: ToolRisk | None = None
    requiresApproval: bool | None = None
    scopes: list[str] | None = None
    available: bool = True
    selectable: bool = False
    detail: str | None = None
    ownership: str = "application"
    typed: bool | None = None
    voice: bool | None = None


class ToolCatalogResponse(BaseModel):
    tools: list[ToolCatalogItem]
    inheritedTools: list[str] = []


@router.get("", response_model=ToolCatalogResponse)
async def list_tools(
    request: Request,
    session_id: str | None = Query(default=None, alias="sessionId"),
    agent_name: str | None = Query(default=None, alias="agentName"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ToolCatalogResponse:
    if session_id and agent_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Choose either a session or an agent preview, not both.",
        )
    registry = request.app.state.tool_registry
    selectable = request.app.state.agent_service.attachable_tools
    items = [
        ToolCatalogItem(
            name=spec.name,
            label=spec.name.replace("_", " ").title(),
            description=spec.description,
            source="built-in",
            risk=spec.risk,
            requiresApproval=spec.needs_approval,
            scopes=sorted(spec.scopes),
            available=spec.enabled and registry.is_allowlisted(spec.name),
            selectable=spec.name in selectable,
            typed=True,
            voice=request.app.state.tool_executor.get(spec.name) is not None,
        )
        for spec in registry.list()
    ]
    for name in sorted(SELECTABLE_SYNTHETIC_TOOL_NAMES):
        available = True
        detail = None
        if name == "recall_memory":
            available = bool(getattr(request.app.state.memory, "enabled", False))
        elif name == "remember_memory":
            available = bool(getattr(request.app.state.memory, "enabled", False))
        elif name == "process_document":
            available = getattr(request.app.state, "document_retrieval", None) is not None
        elif name == "generate_video":
            available = getattr(request.app.state, "video_artifacts", None) is not None
        elif name == "generate_image":
            available = getattr(request.app.state, "image_artifacts", None) is not None
        elif name == "run_workflow":
            available = getattr(request.app.state, "workflow_service", None) is not None
        if not available:
            detail = "The required server feature is not enabled."
        items.append(
            ToolCatalogItem(
                name=name,
                label=name.replace("_", " ").title(),
                description=_SYNTHETIC_DESCRIPTIONS[name],
                source="capability",
                risk=ToolRisk.safe,
                available=available,
                selectable=name in selectable,
                detail=detail,
                typed=True,
                voice=False,
            )
        )
    mcp_service = getattr(request.app.state, "mcp_service", None)
    if mcp_service is not None:
        try:
            for server in await mcp_service.list_for(user.internal_user_id):
                specs = {spec.name: spec for spec in server.tool_specs()}
                for tool in server.discoveredTools:
                    name = namespaced_tool_name(server.name, tool.name)
                    spec = specs[name]
                    items.append(
                        ToolCatalogItem(
                            name=name,
                            label=tool.name.replace("_", " ").title(),
                            description=tool.description or "User MCP tool",
                            source=f"MCP: {server.name}",
                            risk=spec.risk,
                            requiresApproval=spec.needs_approval,
                            scopes=sorted(spec.scopes),
                            available=not bool(server.lastError),
                            selectable=True,
                            detail=server.lastError,
                            ownership="user",
                            typed=True,
                            voice=False,
                        )
                    )
        except Exception:
            # Never let one broken catalog blank the whole tool list — but do not
            # drop it silently either. A bare ``pass`` here made a user's MCP tools
            # vanish from the UI with no trace, so the user concluded they had never
            # been configured. Log for the operator AND surface an unavailable row,
            # mirroring the "governance metadata is unavailable" rows below.
            logger.warning("user MCP tool catalog listing failed", exc_info=True)
            items.append(
                ToolCatalogItem(
                    name="mcp:user:unavailable",
                    label="Your MCP tools",
                    description="Your MCP servers could not be listed just now.",
                    source="MCP",
                    available=False,
                    selectable=False,
                    detail="The server could not read your MCP servers. Existing tools are unaffected; retry shortly.",
                    ownership="user",
                )
            )
    official = getattr(request.app.state, "official_mcp_service", None)
    if official is not None:
        try:
            for server in await official.list_all():
                specs = {spec.name: spec for spec in server.tool_specs()}
                for tool in server.discoveredTools:
                    name = namespaced_tool_name(server.name, tool.name)
                    spec = specs[name]
                    items.append(
                        ToolCatalogItem(
                            name=name,
                            label=tool.name.replace("_", " ").title(),
                            description=tool.description or "Official MCP tool",
                            source=f"Official MCP: {server.name}",
                            risk=spec.risk,
                            requiresApproval=spec.needs_approval,
                            scopes=sorted(spec.scopes),
                            available=not bool(server.lastError),
                            selectable=True,
                            detail=server.lastError,
                            ownership="application",
                            typed=True,
                            voice=False,
                        )
                    )
        except Exception:
            # Same reasoning as the user-MCP catalog above: log for the operator and
            # show the degradation instead of silently returning a short list.
            logger.warning("official MCP tool catalog listing failed", exc_info=True)
            items.append(
                ToolCatalogItem(
                    name="mcp:official:unavailable",
                    label="Official MCP tools",
                    description="The official MCP catalog could not be listed just now.",
                    source="Official MCP",
                    available=False,
                    selectable=False,
                    detail="The server could not read the official MCP catalog. Retry shortly.",
                    ownership="application",
                )
            )
    inherited_tools: tuple[str, ...] = ()
    if session_id:
        try:
            session = await request.app.state.session_repo.get_session(
                user.internal_user_id, session_id
            )
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
            )
        policy = await resolve_conversation_policy(
            request.app.state, user.internal_user_id, session
        )
        inherited_tools = policy.inherited_tools
        known = {item.name for item in items}
        for name in sorted(set(policy.effective_tools) - known):
            items.append(
                ToolCatalogItem(
                    name=name,
                    label=name,
                    description="Governance metadata is unavailable for this effective tool.",
                    source="unknown",
                    available=False,
                    selectable=False,
                    detail="The server could not resolve authoritative tool metadata.",
                    ownership="unknown",
                )
            )
    elif agent_name:
        catalog = await request.app.state.agent_service.catalog_for(
            user.internal_user_id, request.app.state.agents
        )
        agent = catalog.get(agent_name)
        if agent is None or not agent.enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The selected agent is unavailable.",
            )
        inherited_tools = tuple(dict.fromkeys(agent.tools))
        known = {item.name for item in items}
        for name in sorted(set(inherited_tools) - known):
            items.append(
                ToolCatalogItem(
                    name=name,
                    label=name,
                    description="Governance metadata is unavailable for this inherited tool.",
                    source="unknown",
                    available=False,
                    selectable=False,
                    detail="The server could not resolve authoritative tool metadata.",
                    ownership="unknown",
                )
            )
    return ToolCatalogResponse(
        tools=sorted(items, key=lambda item: (item.source, item.label)),
        inheritedTools=list(inherited_tools),
    )
