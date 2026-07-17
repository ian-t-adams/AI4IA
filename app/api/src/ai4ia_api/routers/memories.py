"""Caller-owned memory insight and safe deletion."""
from __future__ import annotations

import time
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..logging_setup import emit_custom_event

router = APIRouter(prefix="/api/memories", tags=["memories"])


class MemoryItem(BaseModel):
    id: str
    text: str
    source: str
    sessionId: str | None = None
    documentId: str | None = None
    createdAt: str | None = None


class MemoryListResponse(BaseModel):
    status: str
    supportsDelete: bool = False
    items: list[MemoryItem] = Field(default_factory=list)
    detail: str | None = None


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    user: AuthenticatedUser = Depends(get_current_user),
) -> MemoryListResponse:
    started = time.monotonic()
    memory = request.app.state.memory
    if not getattr(memory, "enabled", False):
        return MemoryListResponse(status="disabled", detail="Memory is disabled.")
    listing = getattr(memory, "list_memories", None)
    if listing is None:
        return MemoryListResponse(
            status="unavailable",
            detail="This memory backend does not support safe enumeration.",
        )
    records = await listing(user.internal_user_id, limit=limit)
    emit_custom_event(
        "memory_list",
        {
            "status": "ok",
            "count": len(records),
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
    return MemoryListResponse(
        status="ok",
        supportsDelete=hasattr(memory, "delete_memory"),
        items=[
            MemoryItem(
                id=record.id,
                text=record.text,
                source=record.kind,
                sessionId=record.session_id,
                documentId=record.document_id,
                createdAt=record.created_at.isoformat() if record.created_at else None,
            )
            for record in records
        ],
    )


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    started = time.monotonic()
    memory = request.app.state.memory
    deleter = getattr(memory, "delete_memory", None)
    if not getattr(memory, "enabled", False) or deleter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        )
    if not await deleter(user.internal_user_id, memory_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        )
    emit_custom_event(
        "memory_delete",
        {
            "status": "ok",
            "latencyMs": int((time.monotonic() - started) * 1000),
        },
    )
