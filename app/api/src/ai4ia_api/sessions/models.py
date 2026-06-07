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


class Message(BaseModel):
    id: str = Field(default_factory=_new_id)
    sessionId: str
    userId: str
    role: MessageRole
    content: str = ""
    status: MessageStatus = MessageStatus.complete
    model: str | None = None
    # True for the local transcript of a slash command (the echoed command and
    # its reply). These are shown in the UI but excluded from model context and
    # from first-turn auto-titling.
    fromCommand: bool = False
    createdAt: datetime = Field(default_factory=_now)


class Session(BaseModel):
    id: str = Field(default_factory=_new_id)
    userId: str
    title: str = "New chat"
    model: str | None = None
    systemPrompt: str | None = None
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)
