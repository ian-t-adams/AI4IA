"""Read-only listing of the curated **official** MCP servers — for discovery.

``/api/agents/official-mcp-servers`` lets the agent-builder UI enumerate the
admin-curated MCP servers (reached through the shared active APIM front door) and
their discovered tools, so a user can *attach* official tools to an agent the same
way they attach their own BYO tools. Unlike the BYO surface this is **read-only**:
the catalog is provisioned by operators, the credential is app-global, and no
per-user record exists to mutate.

The surface is intentionally never a hard 404 when the feature is off — it simply
returns an empty list (``app.state.official_mcp_service`` is ``None``), so the
frontend can always call it and render nothing when the plane is disabled or the
catalog is empty. No secret is ever exposed: official records carry no
``secretRef`` and the subscription key lives only inside the service.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from .mcp_servers import McpServerListResponse

router = APIRouter(
    prefix="/api/agents/official-mcp-servers", tags=["official-mcp-servers"]
)


@router.get("", response_model=McpServerListResponse)
async def list_official_servers(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> McpServerListResponse:
    """List the official MCP servers (with discovered tools) available to attach.

    Best-effort discovery is performed inside the service and cached; a server
    that has not yet discovered its tools is still returned (with an empty
    ``discoveredTools``) rather than failing the whole list.
    """
    service = getattr(request.app.state, "official_mcp_service", None)
    if service is None:
        return McpServerListResponse(servers=[])
    servers = await service.list_all()
    return McpServerListResponse(servers=servers)
