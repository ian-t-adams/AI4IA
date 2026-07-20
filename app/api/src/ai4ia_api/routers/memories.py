"""Caller-owned memory insight and safe deletion."""
from __future__ import annotations

import time
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..memory.cosmos_store import MemoryConflictError, MemoryNotFoundError
from ..memory.models import MemoryRecord
from ..memory.telemetry import emit_memory_operation

router = APIRouter(prefix="/api/memories", tags=["memories"])


class MemoryItem(BaseModel):
    id: str
    text: str
    source: str
    sessionId: str | None = None
    documentId: str | None = None
    createdAt: str | None = None
    updatedAt: str | None = None
    version: int = 1
    etag: str | None = None
    origin: str = "implicit"
    locked: bool = False


class MemoryListResponse(BaseModel):
    status: str
    supportsCreate: bool = False
    supportsEdit: bool = False
    supportsDelete: bool = False
    items: list[MemoryItem] = Field(default_factory=list)
    detail: str | None = None


class MemoryCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)


class MemoryUpdateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)


def _item(record: MemoryRecord) -> MemoryItem:
    return MemoryItem(
        id=record.id,
        text=record.text,
        source=record.kind,
        sessionId=record.session_id,
        documentId=record.document_id,
        createdAt=record.created_at.isoformat() if record.created_at else None,
        updatedAt=record.updated_at.isoformat() if record.updated_at else None,
        version=record.version,
        etag=record.etag,
        origin=record.origin,
        locked=record.locked,
    )


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    user: AuthenticatedUser = Depends(get_current_user),
) -> MemoryListResponse:
    started = time.monotonic()
    memory = request.app.state.memory
    if not getattr(memory, "enabled", False):
        emit_memory_operation("list", "disabled", "api", started)
        return MemoryListResponse(status="disabled", detail="Memory is disabled.")
    listing = getattr(memory, "list_memories", None)
    if listing is None:
        emit_memory_operation("list", "unsupported", "api", started)
        return MemoryListResponse(
            status="unavailable",
            detail="This memory backend does not support safe enumeration.",
        )
    try:
        records = await listing(user.internal_user_id, limit=limit)
    except Exception:
        emit_memory_operation("list", "failed", "api", started)
        raise
    emit_memory_operation("list", "ok", "api", started, count=len(records))
    return MemoryListResponse(
        status="ok",
        supportsCreate=bool(
            getattr(memory, "supports_create", hasattr(memory, "create_memory"))
        ),
        supportsEdit=bool(
            getattr(memory, "supports_edit", hasattr(memory, "update_memory"))
        ),
        supportsDelete=bool(
            getattr(memory, "supports_delete", hasattr(memory, "delete_memory"))
        ),
        items=[_item(record) for record in records],
    )


@router.post("", response_model=MemoryItem, status_code=status.HTTP_201_CREATED)
async def create_memory(
    body: MemoryCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", max_length=128
    ),
    user: AuthenticatedUser = Depends(get_current_user),
) -> MemoryItem:
    started = time.monotonic()
    memory = request.app.state.memory
    creator = getattr(memory, "create_memory", None)
    if not getattr(memory, "enabled", False) or creator is None:
        emit_memory_operation("create", "unsupported", "api", started)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory is unavailable."
        )
    try:
        record = await creator(
            user.internal_user_id,
            body.text,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        emit_memory_operation("create", "invalid", "api", started)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except MemoryConflictError as exc:
        emit_memory_operation("create", "conflict", "api", started)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Memory changed concurrently; reload and try again.",
        ) from exc
    emit_memory_operation("create", "ok", "api", started, count=1)
    if record.etag:
        response.headers["ETag"] = record.etag
    return _item(record)


@router.patch("/{memory_id}", response_model=MemoryItem)
async def update_memory(
    memory_id: str,
    body: MemoryUpdateRequest,
    request: Request,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", max_length=128
    ),
    user: AuthenticatedUser = Depends(get_current_user),
) -> MemoryItem:
    started = time.monotonic()
    memory = request.app.state.memory
    updater = getattr(memory, "update_memory", None)
    if not getattr(memory, "enabled", False) or updater is None:
        emit_memory_operation("update", "unsupported", "api", started)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        )
    try:
        record = await updater(
            user.internal_user_id,
            memory_id,
            body.text,
            expected_etag=if_match,
            idempotency_key=idempotency_key,
        )
    except MemoryNotFoundError as exc:
        emit_memory_operation("update", "not_found", "api", started)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        ) from exc
    except MemoryConflictError as exc:
        emit_memory_operation("update", "conflict", "api", started)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Memory changed concurrently; reload and try again.",
        ) from exc
    except ValueError as exc:
        emit_memory_operation("update", "invalid", "api", started)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    emit_memory_operation("update", "ok", "api", started, count=1)
    if record.etag:
        response.headers["ETag"] = record.etag
    return _item(record)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: str,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", max_length=128
    ),
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    started = time.monotonic()
    memory = request.app.state.memory
    deleter = getattr(memory, "delete_memory", None)
    if not getattr(memory, "enabled", False) or deleter is None:
        emit_memory_operation("delete", "unsupported", "api", started)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        )
    try:
        options: dict[str, str] = {}
        if if_match is not None:
            options["expected_etag"] = if_match
        if idempotency_key is not None:
            options["idempotency_key"] = idempotency_key
        deleted = await deleter(user.internal_user_id, memory_id, **options)
    except MemoryNotFoundError as exc:
        emit_memory_operation("delete", "not_found", "api", started)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        ) from exc
    except MemoryConflictError as exc:
        emit_memory_operation("delete", "conflict", "api", started)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Memory changed concurrently; reload and try again.",
        ) from exc
    except Exception:
        emit_memory_operation("delete", "failed", "api", started)
        raise
    if not deleted:
        emit_memory_operation("delete", "not_found", "api", started)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"
        )
    emit_memory_operation("delete", "ok", "api", started, count=1)
