"""Chat endpoint: resolves a deployment from the catalog, calls the model
gateway, and persists messages with cancellation-safe streaming semantics."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..catalog import ModelCatalog
from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..logging_setup import get_correlation_id
from ..sessions.models import Message, MessageRole, MessageStatus
from ..sessions.repository import SessionNotFoundError, SessionRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    sessionId: str
    content: str
    model: str | None = None
    region: str | None = None
    dataZone: str | None = None
    stream: bool = True
    params: dict = {}


def _history(messages: list[Message], system_prompt: str | None) -> list[dict]:
    out: list[dict] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    out.extend({"role": m.role.value, "content": m.content} for m in messages)
    return out


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    repo: SessionRepository = request.app.state.session_repo
    catalog: ModelCatalog = request.app.state.catalog
    gateway: ModelGatewayClient = request.app.state.gateway

    try:
        session = await repo.get_session(user.internal_user_id, body.sessionId)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    model_id = body.model or session.model
    if not model_id:
        raise HTTPException(status_code=400, detail="No model selected for this chat")
    deployment = catalog.resolve_deployment(
        model_id, region=body.region, data_zone=body.dataZone
    )
    if deployment is None:
        raise HTTPException(status_code=400, detail=f"Unknown or unavailable model: {model_id}")

    prior = await repo.list_messages(user.internal_user_id, body.sessionId)
    user_msg = Message(
        sessionId=body.sessionId,
        userId=user.internal_user_id,
        role=MessageRole.user,
        content=body.content,
        status=MessageStatus.complete,
    )
    await repo.add_message(user.internal_user_id, user_msg)

    payload_messages = _history(prior, session.systemPrompt) + [
        {"role": "user", "content": body.content}
    ]
    correlation_id = get_correlation_id()

    # Keep the session fresh + auto-title from the first user turn.
    session.updatedAt = datetime.now(timezone.utc)
    if session.title == "New chat" and not prior:
        session.title = body.content[:60]
    session.model = model_id
    await repo.update_session(session)

    if not body.stream:
        try:
            result = await gateway.complete(
                deployment=deployment.deploymentName,
                messages=payload_messages,
                params=body.params,
                correlation_id=correlation_id,
            )
        except ModelGatewayError as exc:
            raise HTTPException(status_code=502, detail=exc.detail)
        text = _extract_text(result)
        assistant = Message(
            sessionId=body.sessionId,
            userId=user.internal_user_id,
            role=MessageRole.assistant,
            content=text,
            status=MessageStatus.complete,
            model=deployment.deploymentName,
        )
        await repo.add_message(user.internal_user_id, assistant)
        return {"sessionId": body.sessionId, "message": assistant}

    # Streaming path: persist a placeholder so a record always exists, then
    # assemble server-side and upsert the final status (best-effort, shielded).
    assistant = Message(
        sessionId=body.sessionId,
        userId=user.internal_user_id,
        role=MessageRole.assistant,
        content="",
        status=MessageStatus.streaming,
        model=deployment.deploymentName,
    )
    await repo.add_message(user.internal_user_id, assistant)

    async def event_stream():
        parts: list[str] = []
        final = MessageStatus.complete
        try:
            async for chunk in gateway.stream(
                deployment=deployment.deploymentName,
                messages=payload_messages,
                params=body.params,
                correlation_id=correlation_id,
            ):
                if chunk.delta:
                    parts.append(chunk.delta)
                if chunk.done:
                    yield "data: [DONE]\n\n"
                    break
                if chunk.raw:
                    yield f"data: {chunk.raw}\n\n"
        except ModelGatewayError as exc:
            final = MessageStatus.error
            yield f"data: {json.dumps({'error': exc.detail})}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            final = MessageStatus.cancelled
            raise
        finally:
            assistant.content = "".join(parts)
            assistant.status = final
            try:
                await asyncio.shield(
                    repo.upsert_message(user.internal_user_id, assistant)
                )
            except Exception:  # noqa: BLE001 - best-effort durability
                logger.exception("Failed to persist assistant message %s", assistant.id)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _extract_text(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("content") or ""
