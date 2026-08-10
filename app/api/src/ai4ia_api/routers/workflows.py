"""User-defined workflow management + execution endpoints.

CRUD (``/api/workflows``) manages the caller's saved workflows; typed service
errors map to HTTP codes (422 validation / 409 conflict / 404 missing). The run
endpoint (``POST /api/workflows/{name}/run``) executes a workflow against a chat
session and persists the result like a normal assistant turn.

The run path mirrors the chat endpoint's hard invariants, in order:

1. Resolve the session (404) and the workflow (404 / 400 disabled).
2. Validate the run input: non-empty and bounded (422) so the pipeline prompt
   can't be unbounded.
3. Resolve the model — request override, else the session's standing model — and
   its single deployment (400 if none/unavailable). All steps share this one
   deployment so the whole run meters to a single model.
4. Refuse Responses-API models (422): workflow steps use the chat-completions
   tool loop, which has no Responses equivalent here yet.
5. Enforce entitlements BEFORE persisting anything (429 + Retry-After / 403), so a
   refused run leaves no dangling user message.
6. Persist the user message (attributed ``workflow:<name>``), run the pipeline
   (total — never raises), persist the assistant result, and meter the
   accumulated usage as ``status="complete"`` so consumed tokens always count
   against quota even when a late step failed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..agents.agent_catalog import AgentCatalog
from ..agents.capabilities import capability_builder_for_state
from ..catalog import ModelCatalog
from ..entitlements.service import EntitlementService
from ..gateway.client import ModelGatewayClient
from ..logging_setup import get_correlation_id
from ..sessions.models import Message, MessageRole, MessageStatus
from ..sessions.repository import SessionNotFoundError, SessionRepository
from ..usage.service import UsageService
from ..workflows.durable import (
    DurableScheduleAcceptanceUnknownError,
    DurableScheduleRejectedError,
    DurableWorkflowsUnavailableError,
    build_orchestration_payload,
    durable_message_ids,
    durable_run_fingerprint,
    durable_run_id,
)
from ..workflows.models import (
    MAX_RUN_INPUT_LEN,
    Workflow,
    WorkflowConflictError,
    WorkflowCreate,
    WorkflowNotFoundError,
    WorkflowUpdate,
    WorkflowValidationError,
)
from ..workflows.runner import run_workflow
from ..workflows.service import WorkflowService

router = APIRouter(prefix="/api", tags=["workflows"])


class WorkflowListResponse(BaseModel):
    workflows: list[Workflow]
    # Whether THIS deployment can honour `durable: true` on a run. Derived from the
    # same app.state.durable_workflows the run endpoint checks, so the advertisement
    # cannot disagree with what the request will actually do -- a separately-plumbed
    # web-side flag could, and would leave the runner offering an option that 422s.
    durableAvailable: bool = False


class WorkflowRunRequest(BaseModel):
    sessionId: str
    input: str
    model: str | None = None
    region: str | None = None
    dataZone: str | None = None
    # Per-REQUEST opt-in to durable execution, not a behaviour switch on the
    # operator flag: flipping a flag must never change the response shape an
    # existing client already depends on. Ignored-with-a-422 rather than
    # silently downgraded when the feature is off, so a caller that needs
    # durability is never told "done" by a run that cannot survive a restart.
    durable: bool = False
    idempotencyKey: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def require_durable_idempotency_key(self) -> WorkflowRunRequest:
        if self.durable and self.idempotencyKey is None:
            raise ValueError("idempotencyKey is required when durable is true")
        return self


class WorkflowRunAcceptedResponse(BaseModel):
    """202 body for a durable run. The assistant message does not exist yet."""

    sessionId: str
    runId: str
    status: str = "accepted"
    idempotencyKey: str


class WorkflowRunStatusResponse(BaseModel):
    runId: str
    status: str
    ok: bool | None = None
    text: str | None = None
    error: str | None = None


class WorkflowRunResponse(BaseModel):
    sessionId: str
    ok: bool
    message: Message


async def _claim_durable_run(
    repo: SessionRepository,
    *,
    user_id: str,
    user_message: Message,
    pending_assistant: Message,
) -> tuple[bool, Message]:
    """Atomically claim scheduling ownership for one deterministic run id."""

    def conflict() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was already used for a different workflow run.",
        )

    def records(messages: list[Message]) -> tuple[Message | None, Message | None]:
        return (
            next((message for message in messages if message.id == user_message.id), None),
            next(
                (message for message in messages if message.id == pending_assistant.id),
                None,
            ),
        )

    def validate(
        existing_user: Message | None, existing_assistant: Message | None
    ) -> None:
        if existing_user is not None and (
            existing_user.role is not MessageRole.user
            or existing_user.workflowRunId != user_message.workflowRunId
            or existing_user.workflowRunFingerprint
            != user_message.workflowRunFingerprint
            or existing_user.content != user_message.content
        ):
            raise conflict()
        if existing_assistant is not None and (
            existing_assistant.role is not MessageRole.assistant
            or existing_assistant.workflowRunId != pending_assistant.workflowRunId
            or existing_assistant.workflowRunFingerprint
            != pending_assistant.workflowRunFingerprint
        ):
            raise conflict()

    async def ensure_user(existing_user: Message | None) -> None:
        if existing_user is not None:
            return
        if await repo.add_message_if_absent(user_id, user_message):
            return
        latest_user, _ = records(
            await repo.list_messages(user_id, pending_assistant.sessionId)
        )
        validate(latest_user, pending_assistant)
        if latest_user is None:
            raise conflict()

    prior_user, prior_assistant = records(
        await repo.list_messages(user_id, pending_assistant.sessionId)
    )
    validate(prior_user, prior_assistant)
    if prior_assistant is not None:
        await ensure_user(prior_user)
        if prior_assistant.workflowRunStatus != "acceptance_unknown":
            return False, prior_assistant
        retry = prior_assistant.model_copy(
            update={
                "content": "",
                "status": MessageStatus.streaming,
                "workflowRunStatus": "pending",
            }
        )
        if await repo.replace_message_if_workflow_status(
            user_id,
            retry,
            expected_status="acceptance_unknown",
        ):
            return True, retry

    elif await repo.add_message_if_absent(user_id, pending_assistant):
        await ensure_user(prior_user)
        return True, pending_assistant
    else:
        # Both concurrent first requests may have read absence. Only the create
        # winner may schedule; this loser must reconcile but never turn a quickly
        # published acceptance_unknown state into a second scheduler call.
        for _attempt in range(3):
            messages = await repo.list_messages(user_id, pending_assistant.sessionId)
            existing_user, existing_assistant = records(messages)
            validate(existing_user, existing_assistant)
            if existing_assistant is None:
                continue
            return False, existing_assistant

    for _attempt in range(3):
        messages = await repo.list_messages(user_id, pending_assistant.sessionId)
        existing_user, existing_assistant = records(messages)
        validate(existing_user, existing_assistant)
        if existing_assistant is None:
            continue
        return False, existing_assistant

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Durable workflow state is temporarily unavailable.",
    )


def _service(request: Request) -> WorkflowService:
    return request.app.state.workflow_service


@router.get("/workflows", response_model=WorkflowListResponse)
async def list_workflows(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowListResponse:
    workflows = await _service(request).list_for(user.internal_user_id)
    return WorkflowListResponse(
        workflows=workflows,
        durableAvailable=getattr(request.app.state, "durable_workflows", None)
        is not None,
    )


@router.post(
    "/workflows", response_model=Workflow, status_code=status.HTTP_201_CREATED
)
async def create_workflow(
    request: Request,
    payload: WorkflowCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Workflow:
    try:
        return await _service(request).create(user.internal_user_id, payload)
    except WorkflowValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except WorkflowConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/workflows/{name}", response_model=Workflow)
async def update_workflow(
    request: Request,
    name: str,
    payload: WorkflowUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Workflow:
    try:
        return await _service(request).update(user.internal_user_id, name, payload)
    except WorkflowValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/workflows/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    request: Request,
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    await _service(request).delete(user.internal_user_id, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/workflows/{name}/run",
    response_model=None,
    responses={
        200: {"model": WorkflowRunResponse},
        202: {"model": WorkflowRunAcceptedResponse},
    },
)
async def run_workflow_endpoint(
    request: Request,
    name: str,
    body: WorkflowRunRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowRunResponse | JSONResponse:
    repo: SessionRepository = request.app.state.session_repo
    catalog: ModelCatalog = request.app.state.catalog
    gateway: ModelGatewayClient = request.app.state.gateway
    registry = request.app.state.tool_registry
    executor = request.app.state.tool_executor
    metering: UsageService = request.app.state.usage
    entitlements: EntitlementService = request.app.state.entitlements
    uid = user.internal_user_id

    try:
        session = await repo.get_session(uid, body.sessionId)
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    workflow = await _service(request).get(uid, name)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown workflow: {name}",
        )
    if not workflow.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Workflow '{workflow.name}' is disabled.",
        )

    run_input = (body.input or "").strip()
    if not run_input:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Input is required.",
        )
    if len(run_input) > MAX_RUN_INPUT_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Input must be at most {MAX_RUN_INPUT_LEN} characters.",
        )

    # Model precedence: explicit request override, else the session's standing
    # model. (There is no implicit catalog default — a session always carries a
    # model once it has chatted; an untouched session must be told which model to
    # run on.) All steps execute on this single deployment so the run meters to
    # one model.
    model_id = body.model or session.model
    if not model_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No model selected. Pass 'model' or set a model on the session first.",
        )
    deployment = catalog.resolve_deployment(
        model_id, region=body.region, data_zone=body.dataZone
    )
    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown or unavailable model: {model_id}",
        )
    entry = catalog.get(model_id)
    if entry is not None and entry.api == "responses":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Model '{model_id}' is served through the Responses API, which "
                "AI4IA does not yet support for workflow steps (they use the "
                "chat-completions tool loop). Choose a chat-completions model."
            ),
        )

    # Entitlements gate BEFORE any persistence/model consumption, so a refused run
    # leaves no dangling user message. Ships unlimited unless an admin set a limit.
    decision = await entitlements.check(uid)
    if not decision.allowed:
        headers = (
            {"Retry-After": str(decision.retry_after_seconds)}
            if decision.retry_after_seconds is not None
            else None
        )
        raise HTTPException(decision.code, decision.reason, headers=headers)

    agent_attr = f"workflow:{workflow.name}"
    correlation_id = get_correlation_id()

    # Bind the service only on the durable path so the branch below tests exactly
    # one fact. Testing `body.durable` in one place and `durable is not None` in
    # another leaves a silent-fallthrough shape: a mismatch would run the workflow
    # synchronously and return 200, answering a different question than was asked.
    durable_service = None
    if body.durable:
        durable_service = getattr(request.app.state, "durable_workflows", None)
        if durable_service is None:
            # Refuse rather than quietly running it synchronously. A caller asking
            # for durability is telling us the run must survive a restart.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Durable workflow execution is not enabled on this deployment. "
                    "Retry without 'durable', or ask an operator to set "
                    "AI4IA_DURABLE_WORKFLOWS_ENABLED=true."
                ),
            )

    if durable_service is not None:
        # The model validator guarantees this before any persistence. A server-
        # generated key cannot protect a request whose response was lost because
        # the caller never learned it to reuse on retry.
        idempotency_key = body.idempotencyKey
        if idempotency_key is None:  # defensive; rejected by the model validator
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="idempotencyKey is required when durable is true",
            )
        run_id = durable_run_id(
            uid,
            idempotency_key,
            scope=f"{body.sessionId}\0{workflow.name}",
        )
        user_message_id, assistant_message_id = durable_message_ids(run_id)
        payload = build_orchestration_payload(
            workflow,
            user_id=uid,
            session_id=body.sessionId,
            run_input=run_input,
            model_id=model_id,
            deployment=deployment,
            correlation_id=correlation_id,
            email=user.email,
            library_document_ids=(
                list(session.libraryDocumentIds)
                if session.libraryDocumentIds is not None
                else None
            ),
            run_id=run_id,
            assistant_message_id=assistant_message_id,
        )
        run_fingerprint = durable_run_fingerprint(payload)
        payload["context"]["runFingerprint"] = run_fingerprint
        user_message = Message(
            id=user_message_id,
            sessionId=body.sessionId,
            userId=uid,
            role=MessageRole.user,
            content=run_input,
            status=MessageStatus.complete,
            agent=agent_attr,
            workflowRunId=run_id,
            workflowRunStatus="accepted",
            workflowRunFingerprint=run_fingerprint,
        )
        pending_assistant = Message(
            id=assistant_message_id,
            sessionId=body.sessionId,
            userId=uid,
            role=MessageRole.assistant,
            content="",
            status=MessageStatus.streaming,
            model=deployment.deploymentName,
            agent=agent_attr,
            workflowRunId=run_id,
            workflowRunStatus="pending",
            workflowRunFingerprint=run_fingerprint,
        )
        owns_schedule, pending_assistant = await _claim_durable_run(
            repo,
            user_id=uid,
            user_message=user_message,
            pending_assistant=pending_assistant,
        )
        if not owns_schedule:
            if pending_assistant.workflowRunStatus == "schedule_failed":
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Durable workflow scheduling previously failed.",
                )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=WorkflowRunAcceptedResponse(
                    sessionId=body.sessionId,
                    runId=run_id,
                    idempotencyKey=idempotency_key,
                    status=(
                        "accepted"
                        if pending_assistant.workflowRunStatus
                        in {"accepted", "completed", "run_failed"}
                        else (
                            "acceptance_unknown"
                            if pending_assistant.workflowRunStatus
                            == "acceptance_unknown"
                            else "pending"
                        )
                    ),
                ).model_dump(),
            )

        try:
            await durable_service.schedule(payload, user_id=uid, run_id=run_id)
        except (DurableScheduleRejectedError, DurableWorkflowsUnavailableError) as exc:
            failed = pending_assistant.model_copy(
                update={
                    "status": MessageStatus.error,
                    "content": (
                        "The durable workflow could not be scheduled. Retry with a "
                        "new idempotency key after the service recovers."
                    ),
                    "workflowRunStatus": "schedule_failed",
                }
            )
            await repo.replace_message_if_workflow_status(
                uid, failed, expected_status="pending"
            )
            await repo.touch_session(uid, session.id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Durable workflow execution is temporarily unavailable.",
            ) from exc
        except DurableScheduleAcceptanceUnknownError:
            unknown = pending_assistant.model_copy(
                update={"workflowRunStatus": "acceptance_unknown"}
            )
            await repo.replace_message_if_workflow_status(
                uid, unknown, expected_status="pending"
            )
            await repo.touch_session(uid, session.id)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=WorkflowRunAcceptedResponse(
                    sessionId=body.sessionId,
                    runId=run_id,
                    idempotencyKey=idempotency_key,
                    status="acceptance_unknown",
                ).model_dump(),
            )
        accepted = pending_assistant.model_copy(
            update={"workflowRunStatus": "accepted"}
        )
        await repo.replace_message_if_workflow_status(
            uid, accepted, expected_status="pending"
        )
        await repo.touch_session(uid, session.id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=WorkflowRunAcceptedResponse(
                sessionId=body.sessionId,
                runId=run_id,
                idempotencyKey=idempotency_key,
            ).model_dump(),
        )

    await repo.add_message(
        uid,
        Message(
            sessionId=body.sessionId,
            userId=uid,
            role=MessageRole.user,
            content=run_input,
            status=MessageStatus.complete,
            agent=agent_attr,
        ),
    )

    # Compose the caller's user agents over the curated catalog so a step can
    # reference either. The runner is total: it never raises, returning ok=False
    # with the failure text and the usage consumed so far.
    composed: AgentCatalog = await request.app.state.agent_service.catalog_for(
        uid, request.app.state.agents
    )
    result = await run_workflow(
        workflow,
        run_input=run_input,
        composed=composed,
        deployment=deployment.deploymentName,
        gateway=gateway,
        registry=registry,
        executor=executor,
        capabilities=capability_builder_for_state(
            request.app.state,
            user_id=uid,
            session_id=body.sessionId,
            email=user.email,
            allowed_document_ids=(
                set(session.libraryDocumentIds)
                if session.libraryDocumentIds is not None
                else None
            ),
        ),
        correlation_id=correlation_id,
    )

    assistant = Message(
        sessionId=body.sessionId,
        userId=uid,
        role=MessageRole.assistant,
        content=result.text,
        status=MessageStatus.complete,
        model=deployment.deploymentName,
        agent=agent_attr,
    )
    await repo.add_message(uid, assistant)

    # Meter accumulated usage as complete so consumed tokens count against quota
    # even when a late step failed (no fail-late-to-dodge-billing hole). Skip
    # entirely when the run failed before any model call (e.g. step 1's agent was
    # unknown/disabled/an orchestrator) — a zero-work failure must not consume a
    # request slot or pollute the usage ledger.
    if result.usage.calls > 0:
        await metering.record_completion(
            user_id=uid,
            session_id=body.sessionId,
            model_id=model_id,
            deployment=deployment,
            usage=result.usage,
            status="complete",
            agent=agent_attr,
            correlation_id=correlation_id,
        )

    await repo.touch_session(uid, session.id)
    return WorkflowRunResponse(sessionId=body.sessionId, ok=result.ok, message=assistant)


@router.get("/workflows/runs/{run_id}", response_model=WorkflowRunStatusResponse)
async def get_workflow_run(
    request: Request,
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowRunStatusResponse:
    """Poll a durable run started with ``durable: true``.

    The run's assistant message lands in the session like any other turn when it
    completes; this endpoint exists so a caller can tell "still running" apart
    from "finished and failed" without diffing the transcript.
    """
    durable = getattr(request.app.state, "durable_workflows", None)
    if durable is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Durable workflow execution is not enabled on this deployment.",
        )
    try:
        state = await durable.get_status(run_id, user_id=user.internal_user_id)
    except DurableWorkflowsUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Durable workflow execution is temporarily unavailable.",
        ) from exc
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown run."
        )
    return WorkflowRunStatusResponse(
        runId=state.runId,
        status=state.status,
        ok=state.ok,
        text=state.text,
        error=state.error,
    )
