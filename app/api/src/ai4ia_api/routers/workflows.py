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
from pydantic import BaseModel

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..agents.agent_catalog import AgentCatalog
from ..catalog import ModelCatalog
from ..entitlements.service import EntitlementService
from ..gateway.client import ModelGatewayClient
from ..logging_setup import get_correlation_id
from ..sessions.models import Message, MessageRole, MessageStatus
from ..sessions.repository import SessionNotFoundError, SessionRepository
from ..usage.service import UsageService
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


class WorkflowRunRequest(BaseModel):
    sessionId: str
    input: str
    model: str | None = None
    region: str | None = None
    dataZone: str | None = None


class WorkflowRunResponse(BaseModel):
    sessionId: str
    ok: bool
    message: Message


def _service(request: Request) -> WorkflowService:
    return request.app.state.workflow_service


@router.get("/workflows", response_model=WorkflowListResponse)
async def list_workflows(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowListResponse:
    workflows = await _service(request).list_for(user.internal_user_id)
    return WorkflowListResponse(workflows=workflows)


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
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except WorkflowConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


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
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except WorkflowNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete("/workflows/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    request: Request,
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    await _service(request).delete(user.internal_user_id, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/workflows/{name}/run", response_model=WorkflowRunResponse)
async def run_workflow_endpoint(
    request: Request,
    name: str,
    body: WorkflowRunRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WorkflowRunResponse:
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    workflow = await _service(request).get(uid, name)
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown workflow: {name}")
    if not workflow.enabled:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Workflow '{workflow.name}' is disabled."
        )

    run_input = (body.input or "").strip()
    if not run_input:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Input is required.")
    if len(run_input) > MAX_RUN_INPUT_LEN:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Input must be at most {MAX_RUN_INPUT_LEN} characters.",
        )

    # Model precedence: explicit request override, else the session's standing
    # model. (There is no implicit catalog default — a session always carries a
    # model once it has chatted; an untouched session must be told which model to
    # run on.) All steps execute on this single deployment so the run meters to
    # one model.
    model_id = body.model or session.model
    if not model_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No model selected. Pass 'model' or set a model on the session first.",
        )
    deployment = catalog.resolve_deployment(
        model_id, region=body.region, data_zone=body.dataZone
    )
    if deployment is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown or unavailable model: {model_id}"
        )
    entry = catalog.get(model_id)
    if entry is not None and entry.api == "responses":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            (
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

    session.updatedAt = assistant.createdAt
    await repo.update_session(session)
    return WorkflowRunResponse(sessionId=body.sessionId, ok=result.ok, message=assistant)
