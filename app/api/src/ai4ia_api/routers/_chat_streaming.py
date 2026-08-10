"""Streaming lifecycle and terminal persistence for the chat route."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

from ..agents.activity import persisted_trace, serialize_step
from ..agents.approvals import (
    ApprovalDraft,
    PendingToolApproval,
    mint_pending_approval,
)
from ..agents.runtime import AgentRunFailed, AgentRunResult, AgentStep
from ..auth.base import AuthenticatedUser
from ..catalog import DeploymentOption
from ..citations import RetrievedSource, attest_message
from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..memory.service import MemoryServiceProtocol
from ..safety import MessageSafety, merge_safety
from ..sessions.models import (
    ActivityStep,
    Message,
    MessageAttachment,
    MessageRole,
    MessageStatus,
)
from ..sessions.repository import SessionRepository
from ..usage.models import TokenUsage
from ..usage.service import UsageService

logger = logging.getLogger(__name__)

CHAT_COMPLETION_FAILED = "Chat completion failed."
AGENT_EVENT_QUEUE_MAXSIZE = 32


def _stream_metadata(
    user_message_id: str | None,
    assistant_message_id: str,
    sources: list[RetrievedSource] | None = None,
) -> str:
    metadata: dict[str, object] = {
        "userMessageId": user_message_id,
        "assistantMessageId": assistant_message_id,
    }
    # The span registry is minted before the model runs, so it can ride the very
    # first frame: the browser can then mark citations as they stream in rather
    # than showing raw tokens until the durable row is refetched. Omitted
    # entirely on an unattested turn, which keeps the base frame byte-identical
    # to what it has always been.
    if sources is not None:
        metadata["sources"] = [s.model_dump(mode="json") for s in sources]
    return f"data: {json.dumps({'metadata': metadata})}\n\n"


def _has_gateway_stream_error(raw: str) -> bool:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("error"))


def _mint_approval_events(
    assistant: Message, drafts: Sequence[ApprovalDraft]
) -> list[dict[str, object]]:
    """Attach durable pending-approval records to ``assistant`` and return the
    client payloads (record + its one-time grant).

    Splitting mint from persist is the point: the record that lands in Cosmos
    carries only ``grantHash``, while the grant itself exists solely in the
    payload returned to the caller, once. Nothing else can reconstruct it.
    """
    if not drafts:
        return []
    records: list[PendingToolApproval] = []
    events: list[dict[str, object]] = []
    for draft in drafts:
        record, grant = mint_pending_approval(draft)
        records.append(record)
        events.append({**record.model_dump(mode="json"), "grant": grant})
    assistant.pendingApprovals = records
    return events


async def _persist_terminal_assistant(
    repo: SessionRepository,
    user_id: str,
    assistant: Message,
) -> bool:
    # Single choke point for every streaming terminal write, which is why the
    # citation attestation happens here: the answer's text is final, and both the
    # agentic and plain streaming generators funnel through this one call rather
    # than each remembering to attest. Idempotent and a no-op on an unattested
    # turn (see ai4ia_api.citations).
    attest_message(assistant)
    write = asyncio.create_task(repo.upsert_message(user_id, assistant))
    try:
        await asyncio.shield(write)
        return True
    except asyncio.CancelledError:
        try:
            await write
        except Exception:  # noqa: BLE001 - cancellation path retries terminal state
            logger.exception("Failed to persist assistant message %s", assistant.id)
        raise
    except Exception:  # noqa: BLE001 - converted to an explicit stream failure
        logger.exception("Failed to persist assistant message %s", assistant.id)
        return False


async def _persist_nonstream_failure(
    *,
    repo: SessionRepository,
    metering: UsageService,
    user: AuthenticatedUser,
    session_id: str,
    model_id: str,
    deployment: DeploymentOption,
    agent_name: str | None,
    correlation_id: str,
    usage: TokenUsage,
    content: str = "",
    steps: list[ActivityStep] | None = None,
    sources: list[RetrievedSource] | None = None,
    attachments: list[MessageAttachment] | None = None,
) -> Message:
    """Persist and meter a terminal error for an accepted non-streaming turn."""
    assistant = Message(
        sessionId=session_id,
        userId=user.internal_user_id,
        role=MessageRole.assistant,
        content=content,
        status=MessageStatus.error,
        model=deployment.deploymentName,
        agent=agent_name,
        attachments=attachments or [],
        steps=steps,
        sources=sources,
    )
    attest_message(assistant)
    await repo.add_message(user.internal_user_id, assistant)
    await metering.record_completion(
        user_id=user.internal_user_id,
        session_id=session_id,
        model_id=model_id,
        deployment=deployment,
        usage=usage,
        status="error",
        agent=agent_name,
        correlation_id=correlation_id,
    )
    return assistant


async def _stream_with_placeholder(
    *,
    repo: SessionRepository,
    user_id: str,
    assistant: Message,
    events: AsyncGenerator[str, None],
) -> AsyncGenerator[str, None]:
    """Own placeholder persistence within the response iterator lifecycle."""
    placeholder_persisted = False
    try:
        write = asyncio.create_task(repo.add_message(user_id, assistant))
        try:
            await asyncio.shield(write)
            placeholder_persisted = True
        except asyncio.CancelledError:
            try:
                await write
                placeholder_persisted = True
            except Exception:  # noqa: BLE001 - no row exists to finalize
                logger.exception(
                    "Failed to persist assistant placeholder %s", assistant.id
                )
            raise
        except Exception:  # noqa: BLE001 - headers are already committed
            logger.exception("Failed to persist assistant placeholder %s", assistant.id)
            payload = {
                "error": "The reply could not be initialized.",
                "persistenceFailed": True,
            }
            yield f"data: {json.dumps(payload)}\n\n"
            return
        async for event in events:
            yield event
    finally:
        await events.aclose()
        if placeholder_persisted and assistant.status is MessageStatus.streaming:
            assistant.status = MessageStatus.cancelled
            await _persist_terminal_assistant(repo, user_id, assistant)


# Live activity + final answer for an agentic (tool-using) turn. Runs the turn as
# a shielded task and forwards its step trace live as ``{"step": {...}}`` SSE
# events (older clients that only read ``choices[0].delta.content`` ignore them).
#
# With ``stream_tokens`` the turn's assistant text is forwarded the same way, as
# ordinary content deltas, interleaved with those step events — so a tool-using
# turn shows text and "running X" while it works instead of going silent for a
# whole model round trip (audit finding P1-16). What was streamed is then exactly
# what is persisted, so a reloaded conversation matches what the user watched
# arrive; a partially-streamed turn that is cancelled or fails keeps its partial
# text on the row, the way the plain (non-tool) streaming path already does.
#
# Without ``stream_tokens`` the generator behaves as before: nothing but steps
# until the run finishes, then a single content delta. That is the kill switch's
# job — one flag, one branch, and the pre-change bytes on the wire.
#
# Either way the terminal row is durably persisted BEFORE ``[DONE]``, and any
# approval prompts ride out after that write and before ``[DONE]``.
async def _agentic_stream(
    *,
    assistant: Message,
    run: Callable[..., Awaitable[AgentRunResult]],
    repo: SessionRepository,
    memory: MemoryServiceProtocol,
    metering: UsageService,
    user: AuthenticatedUser,
    session_id: str,
    model_id: str,
    deployment: DeploymentOption,
    agent_name: str | None,
    correlation_id: str,
    content_for_model: str,
    user_message_id: str,
    extra_usage: list[TokenUsage] | None = None,
    fallback: Callable[[], Awaitable[tuple[str, TokenUsage]]] | None = None,
    get_attachments: Callable[[], list[MessageAttachment]] | None = None,
    get_approval_drafts: Callable[[], list[ApprovalDraft]] | None = None,
    stream_tokens: bool = False,
) -> AsyncGenerator[str, None]:
    """Stream an agent run and own its terminal persistence lifecycle."""
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=AGENT_EVENT_QUEUE_MAXSIZE)
    sentinel = object()

    async def enqueue(item: object) -> None:
        if queue.full():
            logger.info(
                "chat.agent_stream_backpressure",
                extra={
                    "session_id": session_id,
                    "correlation_id": correlation_id,
                    "queue_maxsize": AGENT_EVENT_QUEUE_MAXSIZE,
                },
            )
        await queue.put(item)

    async def on_step(step: AgentStep) -> None:
        view = serialize_step(step)
        if view is not None:
            await enqueue(("step", view))

    async def on_delta(text: str) -> None:
        if text:
            await enqueue(("delta", text))

    async def runner() -> None:
        try:
            result = await (run(on_step, on_delta) if stream_tokens else run(on_step))
            await enqueue(("result", result))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced below; never crashes the response
            await enqueue(("error", exc))
        finally:
            current = asyncio.current_task()
            if current is None or current.cancelling() == 0:
                await enqueue(sentinel)

    task = asyncio.create_task(runner())
    final = MessageStatus.complete
    total_usage = TokenUsage.empty()
    content = ""
    streamed_text = ""
    persisted: list[ActivityStep] | None = None
    remembered = False
    terminal_persisted = False
    try:
        yield _stream_metadata(user_message_id, assistant.id, assistant.sources)
        result: AgentRunResult | None = None
        run_error: Exception | None = None
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            assert isinstance(item, tuple)
            kind, payload = item
            if kind == "step":
                yield f"data: {json.dumps({'step': payload.model_dump(exclude_none=True)})}\n\n"
            elif kind == "delta":
                # Track what was actually put on the wire, not what the runtime
                # says it produced: on a disconnect mid-turn this is the only
                # honest answer to "what did the user receive?", and it is what
                # the finally block persists.
                streamed_text += payload
                content = streamed_text
                yield f"data: {json.dumps({'choices': [{'delta': {'content': payload}}]})}\n\n"
            elif kind == "result":
                result = payload
            elif kind == "error":
                run_error = payload

        if isinstance(run_error, ModelGatewayError):
            total_usage = total_usage.add(TokenUsage.parse(None))

        if isinstance(run_error, AgentRunFailed):
            partial = run_error.partial
            total_usage = partial.usage
            for extra in extra_usage or []:
                total_usage = total_usage.add(extra)
            persisted = persisted_trace(partial.steps) or None
            content = streamed_text if streamed_text else partial.text
            raise run_error.cause

        # ``pending_delta`` is the text still owed to the client. Under streaming
        # that is normally nothing (it already went out increment by increment);
        # without streaming it is the whole answer, exactly as before.
        pending_delta = ""
        if result is not None and (result.text or "").strip():
            content = streamed_text if streamed_text.strip() else result.text
            pending_delta = "" if streamed_text.strip() else content
            total_usage = result.usage
            for extra in extra_usage or []:
                total_usage = total_usage.add(extra)
            persisted = persisted_trace(result.steps) or None
        elif fallback is not None:
            # Empty (or failed) tool turn: complete the answer with a plain call so
            # the turn never dead-ends. The steps that did run are still shown, and
            # anything already streamed is kept rather than retracted — the user
            # cannot un-see it.
            if isinstance(run_error, ModelGatewayError):
                logger.warning(
                    "agentic stream gateway run failed; using fallback",
                    extra={
                        "ai4ia_gateway_status": run_error.status_code,
                        "ai4ia_correlation_id": correlation_id,
                    },
                )
            elif run_error is not None:
                logger.warning(
                    "agentic stream run failed; using fallback",
                    extra={"ai4ia_correlation_id": correlation_id},
                )
            if result is not None:
                total_usage = total_usage.add(result.usage)
                for extra in extra_usage or []:
                    total_usage = total_usage.add(extra)
                persisted = persisted_trace(result.steps) or None
            try:
                fb_text, fb_usage = await fallback()
            except ModelGatewayError:
                total_usage = total_usage.add(TokenUsage.parse(None))
                raise
            pending_delta = f"\n\n{fb_text}" if streamed_text.strip() else fb_text
            content = f"{streamed_text}{pending_delta}" if streamed_text.strip() else fb_text
            total_usage = total_usage.add(fb_usage)
        elif streamed_text.strip():
            # Streamed text but no reported final answer and no fallback: keep what
            # the user saw rather than persisting an empty row over it.
            content = streamed_text
            if result is not None:
                total_usage = result.usage
                for extra in extra_usage or []:
                    total_usage = total_usage.add(extra)
                persisted = persisted_trace(result.steps) or None
        elif run_error is not None:
            raise run_error

        assistant.content = content
        assistant.status = final
        assistant.steps = persisted
        if get_attachments is not None:
            assistant.attachments = get_attachments()
        # Mint BEFORE the terminal write so the durable record and the message it
        # belongs to land in one upsert, but hand the grants to the client only
        # AFTER that write succeeds — a grant whose record was never saved would
        # be unredeemable, and worse, would look approvable to the user.
        approval_events = (
            _mint_approval_events(assistant, get_approval_drafts())
            if get_approval_drafts is not None
            else []
        )
        if pending_delta:
            yield f"data: {json.dumps({'choices': [{'delta': {'content': pending_delta}}]})}\n\n"
        terminal_persisted = await _persist_terminal_assistant(
            repo, user.internal_user_id, assistant
        )
        if not terminal_persisted:
            yield f"data: {json.dumps({'error': 'The reply could not be saved.', 'persistenceFailed': True})}\n\n"
        else:
            if approval_events:
                yield f"data: {json.dumps({'approvals': approval_events})}\n\n"
            yield "data: [DONE]\n\n"
    except ModelGatewayError:
        final = MessageStatus.error
        assistant.content = content
        assistant.status = final
        assistant.steps = persisted
        if get_attachments is not None:
            assistant.attachments = get_attachments()
        terminal_persisted = await _persist_terminal_assistant(
            repo, user.internal_user_id, assistant
        )
        detail = (
            CHAT_COMPLETION_FAILED
            if terminal_persisted
            else "The failed reply could not be saved."
        )
        error_payload: dict[str, object] = {"error": detail}
        if not terminal_persisted:
            error_payload["persistenceFailed"] = True
        yield f"data: {json.dumps(error_payload)}\n\n"
    except Exception:
        final = MessageStatus.error
        assistant.content = content
        assistant.status = final
        assistant.steps = persisted
        if get_attachments is not None:
            assistant.attachments = get_attachments()
        terminal_persisted = await _persist_terminal_assistant(
            repo, user.internal_user_id, assistant
        )
        error_payload = {"error": CHAT_COMPLETION_FAILED}
        if not terminal_persisted:
            error_payload = {
                "error": "The failed reply could not be saved.",
                "persistenceFailed": True,
            }
        yield f"data: {json.dumps(error_payload)}\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        if not terminal_persisted:
            final = MessageStatus.cancelled
        raise
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if not terminal_persisted:
            assistant.content = content
            assistant.status = final
            assistant.steps = persisted
            if get_attachments is not None:
                assistant.attachments = get_attachments()
            await _persist_terminal_assistant(repo, user.internal_user_id, assistant)
        _status_map = {
            MessageStatus.complete: "complete",
            MessageStatus.cancelled: "cancelled",
            MessageStatus.error: "error",
        }
        try:
            await asyncio.shield(
                metering.record_completion(
                    user_id=user.internal_user_id,
                    session_id=session_id,
                    model_id=model_id,
                    deployment=deployment,
                    usage=total_usage,
                    status=_status_map.get(
                        final if terminal_persisted else MessageStatus.error,
                        "error",
                    ),  # pyright: ignore[reportArgumentType]
                    agent=agent_name,
                    correlation_id=correlation_id,
                )
            )
        except Exception:  # noqa: BLE001 - metering must never break a turn
            logger.warning("usage metering failed for %s", assistant.id, exc_info=True)
        if (
            final == MessageStatus.complete
            and terminal_persisted
            and content.strip()
            and not remembered
        ):
            remembered = True
            await asyncio.shield(
                memory.remember(user.internal_user_id, session_id, content_for_model)
            )


async def _plain_gateway_stream(
    *,
    assistant: Message,
    user_message_id: str,
    gateway: ModelGatewayClient,
    deployment: DeploymentOption,
    messages: list[dict],
    params: dict,
    correlation_id: str,
    api: str,
    repo: SessionRepository,
    memory: MemoryServiceProtocol,
    metering: UsageService,
    user: AuthenticatedUser,
    session_id: str,
    model_id: str,
    agent_name: str | None,
    content_for_model: str,
) -> AsyncGenerator[str, None]:
    """Stream a plain gateway call and own its terminal lifecycle."""
    parts: list[str] = []
    final = MessageStatus.complete
    saw_done = False
    stream_usage: dict | None = None
    stream_safety: MessageSafety | None = None
    terminal_persisted = False
    try:
        yield _stream_metadata(user_message_id, assistant.id, assistant.sources)
        async for chunk in gateway.stream(
            deployment=deployment.deploymentName,
            messages=messages,
            params=params,
            correlation_id=correlation_id,
            api=api,
        ):
            if chunk.usage:
                stream_usage = chunk.usage
            if chunk.safety is not None:
                # Prompt verdicts arrive on an early chunk and completion
                # verdicts on a later one, so the full picture only exists
                # after merging across the stream.
                stream_safety = merge_safety(stream_safety, chunk.safety)
                assistant.safety = stream_safety
            if chunk.raw and _has_gateway_stream_error(chunk.raw):
                final = MessageStatus.error
                assistant.content = "".join(parts)
                assistant.status = final
                terminal_persisted = await _persist_terminal_assistant(
                    repo, user.internal_user_id, assistant
                )
                error_payload: dict[str, object] = {"error": "Model stream failed."}
                if not terminal_persisted:
                    error_payload = {
                        "error": "The failed reply could not be saved.",
                        "persistenceFailed": True,
                    }
                yield f"data: {json.dumps(error_payload)}\n\n"
                return
            if chunk.delta:
                parts.append(chunk.delta)
            if chunk.done:
                saw_done = True
                break
            if chunk.raw:
                yield f"data: {chunk.raw}\n\n"
        if not saw_done:
            final = MessageStatus.error
        assistant.content = "".join(parts)
        assistant.status = final
        terminal_persisted = await _persist_terminal_assistant(
            repo, user.internal_user_id, assistant
        )
        if not terminal_persisted:
            yield f"data: {json.dumps({'error': 'The reply could not be saved.', 'persistenceFailed': True})}\n\n"
        elif saw_done:
            yield "data: [DONE]\n\n"
        else:
            yield f"data: {json.dumps({'error': 'Stream ended unexpectedly.'})}\n\n"
    except ModelGatewayError:
        final = MessageStatus.error
        assistant.content = "".join(parts)
        assistant.status = final
        terminal_persisted = await _persist_terminal_assistant(
            repo, user.internal_user_id, assistant
        )
        detail = (
            CHAT_COMPLETION_FAILED
            if terminal_persisted
            else "The failed reply could not be saved."
        )
        error_payload: dict[str, object] = {"error": detail}
        if not terminal_persisted:
            error_payload["persistenceFailed"] = True
        yield f"data: {json.dumps(error_payload)}\n\n"
    except Exception:
        final = MessageStatus.error
        assistant.content = "".join(parts)
        assistant.status = final
        terminal_persisted = await _persist_terminal_assistant(
            repo, user.internal_user_id, assistant
        )
        error_payload = {"error": CHAT_COMPLETION_FAILED}
        if not terminal_persisted:
            error_payload = {
                "error": "The failed reply could not be saved.",
                "persistenceFailed": True,
            }
        yield f"data: {json.dumps(error_payload)}\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        if not terminal_persisted:
            final = MessageStatus.cancelled
        raise
    finally:
        assistant.content = "".join(parts)
        assistant.status = final
        if not terminal_persisted:
            await _persist_terminal_assistant(repo, user.internal_user_id, assistant)
        # Meter the turn (best-effort, shielded so a client disconnect still
        # records it). Non-complete turns are recorded as non-billable status
        # rows; record_completion gates billability on status == "complete".
        _status_map = {
            MessageStatus.complete: "complete",
            MessageStatus.cancelled: "cancelled",
            MessageStatus.error: "error",
        }
        try:
            await asyncio.shield(
                metering.record_completion(
                    user_id=user.internal_user_id,
                    session_id=session_id,
                    model_id=model_id,
                    deployment=deployment,
                    usage=TokenUsage.parse(stream_usage),
                    status=_status_map.get(
                        final if terminal_persisted else MessageStatus.error,
                        "error",
                    ),  # pyright: ignore[reportArgumentType]
                    agent=agent_name,
                    correlation_id=correlation_id,
                )
            )
        except Exception:  # noqa: BLE001 - metering must never break a turn
            logger.warning("usage metering failed for %s", assistant.id, exc_info=True)
        if final == MessageStatus.complete and saw_done and terminal_persisted:
            # Remember the user's turn only when the model stream completed
            # cleanly (a clean end-of-stream marker), so a truncated or errored
            # turn doesn't seed memory. Best-effort: remember() swallows its own
            # failures. (A durable store will move this off the response path.)
            await asyncio.shield(
                memory.remember(user.internal_user_id, session_id, content_for_model)
            )
