"""Domain models for sessions and messages.

``userId`` is denormalized onto every message (in addition to the messages
container PK ``/sessionId``) so the repository can assert ownership on every
read/write — Cosmos cannot route a messages query by user when the partition
key is the session.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class MessageRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"


class MessageStatus(str, Enum):
    complete = "complete"
    streaming = "streaming"
    cancelled = "cancelled"
    error = "error"


class MessageAttachment(BaseModel):
    """A non-text artifact produced during a turn and rendered alongside the
    message text (Phase 11F).

    ``kind="image"`` is a generated image persisted by the ``generate_image``
    tool; ``kind="video"`` is a generated MP4 persisted by the ``generate_video``
    tool. The bytes are NOT inlined — ``id`` references a durable, user-scoped
    blob fetched through the matching authenticated serve endpoint
    (``GET /api/images/artifacts/{id}`` or ``GET /api/videos/artifacts/{id}``),
    so a message stays small and the 8 KB tool-result cap is never an issue.
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
    # Non-text artifacts produced during the turn (e.g. generated images). Empty
    # for ordinary text replies; each entry references a durable blob served
    # through an authenticated endpoint.
    attachments: list[MessageAttachment] = Field(default_factory=list)
    createdAt: datetime = Field(default_factory=_now)


class Session(BaseModel):
    id: str = Field(default_factory=_new_id)
    userId: str
    title: str = "New chat"
    model: str | None = None
    systemPrompt: str | None = None
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
    createdAt: datetime = Field(default_factory=_now)
