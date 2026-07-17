"""Display-safe, caller-aware tool governance catalog."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..agents.tool_exec import SELECTABLE_SYNTHETIC_TOOL_NAMES
from ..agents.mcp_servers import namespaced_tool_name
from ..agents.tools import ToolRisk
from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user

router = APIRouter(prefix="/api/tools", tags=["tools"])

_SYNTHETIC_DESCRIPTIONS = {
    "generate_image": "Generate an image and attach the authenticated artifact to the chat.",
    "generate_video": "Generate a video and attach the authenticated artifact to the chat.",
    "process_document": "Process a ready library document with governed document tools.",
    "recall_memory": "Recall relevant memories owned by the current user.",
}


class ToolCatalogItem(BaseModel):
    name: str
    label: str
    description: str
    source: str
    risk: ToolRisk
    requiresApproval: bool = False
    scopes: list[str] = Field(default_factory=list)
    available: bool = True
    selectable: bool = False
    detail: str | None = None
    ownership: str = "application"
    typed: bool = True
    voice: bool = False


class ToolCatalogResponse(BaseModel):
    tools: list[ToolCatalogItem]


@router.get("", response_model=ToolCatalogResponse)
async def list_tools(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ToolCatalogResponse:
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
            voice=request.app.state.tool_executor.get(spec.name) is not None,
        )
        for spec in registry.list()
    ]
    for name in sorted(SELECTABLE_SYNTHETIC_TOOL_NAMES):
        available = True
        detail = None
        if name == "recall_memory":
            available = bool(getattr(request.app.state.memory, "enabled", False))
        elif name == "process_document":
            available = getattr(request.app.state, "document_retrieval", None) is not None
        elif name == "generate_video":
            available = getattr(request.app.state, "video_artifacts", None) is not None
        elif name == "generate_image":
            available = getattr(request.app.state, "image_artifacts", None) is not None
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
                voice=False,
            )
        )
    mcp_service = getattr(request.app.state, "mcp_service", None)
    if mcp_service is not None:
        try:
            for server in await mcp_service.list_for(user.internal_user_id):
                for tool in server.discoveredTools:
                    items.append(
                        ToolCatalogItem(
                            name=namespaced_tool_name(server.name, tool.name),
                            label=tool.name.replace("_", " ").title(),
                            description=tool.description or "User MCP tool",
                            source=f"MCP: {server.name}",
                            risk=ToolRisk.external,
                            available=not bool(server.lastError),
                            selectable=True,
                            detail=server.lastError,
                            ownership="user",
                            typed=True,
                            voice=False,
                        )
                    )
        except Exception:
            pass
    official = getattr(request.app.state, "official_mcp_service", None)
    if official is not None:
        try:
            for server in await official.list_all():
                for tool in server.discoveredTools:
                    items.append(
                        ToolCatalogItem(
                            name=namespaced_tool_name(server.name, tool.name),
                            label=tool.name.replace("_", " ").title(),
                            description=tool.description or "Official MCP tool",
                            source=f"Official MCP: {server.name}",
                            risk=ToolRisk.external,
                            available=not bool(server.lastError),
                            selectable=True,
                            detail=server.lastError,
                            ownership="application",
                            typed=True,
                            voice=False,
                        )
                    )
        except Exception:
            pass
    return ToolCatalogResponse(tools=sorted(items, key=lambda item: (item.source, item.label)))
