"""Normalized result of a Responses API Code Interpreter call (Phase 11C).

The Responses API returns ``{id, status, output_text?, output:[...]}``. ``output``
is a heterogeneous list of items: assistant ``message`` items carry
``content:[{type:"output_text", text}]`` and ``code_interpreter_call`` items carry
the model-authored ``code`` plus ``outputs`` (logs / generated images). We flatten
this to the assistant's answer text plus the captured stdout-like logs, both of
which the caller treats as **untrusted** model/tool output and nonce-fences before
returning to the chat model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CodeInterpreterResult:
    status: str
    # The assistant's natural-language answer (concatenated output_text).
    output_text: str = ""
    # Captured Code Interpreter logs (stdout/stderr-like), one entry per call.
    logs: list[str] = field(default_factory=list)
    # Filenames of any artifacts the container produced (image/file outputs).
    artifacts: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status.lower() in ("completed", "succeeded")


def _collect_message_text(item: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    content = item.get("content")
    if isinstance(content, list):
        for c in content:
            if (
                isinstance(c, dict)
                and c.get("type") in ("output_text", "text")
                and isinstance(c.get("text"), str)
            ):
                parts.append(c["text"])
    elif isinstance(content, str):
        parts.append(content)
    return parts


def _collect_ci_logs(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    logs: list[str] = []
    artifacts: list[str] = []
    outputs = item.get("outputs")
    if not isinstance(outputs, list):
        return logs, artifacts
    for o in outputs:
        if not isinstance(o, dict):
            continue
        otype = o.get("type")
        if otype == "logs" and isinstance(o.get("logs"), str):
            logs.append(o["logs"])
        elif otype in ("image", "file"):
            name = o.get("url") or o.get("file_id") or o.get("filename")
            if isinstance(name, str):
                artifacts.append(name)
    return logs, artifacts


def parse_response(body: dict[str, Any]) -> CodeInterpreterResult:
    """Normalize a Responses API body into a :class:`CodeInterpreterResult`.

    Defensive by design: the real round-trip is live-only, so unknown/partial
    shapes degrade to whatever text could be extracted rather than raising.
    """
    status = str(body.get("status", "") or "")
    text_parts: list[str] = []

    top_text = body.get("output_text")
    if isinstance(top_text, str) and top_text.strip():
        text_parts.append(top_text)

    logs: list[str] = []
    artifacts: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message" and not top_text:
                text_parts.extend(_collect_message_text(item))
            elif itype in ("code_interpreter_call", "tool_call"):
                ci_logs, ci_arts = _collect_ci_logs(item)
                logs.extend(ci_logs)
                artifacts.extend(ci_arts)

    return CodeInterpreterResult(
        status=status,
        output_text="\n".join(p for p in text_parts if p).strip(),
        logs=logs,
        artifacts=artifacts,
        raw=body,
    )
