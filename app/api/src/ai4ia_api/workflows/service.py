"""WorkflowService (Phase 8 inc 3): owns user-workflow CRUD + validation.

Validation mirrors the user-agents service: a strict name grammar (so a step can
always be resolved by the same ``@``-mention rules), bounded field lengths, a
per-user cap, and a bounded, non-empty step list. A step's ``agent`` is validated
only for *shape* (the same grammar) — existence is resolved at **run time** from
the caller's composed catalog (the catalog is per-user and dynamic, so a step may
reference an agent that is created later, or a curated one), exactly like agent
``links``.

To guarantee the user's run input always reaches the pipeline, the **first**
step's instruction must contain the ``{input}`` placeholder.

Unlike :class:`~ai4ia_api.agents.service.AgentService`, this service is not on the
chat hot path, so reads do **not** fail open — a store error surfaces to the
caller. The per-user cap count on create therefore fails *closed* (a store error
aborts the create rather than letting the cap be bypassed).
"""
from __future__ import annotations

from .models import (
    INPUT_TOKEN,
    MAX_DESCRIPTION_LEN,
    MAX_DISPLAY_NAME_LEN,
    MAX_INSTRUCTION_LEN,
    MAX_STEPS,
    MAX_WORKFLOWS_PER_USER,
    NAME_RE,
    Workflow,
    WorkflowConflictError,
    WorkflowCreate,
    WorkflowNotFoundError,
    WorkflowStep,
    WorkflowUpdate,
    WorkflowValidationError,
    _now,
)
from .store import WorkflowStore


class WorkflowService:
    def __init__(self, store: WorkflowStore) -> None:
        self._store = store

    async def close(self) -> None:
        await self._store.close()

    async def list_for(self, user_id: str) -> list[Workflow]:
        return await self._store.list(user_id)

    async def get(self, user_id: str, name: str) -> Workflow | None:
        return await self._store.get(user_id, (name or "").strip().lower())

    async def create(self, user_id: str, req: WorkflowCreate) -> Workflow:
        name = (req.name or "").strip().lower()
        self._validate_name(name)
        # Fail closed: an existence/count read that errors aborts the create (the
        # error propagates) rather than letting the uniqueness or per-user cap be
        # silently bypassed.
        if await self._store.get(user_id, name) is not None:
            raise WorkflowConflictError(f"You already have a workflow named '{name}'.")
        existing = await self._store.list(user_id)
        if len(existing) >= MAX_WORKFLOWS_PER_USER:
            raise WorkflowConflictError(
                f"You have reached the maximum of {MAX_WORKFLOWS_PER_USER} workflows."
            )
        workflow = self._build(
            user_id=user_id,
            name=name,
            display_name=req.displayName,
            description=req.description,
            steps=req.steps,
            enabled=req.enabled,
        )
        await self._store.put(workflow)
        return workflow

    async def update(self, user_id: str, name: str, req: WorkflowUpdate) -> Workflow:
        key = (name or "").strip().lower()
        current = await self._store.get(user_id, key)
        if current is None:
            raise WorkflowNotFoundError(key)
        workflow = self._build(
            user_id=user_id,
            name=current.name,
            display_name=req.displayName,
            description=req.description,
            steps=req.steps,
            enabled=req.enabled,
            created_at=current.createdAt,
        )
        await self._store.put(workflow)
        return workflow

    async def delete(self, user_id: str, name: str) -> None:
        await self._store.delete(user_id, (name or "").strip().lower())

    # --- Validation -----------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name:
            raise WorkflowValidationError("Workflow name is required.")
        if not NAME_RE.match(name):
            raise WorkflowValidationError(
                "Workflow name must be 1-32 chars, start with a letter, end "
                "alphanumeric, and contain only lowercase letters, digits, '_', "
                "'.', or '-'."
            )

    def _build(
        self,
        *,
        user_id: str,
        name: str,
        display_name: str | None,
        description: str,
        steps: list[WorkflowStep],
        enabled: bool,
        created_at=None,
    ) -> Workflow:
        display = (display_name or name).strip() or name
        if len(display) > MAX_DISPLAY_NAME_LEN:
            raise WorkflowValidationError(
                f"Display name must be at most {MAX_DISPLAY_NAME_LEN} characters."
            )
        desc = (description or "").strip()
        if len(desc) > MAX_DESCRIPTION_LEN:
            raise WorkflowValidationError(
                f"Description must be at most {MAX_DESCRIPTION_LEN} characters."
            )
        clean_steps = self._validate_steps(steps)

        now = _now()
        return Workflow(
            id=name,
            userId=user_id,
            name=name,
            displayName=display,
            description=desc,
            steps=clean_steps,
            enabled=bool(enabled),
            createdAt=created_at or now,
            updatedAt=now,
        )

    def _validate_steps(self, steps: list[WorkflowStep]) -> list[WorkflowStep]:
        if not steps:
            raise WorkflowValidationError("A workflow must have at least one step.")
        if len(steps) > MAX_STEPS:
            raise WorkflowValidationError(
                f"A workflow may have at most {MAX_STEPS} steps."
            )
        clean: list[WorkflowStep] = []
        for i, step in enumerate(steps):
            agent = (step.agent or "").strip().lower()
            if not agent:
                raise WorkflowValidationError(f"Step {i + 1}: agent is required.")
            if not NAME_RE.match(agent):
                raise WorkflowValidationError(
                    f"Step {i + 1}: '{agent}' is not a valid @mentionable agent name."
                )
            instruction = (step.instruction or "").strip()
            if not instruction:
                raise WorkflowValidationError(
                    f"Step {i + 1}: instruction is required."
                )
            if len(instruction) > MAX_INSTRUCTION_LEN:
                raise WorkflowValidationError(
                    f"Step {i + 1}: instruction must be at most "
                    f"{MAX_INSTRUCTION_LEN} characters."
                )
            # Guarantee the run input is never silently dropped: the first step
            # must consume {input}. Later steps thread {previous} automatically and
            # may legitimately ignore the original input.
            if i == 0 and INPUT_TOKEN not in instruction:
                raise WorkflowValidationError(
                    f"The first step's instruction must include the {INPUT_TOKEN} "
                    "placeholder so the run input reaches the workflow."
                )
            clean.append(WorkflowStep(agent=agent, instruction=instruction))
        return clean
