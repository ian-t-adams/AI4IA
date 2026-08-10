"""Durable execution for workflow runs (Azure Durable Task Scheduler).

Default OFF. When enabled, a caller may opt a single run into durable execution
with ``"durable": true``; the endpoint returns 202 + a run id and the
orchestration survives replica loss, deploys, and scale-in. Without it a
workflow run is a synchronous multi-model-call loop inside one HTTP request, so
losing the replica loses the run with no retry, no record, and no way for the
caller to find out.

Why Durable Task Scheduler and not Durable Functions: DTS is deliberately
decoupled from any compute host, so the worker runs inside the Container App
already deployed here. Durable Functions would couple durability to Functions
compute — a second platform, pipeline, and RBAC surface for no gain.

**The async constraint, verified against durabletask 1.9.0 rather than assumed.**
The Python SDK does not await user coroutines: ``_ActivityExecutor.execute`` is a
sync method that calls ``fn(ctx, input)`` unawaited and hands the result straight
to the serializer. Registering an ``async def`` activity therefore fails with
``TypeError: Failed to serialize object of type 'coroutine'`` — loudly, which is
the one mercy here. AI4IA is entirely async, so every activity below is a **sync**
function that bridges onto the API's own event loop with
``asyncio.run_coroutine_threadsafe``.

That bridge target matters. Activities execute on the worker's thread pool, and
the SDK's worker runs on its *own* event loop, not the app's. The app's Cosmos,
httpx, and credential clients are bound to the app loop, so the activity must
hand work back to that loop rather than spin up a private one — a per-activity
``asyncio.run()`` would build and discard a fresh client stack on every step and
could not reuse a single connection pool.

Orchestrator determinism: ``_workflow_orchestrator`` only sequences activity
calls off its input. It performs no I/O, reads no clock, and resolves nothing
from app state — everything it needs is captured in the orchestration input at
schedule time, so a replay years later reaches the same decisions.

Model calls issued from a durable run still go proxy -> APIM -> Foundry exactly
like any other turn (the activity calls the same ``run_workflow_step``). Moving
execution off the request path changes *where the loop runs*, not its egress.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..agents.capabilities import capability_builder_for_state
from ..catalog import DeploymentOption
from ..sessions.models import Message, MessageRole, MessageStatus
from ..usage.models import TokenUsage, UsageTarget
from .models import MAX_STEPS, Workflow
from .runner import run_workflow_step

logger = logging.getLogger(__name__)

ORCHESTRATOR_NAME = "ai4ia_workflow_run"
_STEP_ACTIVITY = "ai4ia_workflow_step"
_PERSIST_ACTIVITY = "ai4ia_workflow_persist"

# Ceiling on how long one activity may block a worker thread waiting on the app
# loop. A workflow step is a bounded agent turn (<= _STEP_MAX_ITERS model<->tool
# round trips), so exceeding this means the app loop is wedged, not that the step
# is legitimately slow. Bounded so a stuck step cannot pin a worker thread
# forever and starve every other run.
_ACTIVITY_BRIDGE_TIMEOUT_SECONDS = 900

# Separates the owning user id from the random part of a run id. A colon cannot
# appear in an internal user id (they are UUIDs), so ``partition`` recovers the
# owner exactly rather than by prefix match.
_RUN_ID_SEPARATOR = ":"

# Orchestration states the scheduler will not move again. Mirrors the web's
# TERMINAL_RUN_STATUSES (app/web/src/lib/api.ts) so the two ends agree on when a
# run has stopped; an unrecognised status counts as still-running on both sides.
_TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "TERMINATED"})

# The Durable Task Scheduler rejects any single JSON-serialized orchestration
# payload over 1 MB. The orchestrator's return value is the binding surface: it
# carries EVERY step's output at once (``previous`` is replaced each step, so it
# is bounded by one result, but ``trace`` accumulates all of them). Six steps of
# unbounded model output clear 1 MB easily -- a reasoning model can emit >100k
# tokens in one turn -- and the SDK would reject the payload only at the END of
# the run, after all the model work had already been paid for.
#
# The per-step budget is DERIVED from MAX_STEPS rather than written as a literal
# so that raising the step cap tightens the per-step allowance automatically. A
# hardcoded budget would keep passing its own test while quietly making the
# payload illegal again.
_TRACE_BUDGET_BYTES = 720_000
_MAX_STEP_TEXT_BYTES = _TRACE_BUDGET_BYTES // MAX_STEPS
_TRUNCATION_MARKER = "\n\n[truncated: durable run payload limit]"


@dataclass
class DurableRunStatus:
    """Caller-facing status of a durable run."""

    runId: str
    status: str
    ok: bool | None = None
    text: str | None = None
    error: str | None = None


class DurableWorkflowsUnavailableError(RuntimeError):
    """Durable execution was requested but is not configured/running."""


class DurableScheduleAcceptanceUnknownError(RuntimeError):
    """The scheduler call failed after acceptance became unknowable."""


class DurableScheduleRejectedError(RuntimeError):
    """The scheduler definitively rejected the run before acceptance."""


_DEFINITE_SCHEDULE_CODES = frozenset(
    {
        "FAILED_PRECONDITION",
        "INVALID_ARGUMENT",
        "NOT_FOUND",
        "OUT_OF_RANGE",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "UNAUTHENTICATED",
        "UNIMPLEMENTED",
    }
)
_DEFINITE_AUTH_ERROR_NAMES = frozenset(
    {"ClientAuthenticationError", "CredentialUnavailableError"}
)


def _schedule_error_code(exc: Exception) -> str | None:
    code_method = getattr(exc, "code", None)
    if not callable(code_method):
        return None
    try:
        code = code_method()
    except Exception:  # noqa: BLE001 - classification must preserve original error
        return None
    name = getattr(code, "name", None)
    if isinstance(name, str):
        return name.upper()
    text = str(code).rsplit(".", 1)[-1].upper()
    return text if text else None


def _is_definite_schedule_rejection(exc: Exception) -> bool:
    return (
        isinstance(exc, (TypeError, ValueError))
        or type(exc).__name__ in _DEFINITE_AUTH_ERROR_NAMES
        or _schedule_error_code(exc) in _DEFINITE_SCHEDULE_CODES
    )


def durable_run_id(
    user_id: str,
    idempotency_key: str | None = None,
    *,
    scope: str = "",
) -> str:
    if idempotency_key is None:
        suffix = uuid4().hex
    else:
        suffix = hashlib.sha256(
            f"{user_id}\0{scope}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()
    return f"{user_id}{_RUN_ID_SEPARATOR}{suffix}"


def durable_message_ids(run_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"wf-{digest}-user", f"wf-{digest}-assistant"


def durable_run_fingerprint(payload: dict[str, Any]) -> str:
    """Hash only execution-defining orchestration input, never request metadata."""
    context = dict(payload.get("context") or {})
    for key in (
        "assistantMessageId",
        "correlationId",
        "runFingerprint",
        "runId",
    ):
        context.pop(key, None)
    canonical = {"steps": payload.get("steps") or [], "context": context}
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DurableWorkflowService:
    """Owns the DTS client + in-process worker for the lifetime of the app.

    Constructed only when ``durable_workflows_enabled`` is set, so a default
    deployment builds no gRPC channel and no worker thread.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        task_hub: str,
        app_state: Any,
        credential: Any | None = None,
        async_credential: Any | None = None,
        timeout_seconds: int = 1800,
    ) -> None:
        self._endpoint = endpoint
        self._task_hub = task_hub
        self._credential = credential
        self._async_credential = async_credential
        self._state = app_state
        self._timeout_seconds = timeout_seconds
        self._client: Any = None
        self._worker: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Build the client, register the orchestration, and start the worker.

        Imports the SDK lazily so a default (flag-off) deployment never pays for
        the grpc/protobuf import. ``tests/test_lazy_imports_are_declared.py``
        re-derives lazy imports from the AST, so this stays covered.

        The credential is the **sync** ``azure.identity.DefaultAzureCredential``,
        not the ``azure.identity.aio`` one used everywhere else in this app: the
        SDK types ``token_credential`` as ``azure.core.credentials.TokenCredential``
        and the worker calls it from its own thread, off the app's event loop.
        Passing the async variant type-checks nowhere useful and fails at token
        acquisition.
        """
        from durabletask.azuremanaged.client import AsyncDurableTaskSchedulerClient
        from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker

        # TWO credentials, deliberately. The client runs on FastAPI's loop over
        # grpc.aio and needs the ASYNC credential; the worker runs on its own
        # thread and loop and needs the SYNC one. Sharing either across the two
        # fails at token acquisition, not at import, so it would surface as a
        # confusing auth error on first use. This split is what Microsoft's own
        # FastAPI sample does.
        if self._async_credential is None:
            from azure.identity.aio import DefaultAzureCredential as AsyncCredential

            self._async_credential = AsyncCredential()
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()

        self._loop = asyncio.get_running_loop()

        self._client = AsyncDurableTaskSchedulerClient(
            host_address=self._endpoint,
            taskhub=self._task_hub,
            token_credential=self._async_credential,
        )
        worker = DurableTaskSchedulerWorker(
            host_address=self._endpoint,
            taskhub=self._task_hub,
            token_credential=self._credential,
        )
        worker.add_orchestrator(self._build_orchestrator())
        worker.add_activity(self._build_step_activity())
        worker.add_activity(self._build_persist_activity())
        worker.start()
        self._worker = worker
        logger.info(
            "durable workflows started (taskHub=%s)",
            self._task_hub,
            extra={"ai4ia_task_hub": self._task_hub},
        )

    async def stop(self) -> None:
        """Stop the worker and close the client. Never raises: shutdown must not
        be able to fail an otherwise-clean app shutdown."""
        if self._worker is not None:
            try:
                self._worker.stop()  # sync: the worker owns its own thread
            except Exception:  # noqa: BLE001 — shutdown is best-effort.
                logger.exception("durable workflows: worker stop() failed")
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                logger.exception("durable workflows: client close() failed")
        if self._async_credential is not None:
            try:
                await self._async_credential.close()
            except Exception:  # noqa: BLE001
                logger.exception("durable workflows: async credential close failed")
        self._worker = None
        self._client = None

    # -- scheduling --------------------------------------------------------

    async def schedule(
        self,
        payload: dict[str, Any],
        *,
        user_id: str,
        run_id: str | None = None,
    ) -> str:
        """Start a durable run and return its id. Raises if not running.

        The run id is ``<userId>:<random>``, generated here and never taken from
        the request. That makes ownership an O(1) exact check on read (see
        :meth:`get_status`) without fetching the orchestration payload — a run id
        is a bearer-ish handle otherwise, and workflow output is user content.
        """
        if self._client is None:
            raise DurableWorkflowsUnavailableError(
                "Durable workflows are not running."
            )
        instance_id = run_id or durable_run_id(user_id)
        owner, _, remainder = instance_id.partition(_RUN_ID_SEPARATOR)
        if not remainder or owner != user_id:
            raise ValueError("run_id must be owned by user_id")
        try:
            await self._client.schedule_new_orchestration(
                ORCHESTRATOR_NAME,
                input=payload,
                instance_id=instance_id,
            )
        except Exception as exc:
            code = _schedule_error_code(exc)
            if code == "ALREADY_EXISTS":
                return instance_id
            if _is_definite_schedule_rejection(exc):
                raise DurableScheduleRejectedError(
                    "Durable workflow scheduling was rejected."
                ) from exc
            # A transport failure can happen after DTS accepted the instance. A
            # point read converts that ambiguity to success when possible; if the
            # read is also inconclusive, the caller must retain pending state and
            # retry the SAME instance id rather than minting another run.
            try:
                state = await self._client.get_orchestration_state(instance_id)
            except Exception:
                state = None
            if state is None:
                raise DurableScheduleAcceptanceUnknownError(
                    "Durable workflow acceptance is unknown."
                ) from exc
        return instance_id

    async def get_status(self, run_id: str, *, user_id: str) -> DurableRunStatus | None:
        """Read a run's current state, or None when the id is unknown.

        Returns None (not a 403) for another user's run: distinguishing "not
        yours" from "does not exist" would confirm the existence of other users'
        runs to anyone able to guess an id.
        """
        if self._client is None:
            raise DurableWorkflowsUnavailableError(
                "Durable workflows are not running."
            )
        owner, _, remainder = run_id.partition(_RUN_ID_SEPARATOR)
        if not remainder or owner != user_id:
            return None

        state = await self._client.get_orchestration_state(run_id)
        if state is None:
            return None

        # Direct attribute access, not getattr-with-default: these fields were
        # verified present on durabletask 1.9.0's OrchestrationState, and a
        # future rename must fail loudly here rather than silently reporting
        # status "None" with no output. `get_output()` deserializes;
        # `serialized_output` is the raw JSON string.
        raw = state.runtime_status
        status = getattr(raw, "name", None) or str(raw)
        failure = state.failure_details

        # Enforce the configured run budget. Until this existed,
        # `durable_workflow_timeout_seconds` was plumbed all the way from Bicep
        # through deploy.yml into Settings and then read by nothing: an operator
        # lowering it to bound stuck runs got no behaviour change at all, and a
        # wedged orchestration polled RUNNING forever. The status endpoint is the
        # right place because that is what the setting's own documentation
        # promises, and it is the only code that sees every poll.
        if status not in _TERMINAL_RUN_STATUSES:
            overdue = self._overdue_seconds(state)
            if overdue is not None:
                await self._terminate_overdue(run_id, overdue)
                # Reported as TERMINATED rather than merely "late" because the
                # run really was stopped — saying "failed" while leaving it to
                # finish and write a message later would be the same
                # failure-as-success lie in reverse.
                return DurableRunStatus(
                    runId=run_id,
                    status="TERMINATED",
                    ok=False,
                    error=(
                        f"The run exceeded its {self._timeout_seconds}s budget "
                        "and was stopped."
                    ),
                )

        result = DurableRunStatus(runId=run_id, status=status)
        if failure is not None:
            result.error = getattr(failure, "message", str(failure))
        parsed = state.get_output()
        if isinstance(parsed, dict):
            result.ok = parsed.get("ok")
            result.text = parsed.get("text")
        return result

    def _overdue_seconds(self, state: Any) -> float | None:
        """Seconds past the configured budget, or None while still inside it.

        A non-positive budget disables enforcement rather than expiring every run
        instantly, so a misconfigured 0 degrades to today's unbounded behaviour
        instead of killing work.
        """
        if self._timeout_seconds <= 0:
            return None
        created = state.created_at
        if not isinstance(created, datetime):
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        overdue = (
            datetime.now(timezone.utc) - created
        ).total_seconds() - self._timeout_seconds
        return overdue if overdue > 0 else None

    async def _terminate_overdue(self, run_id: str, overdue_seconds: float) -> None:
        """Stop an over-budget run, best-effort.

        Best-effort because reporting the breach is what the caller can act on;
        a scheduler that refuses the terminate must not also cost them the
        status. The failure is logged rather than swallowed silently.
        """
        logger.warning(
            "durable run %s exceeded its %ss budget by %.0fs; terminating",
            run_id,
            self._timeout_seconds,
            overdue_seconds,
        )
        try:
            await self._client.terminate_orchestration(run_id)
        except Exception:  # noqa: BLE001 - the breach still has to be reported
            logger.exception(
                "failed to terminate over-budget durable run %s", run_id
            )

    # -- orchestrator ------------------------------------------------------

    def _build_orchestrator(self):
        """Return the orchestrator generator under its registered name.

        Deliberately closes over nothing but the name: an orchestrator is
        replayed from history, so anything it reads from app state would be a
        determinism hazard.
        """

        def ai4ia_workflow_run(ctx, payload: dict[str, Any]):
            steps: list[dict[str, Any]] = payload.get("steps") or []
            previous = ""
            usage_total: dict[str, Any] = {}
            trace: list[dict[str, Any]] = []
            ok = True
            text = ""

            for index, step in enumerate(steps):
                outcome = yield ctx.call_activity(
                    _STEP_ACTIVITY,
                    input={
                        "step": step,
                        "index": index,
                        "previous": previous,
                        "context": payload["context"],
                    },
                )
                # Bound every string that survives the step before it lands in
                # `trace` (which accumulates across the whole run) or in
                # `previous` (which becomes the next activity's input). Pure
                # string math on the activity's own output, so it stays
                # replay-deterministic.
                result = outcome["result"]
                result["text"] = _truncate_for_payload(result.get("text") or "")
                if result.get("error"):
                    result["error"] = _truncate_for_payload(result["error"])
                trace.append(result)
                usage_total = _merge_usage(usage_total, outcome.get("usage") or {})
                if outcome.get("fatal"):
                    ok = False
                    text = result.get("error") or "Workflow step failed."
                    break
                previous = result.get("text") or ""
                text = previous

            # Persist inside the orchestration, not at the caller: a durable run
            # outlives the request that started it, so nothing on the caller's
            # side is still around to write the result.
            yield ctx.call_activity(
                _PERSIST_ACTIVITY,
                input={
                    "context": payload["context"],
                    "ok": ok,
                    "text": text,
                    "usage": usage_total,
                },
            )
            return {"ok": ok, "text": text, "steps": trace, "usage": usage_total}

        ai4ia_workflow_run.__name__ = ORCHESTRATOR_NAME
        return ai4ia_workflow_run

    # -- activities --------------------------------------------------------

    def _run_on_app_loop(self, coro) -> Any:
        """Run ``coro`` on the API's event loop from a worker thread.

        See the module docstring: activities are sync by SDK design, and the
        app's async clients are bound to the app loop.
        """
        loop = self._loop
        if loop is None:  # pragma: no cover - start() always sets it
            # Close it explicitly: an abandoned coroutine is only reported at
            # GC time, as a bare "was never awaited" RuntimeWarning with no
            # traceback, which would obscure the real error being raised here.
            coro.close()
            raise DurableWorkflowsUnavailableError("Durable worker is not started.")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=_ACTIVITY_BRIDGE_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            future.cancel()
            raise

    def _build_step_activity(self):
        service = self

        def ai4ia_workflow_step(ctx, payload: dict[str, Any]) -> dict[str, Any]:
            index = payload.get("index") or 0
            try:
                context = payload["context"]
                step = _step_from_dict(payload["step"])
                return service._run_on_app_loop(
                    service._execute_step(
                        step=step,
                        index=index,
                        previous=payload.get("previous") or "",
                        context=context,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - the activity must be total
                # `run_workflow_step` never raises, but this wrapper does work
                # outside it that can: `_step_from_dict`, `catalog_for`, and the
                # activity bridge's own timeout. Letting any of those escape
                # fails the ORCHESTRATION, which skips the persist activity — so
                # the session keeps a user message with no reply forever and the
                # tokens already spent by earlier steps are never metered. The
                # in-request path cannot lose either (it catches per step and
                # always persists + meters), and accounting must not depend on
                # which execution mode ran the workflow.
                agent = (payload.get("step") or {}).get("agent")
                logger.exception(
                    "durable workflow step %s (agent %s) failed outside the runner",
                    index + 1,
                    agent,
                )
                return {
                    "result": {
                        "agent": agent,
                        "ok": False,
                        "text": "",
                        # Same shape runner.py uses for a step that raised, so
                        # the "Step N:" prefix stays parseable for attribution
                        # and no internal detail reaches the user.
                        "error": f"Step {index + 1}: the run could not continue "
                        f"({type(exc).__name__}).",
                        "iterations": 0,
                    },
                    "usage": _usage_to_dict(TokenUsage.empty()),
                    "fatal": True,
                }

        ai4ia_workflow_step.__name__ = _STEP_ACTIVITY
        return ai4ia_workflow_step

    def _build_persist_activity(self):
        service = self

        def ai4ia_workflow_persist(ctx, payload: dict[str, Any]) -> dict[str, Any]:
            return service._run_on_app_loop(service._persist(payload))

        ai4ia_workflow_persist.__name__ = _PERSIST_ACTIVITY
        return ai4ia_workflow_persist

    # -- the async bodies the activities bridge to -------------------------

    async def _execute_step(
        self, *, step, index: int, previous: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        state = self._state
        uid = context["userId"]
        composed = await state.agent_service.catalog_for(uid, state.agents)
        library_ids = context.get("libraryDocumentIds")
        outcome = await run_workflow_step(
            step,
            index=index,
            workflow_name=context["workflowName"],
            run_input=context["runInput"],
            previous=previous,
            composed=composed,
            deployment=context["deployment"],
            gateway=state.gateway,
            registry=state.tool_registry,
            executor=state.tool_executor,
            # Same builder the in-request path uses, off the same app state, so a
            # step's tool surface cannot depend on which execution mode ran it.
            capabilities=capability_builder_for_state(
                state,
                user_id=uid,
                session_id=context.get("sessionId"),
                email=context.get("email"),
                allowed_document_ids=(
                    set(library_ids) if library_ids is not None else None
                ),
            ),
            correlation_id=context.get("correlationId"),
        )
        return {
            "result": {
                "agent": outcome.result.agent,
                "ok": outcome.result.ok,
                "text": outcome.result.text,
                "error": outcome.result.error,
                "iterations": outcome.result.iterations,
            },
            "usage": _usage_to_dict(outcome.usage),
            "fatal": outcome.fatal,
        }

    async def _persist(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write the assistant message and meter usage, mirroring the in-request
        path so a durable run is indistinguishable in storage from a sync one."""
        state = self._state
        context = payload["context"]
        uid = context["userId"]
        session_id = context["sessionId"]
        agent_attr = f"workflow:{context['workflowName']}"

        assistant = Message(
            id=context.get("assistantMessageId") or uuid4().hex,
            sessionId=session_id,
            userId=uid,
            role=MessageRole.assistant,
            content=payload.get("text") or "",
            status=(
                MessageStatus.complete
                if payload.get("ok")
                else MessageStatus.error
            ),
            model=context["deployment"],
            agent=agent_attr,
            workflowRunId=context.get("runId"),
            workflowRunStatus=(
                "completed" if payload.get("ok") else "run_failed"
            )
            if context.get("runId")
            else None,
            workflowRunFingerprint=context.get("runFingerprint"),
        )
        await state.session_repo.upsert_message(uid, assistant)

        usage = _usage_from_dict(payload.get("usage") or {})
        # Same rule as the in-request path: meter only when a model call actually
        # happened, so a zero-work failure neither consumes a request slot nor
        # pollutes the ledger.
        if usage.calls > 0:
            await state.usage.record_completion(
                user_id=uid,
                session_id=session_id,
                model_id=context["modelId"],
                # The descriptor frozen at schedule time, NOT a fresh resolve.
                # Re-resolving dropped the caller's region/data-zone choice and
                # skipped metering entirely when the id no longer resolved.
                target=_usage_target_from_context(context, state.catalog),
                usage=usage,
                status="complete",
                agent=agent_attr,
                correlation_id=context.get("correlationId"),
            )
        await state.session_repo.touch_session(uid, session_id)
        return {"persisted": True}


