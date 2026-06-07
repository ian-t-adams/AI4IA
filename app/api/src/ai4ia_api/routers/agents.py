"""Agent catalog endpoint (curated, data-driven).

Returns the **public** projection of the agent catalog — enabled agents only,
without their server-side system prompts or tool wiring — for the frontend's
``@``-mention menu.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..agents.agent_catalog import AgentCatalog, AgentSummary

router = APIRouter(prefix="/api", tags=["agents"])


class AgentListResponse(BaseModel):
    agents: list[AgentSummary]


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> AgentListResponse:
    catalog: AgentCatalog = request.app.state.agents
    return AgentListResponse(agents=catalog.public_list())
