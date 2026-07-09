"""Turn an agent-runtime :class:`AgentStep` into a redacted, user-facing view.

The runtime emits a step trace (which tool ran, and how it turned out). This maps
each step to an :class:`~ai4ia_api.sessions.models.ActivityStep` for two surfaces:

* the **live** SSE activity stream during a turn (includes the pre-execution
  ``tool_start`` marker so the UI can say "Searching the web..." while it runs),
* the **persisted** trace stored on the assistant message (finalized steps only).

Redaction is deliberate: only a coarse ``kind``, the tool name, a human ``label``,
and a short single-line ``detail`` pulled from a known-safe argument key (e.g. the
search query or URL) — never raw tool results or full arguments.
"""
from __future__ import annotations

from typing import Any

from ..sessions.models import ActivityStep
from .runtime import AgentStep

# tool name -> (present-progressive label for live, past-tense label for the trace)
_TOOL_VERBS: dict[str, tuple[str, str]] = {
    "web_search": ("Searching the web", "Searched the web"),
    "news_search": ("Searching the news", "Searched the news"),
    "video_search": ("Searching for videos", "Searched for videos"),
    "image_search": ("Searching for images", "Searched for images"),
    "browse_url": ("Reading a web page", "Read a web page"),
    "run_code": ("Running code", "Ran code"),
    "export_document": ("Exporting a document", "Exported a document"),
    "fetch_document": ("Reading a document", "Read a document"),
    "process_document": ("Processing a document", "Processed a document"),
    "analyze_attachment": ("Analyzing an attachment", "Analyzed an attachment"),
    "generate_image": ("Generating an image", "Generated an image"),
    "generate_video": ("Generating a video", "Generated a video"),
    "recall_memory": ("Recalling earlier context", "Recalled earlier context"),
    "delegate_to_agent": ("Delegating to an agent", "Delegated to an agent"),
    "calculator": ("Calculating", "Calculated"),
    "get_current_time": ("Checking the time", "Checked the time"),
}

# Argument keys that are safe + useful to show as a short detail, in priority order.
_DETAIL_KEYS = ("query", "url", "expression", "prompt", "agent", "path", "filename", "language")
_DETAIL_MAX = 80


def _humanize(tool: str | None) -> str:
    return (tool or "the tool").replace("_", " ")


def _verbs(tool: str | None) -> tuple[str, str]:
    human = _humanize(tool)
    return _TOOL_VERBS.get(tool or "", (f"Running {human}", f"Ran {human}"))


def _detail(step: AgentStep) -> str | None:
    """A short, single-lined, length-capped hint drawn from a safe argument key."""
    args = step.arguments
    if not isinstance(args, dict):
        return None
    for key in _DETAIL_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:_DETAIL_MAX]
    return None


def serialize_step(step: AgentStep) -> ActivityStep | None:
    """Compact, redacted view of one step, or ``None`` when it isn't surfaced.

    The bare ``final`` marker (the model's natural-language answer) carries no
    activity and is dropped; everything else maps to a labeled entry.
    """
    kind = step.kind
    if kind == "final":
        return None
    tool = step.tool
    present, past = _verbs(tool)
    if kind == "tool_start":
        label = present
    elif kind in ("tool_result", "delegate"):
        label = past
    elif kind == "tool_denied":
        label = f"Blocked {_humanize(tool)}"
    elif kind == "tool_error":
        label = f"{_humanize(tool).capitalize()} didn't complete"
    else:
        label = present
    out: dict[str, Any] = {"kind": kind, "label": label}
    if tool:
        out["tool"] = tool
    detail = _detail(step)
    if detail:
        out["detail"] = detail
    return ActivityStep(**out)


def persisted_trace(steps: list[AgentStep]) -> list[ActivityStep]:
    """Serialize a finalized step list into the trace stored on the message."""
    out: list[ActivityStep] = []
    for step in steps:
        view = serialize_step(step)
        if view is not None:
            out.append(view)
    return out
