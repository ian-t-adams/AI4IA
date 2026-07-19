"""Domain models for sessions and messages.

``userId`` is denormalized onto every message (in addition to the messages
container PK ``/sessionId``) so the repository can assert ownership on every
read/write — Cosmos cannot route a messages query by user when the partition
key is the session.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def turn_message_id(
    user_id: str, session_id: str, client_turn_id: str, role: "MessageRole"
) -> str:
    """Return a stable message id for one client-originated chat turn."""
    value = f"{user_id}\0{session_id}\0{client_turn_id}\0{role.value}"
    return uuid.uuid5(uuid.NAMESPACE_URL, value).hex


class MessageRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"


class MessageStatus(str, Enum):
    complete = "complete"
    streaming = "streaming"
    cancelled = "cancelled"
    error = "error"


class MessageSource(str, Enum):
    """How a message entered the conversation.

    ``chat`` is the default (typed text turns + their replies). ``voice`` marks a
    turn captured from a Voice Live (real-time speech) exchange and persisted back
    into the SAME session, so text and voice share one continuous transcript and
    context. Voice turns are ordinary user/assistant messages for every other
    purpose (they DO feed model context on subsequent turns); ``source`` is only
    provenance so the UI can mark them and future logic can tell them apart.
    """

    chat = "chat"
    voice = "voice"


class MessageAttachment(BaseModel):
    """A non-text artifact produced during a turn and rendered alongside the
    message text.

    ``kind="image"`` is a generated image persisted by the ``generate_image``
    tool; ``kind="video"`` is a generated MP4 persisted by the ``generate_video``
    tool; ``kind="document"`` is an over-cap ``process_document`` result persisted
    as markdown. The bytes are NOT inlined — ``id`` references a durable,
    user-scoped blob fetched through the matching authenticated serve endpoint
    (``GET /api/images/artifacts/{id}``, ``GET /api/videos/artifacts/{id}``, or
    ``GET /api/documents/artifacts/{id}``), so a message stays small and the 8 KB
    tool-result cap is never an issue.
    """

    id: str
    kind: str = "image"
    mimeType: str = "image/png"
    prompt: str | None = None
    model: str | None = None
    size: str | None = None
    # Image-only: the rendering quality the image was generated at.
    quality: str | None = None
    # Video-only: the requested clip length in seconds.
    durationSeconds: int | None = None
    # Document-only: the source library document's display name.
    filename: str | None = None


class ActivityStep(BaseModel):
    """A redacted, user-facing entry in an assistant turn's activity trace.

    Derived from the agent runtime's ``AgentStep`` for display only: a coarse
    ``kind``, an optional tool name, a human ``label`` (e.g. "Searched the web"),
    and a short redacted ``detail`` (e.g. the query) — never raw tool results or
    full arguments. Persisted on the assistant message so the trace survives a
    reload, and also streamed live during the turn.
    """

    kind: str
    label: str
    tool: str | None = None
    detail: str | None = None


class Message(BaseModel):
    id: str = Field(default_factory=_new_id)
    sessionId: str
    userId: str
    role: MessageRole
    content: str = ""
    status: MessageStatus = MessageStatus.complete
    model: str | None = None
    # Name of the agent this turn was routed to via an ``@mention`` (set on both
    # the routed user message and the assistant reply). ``None`` for plain turns.
    agent: str | None = None
    # True for the local transcript of a slash command (the echoed command and
    # its reply). These are shown in the UI but excluded from model context and
    # from first-turn auto-titling.
    fromCommand: bool = False
    # How this turn entered the conversation: ``chat`` (typed) or ``voice`` (a
    # Voice Live exchange persisted back into the same session). Voice turns still
    # feed model context like any other; this is provenance only.
    source: MessageSource = MessageSource.chat
    # Browser-generated UUID shared by the user/assistant rows for one typed turn.
    # Optional so historical Cosmos documents continue to validate unchanged.
    clientTurnId: str | None = None
    # Internal hash used to reject reuse of a turn id for different request input.
    # It is persisted by repositories but excluded from HTTP response models.
    clientRequestFingerprint: str | None = Field(default=None, exclude=True)
    # Per-attempt ownership lease for a streaming assistant claim. Completion and
    # error writes must present this value and may only transition that same
    # still-streaming claim. It is internal protocol state, never an HTTP field.
    claimLeaseId: str | None = Field(default=None, exclude=True)
    # Non-text artifacts produced during the turn (e.g. generated images). Empty
    # for ordinary text replies; each entry references a durable blob served
    # through an authenticated endpoint.
    attachments: list[MessageAttachment] = Field(default_factory=list)
    # Redacted activity trace for an agentic/tool turn: the tools the model called
    # and their outcome, rendered as an expandable panel under the answer. None for
    # plain (no-tool) turns. Display-only, derived from the runtime's step trace.
    steps: list[ActivityStep] | None = None
    # Present only on the assistant reply produced by /summarize. Repositories
    # use it to fence and purge replies superseded by clear/newer summaries.
    summaryVersion: int | None = None
    createdAt: datetime = Field(default_factory=_now)


class ToolOverrides(BaseModel):
    """Chat-level tool changes relative to the selected agent/default baseline."""

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


MAX_LIBRARY_DOCUMENTS_PER_SESSION = 20
MAX_SESSION_TITLE_CHARS = 120


def normalize_tool_overrides(
    value: ToolOverrides | Mapping[str, object],
) -> ToolOverrides:
    """Return one canonical override shape at HTTP and repository boundaries."""
    try:
        overrides = (
            value
            if isinstance(value, ToolOverrides)
            else ToolOverrides.model_validate(value)
        )
    except ValidationError as exc:
        raise ValueError(
            "toolOverrides must contain string lists named added and removed."
        ) from exc

    def clean(values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                normalized
                for item in values
                if (normalized := item.strip())
            )
        )

    added = clean(overrides.added)
    removed = clean(overrides.removed)
    overlap = set(added) & set(removed)
    if overlap:
        raise ValueError(
            "Tools cannot be both added and removed: "
            + ", ".join(sorted(overlap))
        )
    if len(added) > 8 or len(removed) > 16:
        raise ValueError("Too many conversation tool overrides.")
    return ToolOverrides(added=added, removed=removed)


def normalize_session_title(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Conversation title must be text.")
    title = value.strip()
    if not title:
        raise ValueError("Conversation title cannot be empty.")
    if len(title) > MAX_SESSION_TITLE_CHARS:
        raise ValueError(
            f"Conversation title must be {MAX_SESSION_TITLE_CHARS} characters or fewer."
        )
    return title


def normalize_session_patch_changes(
    changes: Mapping[str, object],
) -> dict[str, object]:
    normalized = dict(changes)
    if "toolOverrides" in normalized:
        value = normalized["toolOverrides"]
        if not isinstance(value, (ToolOverrides, Mapping)):
            raise ValueError(
                "toolOverrides must contain string lists named added and removed."
            )
        normalized["toolOverrides"] = normalize_tool_overrides(value)
    if "title" in normalized:
        normalized["title"] = normalize_session_title(normalized["title"])
        normalized.setdefault("titleSource", "manual")
    return normalized


class Session(BaseModel):
    id: str = Field(default_factory=_new_id)
    userId: str
    title: str = "New chat"
    titleSource: Literal["auto", "manual"] = "auto"
    model: str | None = None
    systemPrompt: str | None = None
    # Standing conversation policy. All fields are additive so existing Cosmos
    # records remain valid without a migration.
    agentName: str | None = None
    toolOverrides: ToolOverrides = Field(default_factory=ToolOverrides)
    # None preserves the legacy behavior: all accessible ready library documents
    # may contribute. An explicit [] opts the conversation out of library context.
    # A non-empty list is the exact selected-document allowlist.
    libraryDocumentIds: list[str] | None = None
    # When rolling summarization has
    # folded older turns, ``summary`` holds the compact running digest of every
    # turn UP TO AND INCLUDING ``summarizedThroughMessageId``; turns after that
    # id are still sent verbatim. Both default to ``None`` (no summary yet) and a
    # session that has never been summarized round-trips byte-for-byte. The full
    # transcript is ALWAYS retained in the messages container and the UI — these
    # only change what is sent to the model as context, never what is stored.
    summary: str | None = None
    summarizedThroughMessageId: str | None = None
    # Monotonic generation for summary source state. Clear/reset and successful
    # summary commits both advance it so stale in-flight summarizers fail closed.
    summaryVersion: int = 0
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)


class Document(BaseModel):
    """A user-uploaded reference file whose extracted plain text is injected
    (capped) into chat turns as untrusted context.

    Stored in its own Cosmos container (PK ``/sessionId``) rather than embedded
    on the session so large text never bloats the session list. ``userId`` is
    denormalized + ownership-checked on every access, mirroring ``Message``.
    """

    id: str = Field(default_factory=_new_id)
    sessionId: str
    userId: str
    filename: str
    contentType: str = ""
    # Size of the original uploaded bytes (not the extracted text).
    size: int = 0
    # Length of the stored (post-cap) extracted text.
    charCount: int = 0
    # True when the original text exceeded the per-document storage cap.
    truncated: bool = False
    text: str = ""
    # Blob path of the EPHEMERAL retained original bytes, set only when the inline
    # code-interpreter feature is on AND this upload was a CI-supported, within-cap
    # file (see routers/documents.py). ``None`` (the default) means no bytes were
    # retained — today's text-only behavior — so this also gates whether the
    # ``analyze_attachment`` tool is offered for the document. The bytes are
    # session-scoped, ownership-checked, and purged on document/session delete; the
    # actual fetch path is recomposed from the authenticated identity, never from
    # this stored value.
    rawRef: str | None = None
    createdAt: datetime = Field(default_factory=_now)