def build_orchestration_payload(
    workflow: Workflow,
    *,
    user_id: str,
    session_id: str,
    run_input: str,
    model_id: str,
    deployment: DeploymentOption,
    correlation_id: str | None,
    email: str | None = None,
    library_document_ids: list[str] | None = None,
    run_id: str | None = None,
    assistant_message_id: str | None = None,
) -> dict[str, Any]:
    """Freeze everything a run needs into its orchestration input.

    A durable orchestration replays from history, so it must not re-resolve the
    workflow, the model, or the agent catalog later — a definition edited
    mid-run would otherwise change the meaning of an in-flight run.

    That is also why ``email`` and ``libraryDocumentIds`` are frozen here rather
    than re-read from the session inside the activity: they scope which documents
    a step's ``fetch_document`` tool can reach, and re-reading them would let a
    session edited mid-run widen or narrow an in-flight run's data access.

    The same rule governs the **usage target**. The whole ``DeploymentOption`` is
    frozen, not just its name: the caller resolved it with an explicit region and
    data zone, and most catalog entries have options in more than one data zone.
    ``_persist`` used to re-resolve from ``modelId`` alone, which falls back to
    the first option — so a run the user pinned to Sweden metered as East US,
    and a model id that stopped resolving mid-run metered as nothing at all.
    """
    return {
        # Serialized by the model itself, not a hand-listed subset: a field added
        # to WorkflowStep later must survive the durable boundary automatically.
        # `extraTools` was dropped here when it was added, so a durable run
        # executed with fewer tools than the identical in-request run — silently,
        # because a step with no tools still answers 200 and the model narrates
        # what it would have done.
        "steps": [s.model_dump(mode="json") for s in workflow.steps],
        "context": {
            "userId": user_id,
            "sessionId": session_id,
            "workflowName": workflow.name,
            "runInput": run_input,
            "modelId": model_id,
            "deployment": deployment.deploymentName,
            # Frozen usage descriptor. Flat scalars rather than a nested object so
            # the orchestration history stays plain JSON.
            "usageTarget": {
                "provider": "azure_openai",
                "deployment": deployment.deploymentName,
                "target": deployment.deploymentName,
                "region": deployment.region,
                "dataZone": deployment.dataZone,
            },
            "correlationId": correlation_id,
            "email": email,
            "libraryDocumentIds": library_document_ids,
            "runId": run_id,
            "assistantMessageId": assistant_message_id,
        },
    }


