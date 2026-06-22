"""User MCP-server management endpoints — "bring your own" tools.

``/api/agents/mcp-servers`` lets a user register, inspect, re-test, and remove
remote MCP servers whose tools they can later attach to their agents. Every
endpoint is gated by ``settings.custom_tools_enabled``: when the feature is off,
``app.state.mcp_service`` is ``None`` and the whole surface 404s, so the app's
default behavior is unchanged.

All writes translate the service's typed errors to HTTP codes. Endpoints are
strictly per-user (the caller's id scopes every read/write); a server owned by
another user is indistinguishable from one that does not exist (404).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..agents.mcp_servers import (
    McpConflictError,
    McpConnectionError,
    McpNotFoundError,
    McpValidationError,
    UserMcpServer,
    UserMcpServerCreate,
    UserMcpServerTest,
    UserMcpServerUpdate,
)
from ..agents.mcp_service import McpServerService
from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user

router = APIRouter(prefix="/api/agents/mcp-servers", tags=["mcp-servers"])


class McpServerListResponse(BaseModel):
    servers: list[UserMcpServer]


def _service(request: Request) -> McpServerService:
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        # Feature disabled: present as not-found so the surface is fully dark.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Custom tools are not enabled.",
        )
    return service


@router.get("", response_model=McpServerListResponse)
async def list_servers(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> McpServerListResponse:
    servers = await _service(request).list_for(user.internal_user_id)
    return McpServerListResponse(servers=servers)


@router.post("", response_model=UserMcpServer, status_code=status.HTTP_201_CREATED)
async def create_server(
    request: Request,
    payload: UserMcpServerCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserMcpServer:
    try:
        return await _service(request).create(user.internal_user_id, payload)
    except McpValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except McpConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except McpConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/{name}", response_model=UserMcpServer)
async def get_server(
    request: Request,
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserMcpServer:
    try:
        return await _service(request).get(user.internal_user_id, name)
    except McpNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("/{name}", response_model=UserMcpServer)
async def update_server(
    request: Request,
    name: str,
    payload: UserMcpServerUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserMcpServer:
    try:
        return await _service(request).update(user.internal_user_id, name, payload)
    except McpValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except McpNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except McpConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    request: Request,
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    await _service(request).delete(user.internal_user_id, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/test", response_model=UserMcpServer)
async def test_server(
    request: Request,
    name: str,
    payload: UserMcpServerTest | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserMcpServer:
    secret = payload.secret if payload is not None else None
    try:
        return await _service(request).test(user.internal_user_id, name, secret)
    except McpValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except McpNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except McpConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
