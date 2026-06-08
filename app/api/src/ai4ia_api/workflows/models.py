"""User-defined workflows (Phase 8 inc 3): a per-user, durable, ordered pipeline
of agent steps.

A *workflow* is a named sequence of steps a single user authors and saves. Each
step targets an agent (curated or user-defined, by name) with an instruction
**template**; at run time the steps execute in order and each step's prompt is
built by substituting ``{input}`` (the original user input) and ``{previous}``
(the prior step's output) into the template. The final step's output is the
workflow result.

Workflows live in their own Cosmos container (``workflows``, PK ``/userId``) and
their own invocation surface (``POST /api/workflows/{name}/run``), so they share
no namespace with the agent ``@``-mention catalog — a workflow and an agent may
share a name without conflict.

This module defines the durable record (:class:`Workflow`), the client request
payloads (server-owned fields excluded), and the typed errors the service raises
(translated to HTTP status codes by the router). Agent-name grammar is reused
from the user-agents module so a step target is always a syntactically valid
``@``-mentionable agent name.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..agents.user_agents import NAME_RE  # reuse the @mention/Cosmos-id grammar

MAX_WORKFLOWS_PER_USER = 50
MAX_NAME_LEN = 32
MAX_DISPLAY_NAME_LEN = 80
MAX_DESCRIPTION_LEN = 280
MAX_STEPS = 6
MAX_INSTRUCTION_LEN = 4000
# Hard cap on the per-run input a caller may submit, so the prompt that flows
# into step 1 (and is then amplified across the pipeline) can't be unbounded.
MAX_RUN_INPUT_LEN = 8000

# Placeholders a step instruction may reference. ``{input}`` is the original
# user-supplied run input; ``{previous}`` is the prior step's output text.
INPUT_TOKEN = "{input}"
PREVIOUS_TOKEN = "{previous}"

__all__ = [
    "NAME_RE",
    "MAX_WORKFLOWS_PER_USER",
    "MAX_NAME_LEN",
    "MAX_DISPLAY_NAME_LEN",
    "MAX_DESCRIPTION_LEN",
    "MAX_STEPS",
    "MAX_INSTRUCTION_LEN",
    "MAX_RUN_INPUT_LEN",
    "INPUT_TOKEN",
    "PREVIOUS_TOKEN",
    "WorkflowError",
    "WorkflowValidationError",
    "WorkflowConflictError",
    "WorkflowNotFoundError",
    "WorkflowStep",
    "Workflow",
    "WorkflowCreate",
    "WorkflowUpdate",
]


class WorkflowError(Exception):
    """Base class for workflow service errors."""


class WorkflowValidationError(WorkflowError):
    """A field failed validation (-> HTTP 422)."""


class WorkflowConflictError(WorkflowError):
    """Name already exists or the per-user cap is reached (-> 409)."""


class WorkflowNotFoundError(WorkflowError):
    """No workflow with that name for this user (-> 404)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowStep(BaseModel):
    """One step in a workflow: run ``agent`` with the rendered ``instruction``.

    ``agent`` is the name of a curated or user-defined agent; existence is NOT
    checked at write time (the composed catalog is per-user and dynamic, mirroring
    agent ``links``) — an unknown/disabled/unsupported target surfaces as a
    structured run error. ``instruction`` may reference ``{input}`` and
    ``{previous}`` placeholders.
    """

    agent: str
    instruction: str


class Workflow(BaseModel):
    """Durable, server-side record of a user-authored workflow.

    ``id`` equals ``name`` and is unique within the user's ``/userId`` partition,
    so reads/deletes are single-partition point operations.
    """

    id: str
    userId: str
    name: str
    displayName: str
    description: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    enabled: bool = True
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)


class WorkflowCreate(BaseModel):
    """Client payload for creating a workflow. Server-owned fields (``id``,
    ``userId``, timestamps) are intentionally absent and set server-side."""

    name: str
    displayName: str | None = None
    description: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    enabled: bool = True


class WorkflowUpdate(BaseModel):
    """Client payload for replacing a workflow (the name comes from the path)."""

    displayName: str | None = None
    description: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    enabled: bool = True
