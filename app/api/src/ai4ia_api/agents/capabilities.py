"""Capabilities shared by every execution mode that runs an agent turn.

Synthetic capabilities are built per turn and closure-bound to the authenticated
user, so they cannot live in the tool registry (whose executor runs with an empty
:class:`~ai4ia_api.agents.tool_exec.ToolContext`). They were therefore assembled
inline in the chat router — which meant they existed *only* in chat. A workflow
step called :func:`~ai4ia_api.agents.runtime.run_agent_turn` with no
``extra_tools`` at all, so every step ran with just the two registry built-ins
(``calculator``, ``get_current_time``) no matter which tools its agent declared.
Nothing failed: the model simply answered that it could not read documents,
search the web, or remember anything, and that answer was persisted as the run's
result.

This module owns the capabilities that are **identical regardless of how the turn
was started**, so chat and workflows cannot drift into different tool surfaces:

* ``fetch_document``   — read the user's ready library
* the Web IQ suite     — ``web_search`` / ``news_search`` / ``video_search`` /
  ``image_search`` / ``browse_url``
* ``recall_memory``    — read the user's durable memory
* ``remember_memory``  — write to the user's durable memory

Deliberately **not** here: ``delegate_to_agent`` (workflows reject orchestrator
steps by construction), ``run_code`` / ``export_document`` and
``analyze_attachment`` (gated on a per-turn classification and on inline session
attachments), MCP tools (they replace the registry/executor pair rather than add
to it), ``run_workflow`` (chat-only to prevent recursion), and
``generate_image`` / ``generate_video`` / ``process_document``. The media/document
tools write into a *sink* list that the chat router drains onto the
assistant message; a durable workflow activity finishes in a different process
from the one that persists the run's message, so a sink filled there would be
silently discarded. Offering a tool whose output cannot be delivered is worse
than not offering it, so they stay chat-only until the run result can carry
attachments.
"""
from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..library.chat_capability import build_document_capability
from ..memory.recall_capability import RECALL_TOOL_NAME, build_recall_capability
from ..memory.remember_capability import (
    REMEMBER_TOOL_NAME,
    build_remember_capability,
)
from ..memory.service import MemoryServiceProtocol
from .tool_exec import CHAT_ONLY_SYNTHETIC_TOOL_NAMES, ToolContext

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]
#: Builds the synthetic capabilities for one turn, given the agent's declared tool
#: names. Called **per turn/step** because the set is tool-dependent:
#: :func:`~ai4ia_api.agents.runtime.run_agent_turn` appends ``extra_tools``
#: wholesale rather than filtering them against ``tool_names``, so gating happens
#: at build time or not at all.
CapabilityBuilder = Callable[[Sequence[str]], tuple[list[dict[str, Any]], dict[str, Handler]]]


class _WebSearchCapabilityBuilder(Protocol):
    """The slice of ``WebSearchService`` this module uses.

    Structural, so importing the shared builder does not drag the Web IQ SDK into
    call sites that never enable it. ``session_id`` is required (not optional)
    because Web IQ meters and rate-limits per session — mirrored below by only
    offering these tools when a session id is actually available.
    """

    def build_capability(
        self, *, user_id: str, session_id: str, nonce: str
    ) -> tuple[list[dict], dict[str, Handler]]: ...


@dataclass(frozen=True)
class SharedCapabilities:
    """Assembled tool schemas + handlers, plus what was skipped and why.

    ``unavailable`` lists tools the agent asked for that could not be built. The
    caller decides what to do with it; the point is that a missing capability is
    *known* rather than inferred from the model apologising in its answer.
    """

    tools: list[dict[str, Any]]
    handlers: dict[str, Handler]
    unavailable: dict[str, str]