def _step_from_dict(raw: dict[str, Any]):
    """Rebuild a step from its orchestration payload.

    Validated by the model rather than reconstructed field by field, so this
    stays the exact inverse of :func:`build_orchestration_payload`. An
    orchestration whose history predates a new field still replays: the field
    is absent from the stored dict and falls back to its default.
    """
    from .models import WorkflowStep

    return WorkflowStep.model_validate(raw)


def _usage_to_dict(usage: TokenUsage) -> dict[str, Any]:
    return {
        "prompt": usage.prompt,
        "completion": usage.completion,
        "total": usage.total,
        "known": usage.known,
        "complete": usage.complete,
        "calls": usage.calls,
    }


def _usage_target_from_context(context: dict[str, Any], catalog: Any) -> UsageTarget:
    """Rebuild the usage descriptor frozen at schedule time.

    Falls back to re-resolving from ``modelId`` ONLY for orchestrations whose
    history predates the frozen descriptor — durability means a run scheduled by
    the previous revision is still replaying after a deploy, and dropping its
    metering would be exactly the hole this function exists to close. The
    fallback is logged, because it is a lossy answer: it cannot recover the
    region/data zone the caller originally asked for.
    """
    frozen = context.get("usageTarget")
    if isinstance(frozen, dict):
        return UsageTarget(
            provider=frozen.get("provider") or "azure_openai",
            deployment=frozen.get("deployment"),
            target=frozen.get("target"),
            region=frozen.get("region"),
            dataZone=frozen.get("dataZone"),
        )

    model_id = context.get("modelId") or ""
    option = catalog.resolve_deployment(model_id)
    if option is not None:
        logger.info(
            "durable run has no frozen usage target; re-resolved %s "
            "(region/data zone may differ from the scheduled run)",
            model_id,
        )
        return UsageTarget.from_deployment(option)

    # Last resort. The tokens were spent, so a ledger row with the deployment
    # name we do know beats silently metering nothing.
    logger.warning(
        "durable run has no frozen usage target and %s no longer resolves; "
        "metering with the recorded deployment name only",
        model_id,
    )
    deployment_name = context.get("deployment")
    return UsageTarget(deployment=deployment_name, target=deployment_name)


