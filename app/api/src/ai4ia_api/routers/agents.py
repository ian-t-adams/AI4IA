"""Agent catalog + user-agent management endpoints.

``GET /api/agents`` returns the **public** projection (enabled agents only, no
system prompts or tool wiring) of the curated catalog *composed with the calling
user's own saved agents* — this drives the frontend's ``@``-mention menu, so a
user sees both the shared personas and their own.

``/api/agents/mine`` + POST/PUT/DELETE manage the caller's user-defined agents
(Phase 8). All write paths translate the service's typed errors to HTTP codes;
reads fail open to the curated catalog (handled in the service) so a store blip
can't break the menu.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..agents.agent_catalog import AgentCatalog, AgentSummary
from ..agents.service import AgentService
from ..agents.user_agents import (
    AgentConflictError,
    AgentNotFoundError,
    AgentValidationError,
    UserAgent,
    UserAgentCreate,
    UserAgentUpdate,
)

router = APIRouter(prefix="/api", tags=["agents"])


class AgentListResponse(BaseModel):
    agents: list[AgentSummary]


class UserAgentListResponse(BaseModel):
    agents: list[UserAgent]


def _service(request: Request) -> AgentService:
    return request.app.state.agent_service


def _curated(request: Request) -> AgentCatalog:
    return request.app.state.agents


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AgentListResponse:
    catalog = await _service(request).catalog_for(
        user.internal_user_id, _curated(request)
    )
    return AgentListResponse(agents=catalog.public_list())


@router.get("/agents/mine", response_model=UserAgentListResponse)
async def list_my_agents(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserAgentListResponse:
    agents = await _service(request).list_for(user.internal_user_id)
    return UserAgentListResponse(agents=agents)


@router.post("/agents", response_model=UserAgent, status_code=status.HTTP_201_CREATED)
async def create_my_agent(
    request: Request,
    payload: UserAgentCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserAgent:
    reserved = {a.name for a in _curated(request).agents}
    try:
        return await _service(request).create(
            user.internal_user_id, payload, reserved_names=reserved
        )
    except AgentValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except AgentConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.put("/agents/{name}", response_model=UserAgent)
async def update_my_agent(
    request: Request,
    name: str,
    payload: UserAgentUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserAgent:
    try:
        return await _service(request).update(user.internal_user_id, name, payload)
    except AgentValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except AgentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete("/agents/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_agent(
    request: Request,
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    await _service(request).delete(user.internal_user_id, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
