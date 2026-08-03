"""The ``remember_memory`` synthetic capability.

The write counterpart to :mod:`ai4ia_api.memory.recall_capability`. Until this
existed, memory could only ever be written by the *passive* path in the chat
router, which stores the user's own utterance after a turn. There was no way for
an agent to record a conclusion it reached — so an agent asked to "read these
notes and remember the decisions" would correctly answer that it cannot, which is
exactly what a workflow built for that job reported.

Safety posture (matches :func:`~ai4ia_api.memory.recall_capability.build_recall_capability`):
- ``user_id`` is closure-bound, never a tool argument, so the model can only ever
  write to the caller's own store.
- ``session_id`` is closure-bound so a written memory is attributable to the
  conversation or run that produced it.
- A per-turn write budget bounds how much one turn can persist.
- Text is capped, so a single call cannot store an unbounded blob.

**Honest results.** :meth:`MemoryService.remember` is deliberately fail-soft and
returns whether anything was durably written. This handler forwards that verdict
verbatim instead of assuming success: a skipped or failed write reported as
"saved" is a lie the model would confidently repeat to the user. The Cosmos-backed
service legitimately answers "no change" when its planner decides the text adds
nothing already known, and that is reported as such rather than as a failure —
the two are different outcomes and conflating them would teach the model to
retry a write that was already correctly declined.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..agents.tool_exec import ToolContext
from .service import MemoryServiceProtocol

REMEMBER_TOOL_NAME = "remember_memory"
# Hard cap on memory writes per turn (on top of the runtime's global tool-call
# budget) so one turn can't flood the user's store.
MAX_REMEMBERS_PER_TURN = 8
# Upper bound on a single stored memory. Memories are meant to be short, durable
# facts; a whole document belongs in the library, not the memory store.
MAX_TEXT_LEN = 1000

Handler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


def build_remember_capability(
    *,
    memory: MemoryServiceProtocol,
    user_id: str,
    session_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Handler]]:
    """Build the ``remember_memory`` tool for ``user_id``.

    Returns ``(extra_tools, extra_handlers)`` ready to merge into
    :func:`~ai4ia_api.agents.runtime.run_agent_turn`.
    """
    budget = {"used": 0}

    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": REMEMBER_TOOL_NAME,
            "description": (
                "Save a short, durable fact about the current user so it can be "
                "recalled in later conversations — a preference, a decision, a "
                "project name, a deadline, a requirement. Call it once per "
                "distinct fact, and write each one as a self-contained sentence "
                "that will still make sense months later with no surrounding "
                "context. Do not use it for transient chit-chat, for content that "
                "belongs in a document, or to echo something the user only asked "
                "you to display."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "The fact to remember, as one self-contained sentence."
                        ),
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if budget["used"] >= MAX_REMEMBERS_PER_TURN:
            return {
                "saved": False,
                "error": (
                    f"Memory write budget for this turn is exhausted "
                    f"({MAX_REMEMBERS_PER_TURN} writes). Tell the user which "
                    "facts were not saved."
                ),
            }
        text = str(args.get("text") or "").strip()
        if not text:
            return {"saved": False, "error": "text must be a non-empty string."}
        if len(text) > MAX_TEXT_LEN:
            return {
                "saved": False,
                "error": (
                    f"text exceeds {MAX_TEXT_LEN} characters. Store a short fact, "
                    "not a document."
                ),
            }
        budget["used"] += 1
        try:
            # user_id is closure-bound, NEVER taken from tool args, so the model
            # cannot write into another user's memory.
            stored = await memory.remember(user_id, session_id, text)
        except Exception:  # noqa: BLE001 - memory must never break a turn
            return {
                "saved": False,
                "error": (
                    "Memory is currently unavailable, so nothing was saved. Say so "
                    "rather than claiming the fact was remembered."
                ),
            }
        if stored:
            return {"saved": True, "text": text}
        return {
            "saved": False,
            "note": (
                "Nothing new was stored — the fact was too short to keep, or it is "
                "already covered by an existing memory. This is not an error; do "
                "not retry the same text."
            ),
        }

    return [schema], {REMEMBER_TOOL_NAME: _handler}
