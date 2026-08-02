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
import logging
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..sessions.models import Message, MessageRole, MessageStatus
from ..usage.models import TokenUsage
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

    async def schedule(self, payload: dict[str, Any], *, user_id: str) -> str:
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
        instance_id = f"{user_id}{_RUN_ID_SEPARATOR}{uuid4().hex}"
        return await self._client.schedule_new_orchestration(
            ORCHESTRATOR_NAME,
            input=payload,
            instance_id=instance_id,
        )

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

        result = DurableRunStatus(runId=run_id, status=status)
        if failure is not None:
            result.error = getattr(failure, "message", str(failure))
        parsed = state.get_output()
        if isinstance(parsed, dict):
            result.ok = parsed.get("ok")
            result.text = parsed.get("text")
        return result

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
            context = payload["context"]
            step = _step_from_dict(payload["step"])
            return service._run_on_app_loop(
                service._execute_step(
                    step=step,
                    index=payload["index"],
                    previous=payload.get("previous") or "",
                    context=context,
                )
            )

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
            sessionId=session_id,
            userId=uid,
            role=MessageRole.assistant,
            content=payload.get("text") or "",
            status=MessageStatus.complete,
            model=context["deployment"],
            agent=agent_attr,
        )
        await state.session_repo.add_message(uid, assistant)

        usage = _usage_from_dict(payload.get("usage") or {})
        # Same rule as the in-request path: meter only when a model call actually
        # happened, so a zero-work failure neither consumes a request slot nor
        # pollutes the ledger.
        if usage.calls > 0:
            deployment = state.catalog.resolve_deployment(context["modelId"])
            if deployment is not None:
                await state.usage.record_completion(
                    user_id=uid,
                    session_id=session_id,
                    model_id=context["modelId"],
                    deployment=deployment,
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
    deployment: str,
    correlation_id: str | None,
) -> dict[str, Any]:
    """Freeze everything a run needs into its orchestration input.

    A durable orchestration replays from history, so it must not re-resolve the
    workflow, the model, or the agent catalog later — a definition edited
    mid-run would otherwise change the meaning of an in-flight run.
    """
    return {
        "steps": [
            {"agent": s.agent, "instruction": s.instruction} for s in workflow.steps
        ],
        "context": {
            "userId": user_id,
            "sessionId": session_id,
            "workflowName": workflow.name,
            "runInput": run_input,
            "modelId": model_id,
            "deployment": deployment,
            "correlationId": correlation_id,
        },
    }


def _step_from_dict(raw: dict[str, Any]):
    from .models import WorkflowStep

    return WorkflowStep(agent=raw["agent"], instruction=raw["instruction"])


def _usage_to_dict(usage: TokenUsage) -> dict[str, Any]:
    return {
        "prompt": usage.prompt,
        "completion": usage.completion,
        "total": usage.total,
        "known": usage.known,
        "complete": usage.complete,
        "calls": usage.calls,
    }


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
