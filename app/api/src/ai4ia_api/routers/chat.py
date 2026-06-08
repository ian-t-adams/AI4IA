"""Chat endpoint: resolves a deployment from the catalog, calls the model
gateway, and persists messages with cancellation-safe streaming semantics.

Turns may be routed to an agent via a leading ``@mention``. Agent routing is
*per-turn*: the agent's system prompt replaces the session prompt for that turn
only (it does not mutate ``session.systemPrompt``), the ``@mention`` is stripped
from the text the model sees (and from what is stored, so it never replays into
later context), and the agent name is recorded on both the user and assistant
messages for attribution and future tracing.
"""
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
from ..sessions.models import Message, MessageRole, MessageStatus, Session
from ..sessions.repository import SessionNotFoundError, SessionRepository
from ..agents.agent_catalog import AgentCatalog, AgentSpec
from ..agents.command_service import execute_command
from ..agents.commands import parse_input
from ..agents.runtime import run_agent_turn
from ..agents.tool_exec import ToolContext, ToolExecutor
from ..agents.tools import ToolRegistry
from ..memory.service import MemoryServiceProtocol

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
    out.extend(
        {"role": m.role.value, "content": m.content}
        for m in messages
        if not m.fromCommand
    )
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_reply_response(session_id: str, assistant: Message, stream: bool):
    """Uniform response for a locally-produced reply (command / agent notice).

    Mirrors the model SSE shape so the frontend parser is unchanged: one content
    delta, then ``[DONE]``.
    """
    if not stream:
        return {"sessionId": session_id, "message": assistant}

    async def gen():
        chunk = {"choices": [{"delta": {"content": assistant.content}}]}
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _persist_local_reply(
    *,
    repo: SessionRepository,
    session: Session,
    user: AuthenticatedUser,
    user_content: str,
    reply: str,
    agent: str | None = None,
) -> Message:
    """Persist a user echo + a local assistant reply (both excluded from model
    context via ``fromCommand``) and return the assistant message."""
    uid = user.internal_user_id
    await repo.add_message(
        uid,
        Message(
            sessionId=session.id,
            userId=uid,
            role=MessageRole.user,
            content=user_content,
            status=MessageStatus.complete,
            fromCommand=True,
            agent=agent,
        ),
    )
    assistant = Message(
        sessionId=session.id,
        userId=uid,
        role=MessageRole.assistant,
        content=reply,
        status=MessageStatus.complete,
        fromCommand=True,
        agent=agent,
    )
    await repo.add_message(uid, assistant)
    session.updatedAt = _now()
    await repo.update_session(session)
    return assistant


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    repo: SessionRepository = request.app.state.session_repo
    catalog: ModelCatalog = request.app.state.catalog
    gateway: ModelGatewayClient = request.app.state.gateway
    agents: AgentCatalog = request.app.state.agents
    registry: ToolRegistry = request.app.state.tool_registry
    executor: ToolExecutor = request.app.state.tool_executor
    memory: MemoryServiceProtocol = request.app.state.memory

    try:
        session = await repo.get_session(user.internal_user_id, body.sessionId)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    parsed = parse_input(body.content)

    # Resolve an @mention to an agent BEFORE handling commands or the model, so
    # an invalid mention can never fall through to either. Disabled agents are
    # treated as unavailable.
    agent: AgentSpec | None = None
    if parsed.agent is not None:
        agent = agents.get(parsed.agent)
        if agent is None or not agent.enabled:
            assistant = await _persist_local_reply(
                repo=repo,
                session=session,
                user=user,
                user_content=parsed.raw,
                reply=(
                    f"Unknown agent: @{parsed.agent}. "
                    "Type /agents to see the agents you can mention."
                ),
            )
            return _local_reply_response(body.sessionId, assistant, body.stream)

    # Slash commands (/help, /clear, /system, /model, /agents, ...) are handled
    # locally and never reach a model. A command takes precedence over an agent
    # mention (e.g. "@coder /help" runs /help); the mention was already
    # validated above.
    if parsed.is_command:
        assistant = await execute_command(
            parsed=parsed,
            session=session,
            user=user,
            repo=repo,
            catalog=catalog,
            agents=agents,
            memory=memory,
        )
        return _local_reply_response(body.sessionId, assistant, body.stream)

    # Determine the system prompt, model, and the content the model actually
    # sees. For an agent turn the persona prompt replaces the session prompt
    # (this turn only) and the mention is stripped from the text.
    if agent is not None:
        content_for_model = parsed.text
        if not content_for_model:
            assistant = await _persist_local_reply(
                repo=repo,
                session=session,
                user=user,
                user_content=parsed.raw,
                reply=(
                    f"You mentioned @{agent.name} but didn't include a message. "
                    "What would you like to ask?"
                ),
                agent=agent.name,
            )
            return _local_reply_response(body.sessionId, assistant, body.stream)
        system_prompt = agent.systemPrompt
        # Precedence: explicit body model > session's standing model > agent's
        # preferred model. The agent default is a per-turn fallback only and is
        # never written back to the session.
        model_id = body.model or session.model or agent.defaultModel
        model_from_agent_default = (
            not body.model and not session.model and agent.defaultModel is not None
        )
        agent_name: str | None = agent.name
    else:
        content_for_model = body.content
        system_prompt = session.systemPrompt
        model_id = body.model or session.model
        model_from_agent_default = False
        agent_name = None

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
        content=content_for_model,
        status=MessageStatus.complete,
        agent=agent_name,
    )
    await repo.add_message(user.internal_user_id, user_msg)

    payload_messages = _history(prior, system_prompt) + [
        {"role": "user", "content": content_for_model}
    ]
    correlation_id = get_correlation_id()

    # Per-user memory recall (best-effort, feature-flagged). Injected as a clearly
    # delimited, explicitly-untrusted context block placed AFTER the main system
    # prompt so the agent/session instructions keep top authority. ``recall`` runs
    # on the prior-history snapshot (the current user message was added to the
    # store, not the recall index, so it can't recall itself).
    recalled = await memory.recall(user.internal_user_id, content_for_model)
    memory_block = memory.format_context(recalled)
    if memory_block:
        insert_at = 1 if (payload_messages and payload_messages[0]["role"] == "system") else 0
        payload_messages.insert(insert_at, {"role": "system", "content": memory_block})

    # Keep the session fresh + auto-title from the first real (non-command) turn.
    has_prior_chat = any(not m.fromCommand for m in prior)
    session.updatedAt = datetime.now(timezone.utc)
    if session.title == "New chat" and not has_prior_chat:
        session.title = content_for_model[:60]
    # Persist the model choice to the session unless it came purely from the
    # agent's per-turn default (which must not silently rebind the session).
    if not model_from_agent_default:
        session.model = model_id
    await repo.update_session(session)

    # Tool-enabled agent turn: run the gateway-native tool-calling loop governed
    # by the tool-safety registry. The model picks/sequences tools; we authorize
    # and execute each call. This path is non-streaming internally; the resolved
    # final answer is returned via the standard reply shape (a single SSE delta
    # when streaming). Agents without tools fall through to the direct model path
    # below, which keeps true token streaming.
    if agent is not None and agent.tools:
        ctx = ToolContext(correlation_id=correlation_id)
        run = await run_agent_turn(
            deployment=deployment.deploymentName,
            messages=payload_messages,
            tool_names=agent.tools,
            gateway=gateway,
            registry=registry,
            executor=executor,
            ctx=ctx,
            params=body.params,
        )
        assistant = Message(
            sessionId=body.sessionId,
            userId=user.internal_user_id,
            role=MessageRole.assistant,
            content=run.text,
            status=MessageStatus.complete,
            model=deployment.deploymentName,
            agent=agent_name,
        )
        await repo.add_message(user.internal_user_id, assistant)
        await memory.remember(user.internal_user_id, body.sessionId, content_for_model)
        return _local_reply_response(body.sessionId, assistant, body.stream)

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
            agent=agent_name,
        )
        await repo.add_message(user.internal_user_id, assistant)
        await memory.remember(user.internal_user_id, body.sessionId, content_for_model)
        return {"sessionId": body.sessionId, "message": assistant}

    # Streaming path: persist a placeholder so a record always exists, then
    # assemble server-side and upsert the final status (best-effort, shielded).
    # The agent attribution is set on the placeholder so a cancelled/errored
    # turn keeps it.
    assistant = Message(
        sessionId=body.sessionId,
        userId=user.internal_user_id,
        role=MessageRole.assistant,
        content="",
        status=MessageStatus.streaming,
        model=deployment.deploymentName,
        agent=agent_name,
    )
    await repo.add_message(user.internal_user_id, assistant)

    async def event_stream():
        parts: list[str] = []
        final = MessageStatus.complete
        saw_done = False
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
                    saw_done = True
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
            # Remember the user's turn only when the model stream completed
            # cleanly (a clean end-of-stream marker), so a truncated or errored
            # turn doesn't seed memory. Best-effort: remember() swallows its own
            # failures. (A durable store will move this off the response path.)
            if final == MessageStatus.complete and saw_done:
                await asyncio.shield(
                    memory.remember(
                        user.internal_user_id, body.sessionId, content_for_model
                    )
                )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _extract_text(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("content") or ""