def _usage_from_dict(raw: dict[str, Any]) -> TokenUsage:
    if not raw:
        return TokenUsage.empty()
    return TokenUsage(
        prompt=raw.get("prompt") or 0,
        completion=raw.get("completion") or 0,
        total=raw.get("total") or 0,
        known=bool(raw.get("known")),
        # Absent means "nothing folded yet", which is complete by definition —
        # matching TokenUsage.empty(). Defaulting to False would mark every run
        # incomplete and silently flag all durable usage as unreliable.
        complete=bool(raw.get("complete", True)),
        calls=raw.get("calls") or 0,
    )


def _merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Fold two serialized usages using ``TokenUsage.add`` itself.

    Deliberately round-trips through the model instead of summing the dicts
    inline: ``known`` is an OR, ``complete`` is an AND that also demands the
    folded call be known, and re-deriving those rules here is how the durable
    and in-request ledgers would drift into disagreeing about the same run.
    Pure computation, so it stays safe to call from the orchestrator.
    """
    return _usage_to_dict(_usage_from_dict(left).add(_usage_from_dict(right)))


def _truncate_for_payload(text: str, limit: int = _MAX_STEP_TEXT_BYTES) -> str:
    """Bound one step's text so the orchestration payload stays under 1 MB.

    Measured in UTF-8 BYTES, not characters, because that is what the scheduler
    counts. The cut can land mid-character, so the tail is decoded with
    ``errors="ignore"`` to drop the partial sequence rather than raise.

    Truncation is visible on purpose. Silently dropping the tail would make a
    short answer look like the model's own, which is the "configured but inert"
    shape this repo keeps getting bitten by; the marker makes the loss legible
    in the run output.
    """
    if not text:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    keep = max(limit - len(_TRUNCATION_MARKER.encode("utf-8")), 0)
    return encoded[:keep].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER


def _loads(raw: str) -> Any:
    import json

    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