def build_shared_capabilities(
    *,
    attached_tool_names: Collection[str],
    user_id: str,
    nonce: str,
    session_id: str | None = None,
    email: str | None = None,
    retrieval: Any | None = None,
    library_tools_enabled: bool = False,
    allowed_document_ids: set[str] | None = None,
    web_search: _WebSearchCapabilityBuilder | None = None,
    memory: MemoryServiceProtocol | None = None,
) -> SharedCapabilities:
    """Assemble the execution-mode-independent capabilities for one turn.

    Every block is best-effort in the same way its chat-router ancestor was: a
    build failure costs that one capability and never breaks the turn. Each
    capability is closure-bound to ``user_id``, so none of them can reach another
    user's data regardless of what the model passes as arguments.

    ``attached_tool_names`` is the agent's declared tool list. Library and Web IQ
    are offered whenever their service is present (matching chat, where they are
    available to any tool-enabled turn); the two memory tools are offered only
    when explicitly attached, because writing to a user's durable memory should
    follow from a deliberate choice rather than from a service happening to be
    configured.
    """
    tools: list[dict[str, Any]] = []
    handlers: dict[str, Handler] = {}
    unavailable: dict[str, str] = {}

    if retrieval is not None and library_tools_enabled:
        try:
            d_tools, d_handlers = build_document_capability(
                service=retrieval,
                user_id=user_id,
                nonce=nonce,
                email=email,
                allowed_document_ids=allowed_document_ids,
            )
            tools.extend(d_tools)
            handlers.update(d_handlers)
        except Exception:  # noqa: BLE001 - a capability must never break a turn
            logger.warning("document capability build failed", exc_info=True)
            unavailable["fetch_document"] = "document library is unavailable"

    if web_search is not None:
        if session_id is None:
            # Web IQ meters and rate-limits per session; without one there is
            # nothing to attribute the calls to. Recorded rather than skipped
            # silently so a caller that forgot to pass a session can see why the
            # model suddenly cannot search.
            unavailable["web_search"] = "no session to attribute searches to"
        else:
            try:
                w_tools, w_handlers = web_search.build_capability(
                    user_id=user_id, session_id=session_id, nonce=nonce
                )
                tools.extend(w_tools)
                handlers.update(w_handlers)
            except Exception:  # noqa: BLE001 - a capability must never break a turn
                logger.warning("web search capability build failed", exc_info=True)
                unavailable["web_search"] = "web search is unavailable"

    memory_enabled = memory is not None and getattr(memory, "enabled", False)

    if RECALL_TOOL_NAME in attached_tool_names:
        if memory_enabled and memory is not None:
            try:
                r_tools, r_handlers = build_recall_capability(
                    memory=memory,
                    user_id=user_id,
                    nonce=nonce,
                    session_id=session_id,
                )
                tools.extend(r_tools)
                handlers.update(r_handlers)
            except Exception:  # noqa: BLE001 - a capability must never break a turn
                logger.warning("recall capability build failed", exc_info=True)
                unavailable[RECALL_TOOL_NAME] = "memory recall is unavailable"
        else:
            unavailable[RECALL_TOOL_NAME] = "memory is not enabled"

    if REMEMBER_TOOL_NAME in attached_tool_names:
        if memory_enabled and memory is not None:
            try:
                m_tools, m_handlers = build_remember_capability(
                    memory=memory, user_id=user_id, session_id=session_id
                )
                tools.extend(m_tools)
                handlers.update(m_handlers)
            except Exception:  # noqa: BLE001 - a capability must never break a turn
                logger.warning("remember capability build failed", exc_info=True)
                unavailable[REMEMBER_TOOL_NAME] = "memory writes are unavailable"
        else:
            unavailable[REMEMBER_TOOL_NAME] = "memory is not enabled"

    # Chat-only capabilities. A workflow step may legitimately carry one, because
    # `attachable_tool_names` governs what a user may attach to an *agent*, not
    # which execution mode later runs it. Nothing here can build them, so record
    # them: until this existed the step simply ran without the tool, the model
    # narrated work it had not done, and the run was persisted as a success with
    # zero server-side signal — precisely the failure `unavailable` exists to
    # make visible. ``run_workflow`` is also chat-only to prevent recursion.
    for name in sorted(CHAT_ONLY_SYNTHETIC_TOOL_NAMES & set(attached_tool_names)):
        unavailable[name] = "chat only: results are delivered as message attachments"

    return SharedCapabilities(tools=tools, handlers=handlers, unavailable=unavailable)


def capability_builder_for_state(
    state: Any,
    *,
    user_id: str,
    session_id: str | None = None,
    email: str | None = None,
    allowed_document_ids: set[str] | None = None,
    nonce: str | None = None,
) -> CapabilityBuilder:
    """Adapt the app state into a per-step :data:`CapabilityBuilder`.

    Both workflow execution modes (the in-request runner and the durable
    activity) call this with the same app state, so a step's tool surface cannot
    depend on *how* the run was started. ``state`` is duck-typed rather than
    imported as a FastAPI type because the durable worker holds a plain snapshot
    of the same services, not a ``Request``.

    ``nonce`` fences untrusted tool output against prompt injection exactly as the
    chat turn's library nonce does. One is generated per builder when not
    supplied, so a caller cannot accidentally reuse a predictable marker.
    """
    marker = nonce or secrets.token_hex(4)
    retrieval = getattr(state, "document_retrieval", None)
    web_search = getattr(state, "web_search", None)
    memory = getattr(state, "memory", None)

    def _build(
        tool_names: Sequence[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Handler]]:
        built = build_shared_capabilities(
            attached_tool_names=tool_names,
            user_id=user_id,
            nonce=marker,
            session_id=session_id,
            email=email,
            retrieval=retrieval,
            # Mirrors chat: a session scoped to an explicit (possibly empty)
            # document set disables library tools when that set is empty, but an
            # unscoped session (None) leaves them on.
            library_tools_enabled=allowed_document_ids is None
            or bool(allowed_document_ids),
            allowed_document_ids=allowed_document_ids,
            web_search=web_search,
            memory=memory,
        )
        if built.unavailable:
            logger.info(
                "capabilities unavailable for user turn: %s", built.unavailable
            )
        return built.tools, built.handlers

    return _build
