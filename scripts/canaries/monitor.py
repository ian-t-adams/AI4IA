"""One governed chat request and, separately approved, one no-audio GA setup."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp

from app.api.src.ai4ia_api.usage.pricing import PricingBook, load_pricing
from scripts._canary_contract import (
    SetupOrderError, SetupState, acknowledge_setup, catalog_model_preferences,
    chat_payload, session_payload,
)

from .configuration import Configuration
from .contracts import (
    CanaryError, IDENTIFIER, MAX_OUTPUT_TOKENS, Report, digest,
    encoded, integer, obj, strict_json, timestamp, utc_now,
)
from .transport import Response, Transport

_SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class Budget:
    ends_at: float
    expires_at: datetime
    clock: Callable[[], float]
    wall_clock: Callable[[], datetime]

    @classmethod
    def start(cls, config: Configuration) -> Budget:
        return cls(time.monotonic() + 105, timestamp(config.expires_at), time.monotonic, utc_now)

    def timeout(self, ceiling: float) -> float:
        approval_remaining = (self.expires_at - self.wall_clock()).total_seconds()
        if approval_remaining <= 0:
            raise CanaryError("approval_expired")
        remaining = min(self.ends_at - self.clock(), approval_remaining)
        if remaining <= 0:
            raise CanaryError("deadline")
        return min(ceiling, remaining)


@dataclass(frozen=True)
class Selection:
    model: str
    api: str
    reasoning: str | None
    deployments: frozenset[str]


def select_chat(source: dict[str, Any], advertised: dict[str, Any], pricing: PricingBook) -> Selection:
    rows = advertised.get("models")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise CanaryError("invalid_response")
    by_id = {}
    for row in rows:
        key = obj(row).get("id")
        if not isinstance(key, str) or not IDENTIFIER.fullmatch(key) or key in by_id:
            raise CanaryError("invalid_response")
        by_id[key] = row
    candidates: list[tuple[int, str, Selection]] = []
    compatible = False
    source_catalog = source.get("catalog")
    if not isinstance(source_catalog, list) or not 1 <= len(source_catalog) <= 128:
        raise CanaryError("invalid_response")
    source_rows = {}
    for row in source_catalog:
        name = obj(row).get("name")
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or name in source_rows:
            raise CanaryError("invalid_response")
        source_rows[name] = row
    for name in catalog_model_preferences(source):
        row = by_id.get(name)
        if row is None or row.get("conversational") is not True:
            continue
        api = row.get("api")
        if api not in ("chat", "responses") or row.get("category") not in ("chat", "chat-fast"):
            continue
        expected = source_rows[name]
        if expected.get("category") != row.get("category") or expected.get("api", "chat") != api:
            continue
        efforts = row.get("reasoningEffortOptions")
        if not isinstance(efforts, list):
            continue
        if "none" not in efforts and not (efforts == [] and row.get("supportsSampling") is True):
            continue
        maximum = row.get("maxOutputTokens")
        if type(maximum) is not int or maximum < MAX_OUTPUT_TOKENS:
            continue
        options = row.get("options")
        if not isinstance(options, list) or not 1 <= len(options) <= 16:
            raise CanaryError("invalid_response")
        names: list[str] = []
        for option in options:
            item = obj(option).get("deploymentName")
            if not isinstance(item, str) or not IDENTIFIER.fullmatch(item):
                raise CanaryError("invalid_response")
            names.append(item)
        deployments = frozenset(names)
        compatible = True
        rate = pricing.rate(name)
        if rate is None or any(
            not math.isfinite(value) or value < 0
            for value in (rate.input_per_1m, rate.output_per_1m)
        ):
            continue
        estimate = pricing.estimate_token_bound(
            name, prompt_tokens=1024, completion_tokens=MAX_OUTPUT_TOKENS,
        )
        if not estimate.known or estimate.micro_usd is None:
            continue
        candidates.append((
            estimate.micro_usd, name,
            Selection(name, api, "none" if "none" in efforts else None, deployments),
        ))
    if not candidates:
        raise CanaryError("unpriced" if compatible else "no_compatible_model")
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def require_response(response: Response, expected: int = 200) -> dict[str, Any]:
    if response.status in (401, 403):
        raise CanaryError("auth_rejected")
    if response.status != expected:
        raise CanaryError("invalid_response")
    return response.object()


def capability(payload: dict[str, Any], selection: Selection) -> str:
    integer(payload.get("version"), 1, 1)
    if (
        payload.get("version") != 1 or payload.get("ready") is not True
        or payload.get("model") != selection.model or payload.get("api") != selection.api
        or payload.get("constraints") != {
            "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
            "maxOutputTokens": MAX_OUTPUT_TOKENS, "libraryDocumentIds": [],
        }
    ):
        raise CanaryError("posture_unavailable")
    region = payload.get("region")
    if not isinstance(region, str) or not IDENTIFIER.fullmatch(region):
        raise CanaryError("invalid_response")
    return region


def inspect_message(
    payload: dict[str, Any], session_id: str, selected: Selection, pricing: PricingBook,
    report: Report,
) -> dict[str, Any]:
    message = obj(payload.get("message"))
    if payload.get("sessionId") != session_id or message.get("sessionId") != session_id:
        raise CanaryError("invalid_response")
    if (
        message.get("role") != "assistant" or message.get("status") != "complete"
        or not isinstance(message.get("model"), str) or message["model"] not in selected.deployments
        or not isinstance(message.get("id"), str) or not _SESSION_ID.fullmatch(message["id"])
        or message.get("agent") is not None or message.get("attachments") != []
        or message.get("pendingApprovals") not in (None, [])
        or not isinstance(message.get("content"), str) or not 1 <= len(message["content"]) <= 2048
        or message["content"].strip().lower().rstrip(".") != "ready"
    ):
        raise CanaryError("model_failed")
    receipt = obj(message.get("executionReceipt"))
    integer(receipt.get("version"), 1, 1)
    for key in ("toolCallCount", "toolsOfferedCount"):
        integer(receipt.get(key), 0, 0)
    integer(receipt.get("iterations"), 1, 1)
    runtime = obj(receipt.get("runtime"))
    integer(runtime.get("modelCallCount"), 1, 1)
    if (
        receipt.get("version") != 1 or receipt.get("status") != "complete"
        or receipt.get("partial") is not False or receipt.get("truncated") is not False
        or receipt.get("toolCallCount") != 0 or receipt.get("toolsOfferedCount") != 0
        or receipt.get("toolCalls") != [] or receipt.get("toolsOffered") != []
        or receipt.get("delegations") != [] or receipt.get("iterations") != 1
        or runtime.get("modelId") != selected.model or runtime.get("api") != selected.api
        or runtime.get("modelCallCount") != 1
    ):
        raise CanaryError("receipt_incomplete")
    calls = runtime.get("modelCalls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise CanaryError("receipt_incomplete")
    call = obj(calls[0])
    parameters = obj(call.get("parameters"))
    integer(parameters.get("maxOutputTokens"), MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)
    if (
        call.get("modelId") != selected.model or call.get("api") != selected.api
        or call.get("coverage") != "recorded" or call.get("providerCompleted") is not True
        or parameters.get("maxOutputTokens") != MAX_OUTPUT_TOKENS
        or call.get("usageKnown") is not True or call.get("usageComplete") is not True
    ):
        raise CanaryError("receipt_incomplete")
    integer(call.get("httpAttempts"), 1, 8)
    prompt_tokens = integer(call.get("promptTokens"), 1, 1024)
    completion_tokens = integer(call.get("completionTokens"), 1, MAX_OUTPUT_TOKENS)
    # Reuse the shared calculator with this invocation's pre-dispatch snapshot.
    # This is reported usage, not a provider retry or all-service bill bound.
    cost = pricing.estimate(
        selected.model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
    )
    if not cost.known or cost.micro_usd is None or cost.version is None:
        raise CanaryError("receipt_incomplete")
    recorded_cost = obj(call.get("cost"))
    if (
        recorded_cost.get("coverage") != "known" or recorded_cost.get("currency") != "USD"
        or type(recorded_cost.get("priceInputPer1M")) not in (int, float)
        or type(recorded_cost.get("priceOutputPer1M")) not in (int, float)
        or recorded_cost.get("priceVersion") != cost.version
        or recorded_cost.get("priceInputPer1M") != cost.input_per_1m
        or recorded_cost.get("priceOutputPer1M") != cost.output_per_1m
        or integer(recorded_cost.get("estCostMicroUsd"), 0, 2**53 - 1) != cost.micro_usd
    ):
        raise CanaryError("receipt_incomplete")
    report.usage_known = True
    report.estimated_micro_usd = recorded_cost["estCostMicroUsd"]
    report.price_version = recorded_cost["priceVersion"]
    return message


def _verified_cleanup(status: dict[str, Any], session_id: str) -> bool:
    required = {
        "sessionId", "state", "phase", "requestedAt", "updatedAt", "lastVerifiedAt",
        "messagesVerified", "documentsVerified", "attachmentsVerified", "pendingUploads",
        "pendingUploadsTruncated", "retryReason", "attempts", "scope", "backupsErased",
        "coordinationRetained", "autonomousCleanup",
    }
    if set(status) != required:
        return False
    if (
        status["sessionId"] != session_id or status["state"] != "cleanup_verified"
        or status["phase"] != "complete" or status["pendingUploads"] != []
        or status["pendingUploadsTruncated"] is not False or status["retryReason"] is not None
        or status["scope"] != "conversation_content_and_inline_originals"
        or status["backupsErased"] is not False or status["coordinationRetained"] is not True
        or status["autonomousCleanup"] is not False
        or any(status[key] is not True for key in (
            "messagesVerified", "documentsVerified", "attachmentsVerified",
        ))
    ):
        return False
    integer(status["attempts"], 1, 1000)
    dates = []
    for key in ("requestedAt", "updatedAt", "lastVerifiedAt"):
        raw = status[key]
        if not isinstance(raw, str) or len(raw) > 40:
            return False
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if value.utcoffset() != timezone.utc.utcoffset(value) or value > utc_now():
            return False
        dates.append(value)
    return dates[0] <= dates[2] <= dates[1]


async def _cleanup(
    transport: Transport, config: Configuration, token: str,
    session_id: str, report: Report, *, chat_settled: bool, budget: Budget,
) -> None:
    if not chat_settled:
        report.mark("cleanup", "unknown", "cleanup_pending")
        return
    started = time.monotonic()
    calls = 0

    async def send(method: str, suffix: str, ceiling: float) -> Response:
        nonlocal calls
        timeout = budget.timeout(ceiling)
        calls += 1
        return await transport.request(
            method, f"{config.web_origin}/api/sessions/{session_id}{suffix}",
            token=token, timeout=timeout, limit=8192,
        )

    try:
        deleted = await send("DELETE", "", 8)
        if deleted.status == 204 and deleted.body == b"":
            readback = await send("GET", "", 5)
            # A completed, exclusively owned, empty-attachment turn followed by
            # the ordinary cascade and an ownership-scoped not-found is a
            # logical deletion observation, not physical v1 cleanup proof.
            if readback.status == 404 and isinstance(readback.json(), dict):
                report.mark("cleanup", "partial", "logical_deleted", elapsed=time.monotonic() - started, attempts=calls)
                return
        elif deleted.status in (200, 202):
            status = deleted.object()
            for _ in range(2):
                if _verified_cleanup(status, session_id):
                    break
                if status.get("sessionId") != session_id or status.get("state") not in ("pending", "retryable"):
                    raise CanaryError("invalid_response")
                # Only the just-created, exact-owner v1 intent may be resumed.
                # Two bounded passes suffice for this empty-attachment two-row
                # fixture; anything still pending stops the next observation.
                resumed = await send("POST", "/deletion/reconcile", 22)
                if resumed.status not in (200, 202):
                    raise CanaryError("cleanup_failed")
                status = resumed.object()
            if _verified_cleanup(status, session_id):
                readback = await send("GET", "/deletion", 5)
                if (
                    readback.status == 200 and readback.object() == status
                    and _verified_cleanup(readback.object(), session_id)
                ):
                    report.cleanup_safe = True
                    report.mark("cleanup", "pass", "cleanup_verified", elapsed=time.monotonic() - started, attempts=calls)
                    return
            report.mark("cleanup", "partial", "cleanup_pending", elapsed=time.monotonic() - started, attempts=calls)
            return
        report.mark("cleanup", "fail", "cleanup_failed", elapsed=time.monotonic() - started, attempts=calls)
    except CanaryError as exc:
        report.mark("cleanup", "unknown", exc.code, elapsed=time.monotonic() - started, attempts=calls)


async def chat(
    transport: Transport, config: Configuration, token: str,
    source: dict[str, Any], report: Report, *, pricing: PricingBook | None = None,
    budget: Budget | None = None,
) -> dict[str, Any] | None:
    stage = "platform"
    started = time.monotonic()
    session_id: str | None = None
    chat_settled = False
    advertised: dict[str, Any] | None = None
    budget = budget or Budget.start(config)
    try:
        response = await transport.request(
            "GET", f"{config.web_origin}/api/models", token=token, timeout=budget.timeout(15),
        )
        if response.status in (401, 403):
            report.mark("platform", "pass", elapsed=response.elapsed, attempts=1)
            stage = "auth"
        advertised = require_response(response)
        report.mark("platform", "pass", elapsed=response.elapsed, attempts=1)
        report.mark("auth", "pass", elapsed=response.elapsed, attempts=1)
        stage = "catalog"
        book = pricing or load_pricing()
        selected = select_chat(source, advertised, book)
        book = book.snapshot_token_prices(selected.model)
        report.catalog_version = digest(source)
        report.protocol = selected.api
        report.mark("catalog", "pass")
        stage = "posture"
        preflight = await transport.request(
            "GET", f"{config.web_origin}/api/canary/capabilities?{urlencode({'model': selected.model})}",
            token=token, limit=8192, timeout=budget.timeout(15),
        )
        region = capability(require_response(preflight), selected)
        report.mark("posture", "pass", elapsed=preflight.elapsed, attempts=1)
        stage = "session"
        create_timeout = budget.timeout(15)
        report.cleanup_safe = False
        created = await transport.request(
            "POST", f"{config.web_origin}/api/sessions", token=token,
            body=encoded({
                **session_payload(selected.model), "libraryDocumentIds": [],
                "agentName": None, "toolOverrides": {"added": [], "removed": []},
            }),
            timeout=create_timeout,
        )
        if created.status in (400, 401, 403, 422):
            report.cleanup_safe = True
            raise CanaryError("session_rejected")
        value = require_response(created, 201)
        identifier = value.get("id")
        if not isinstance(identifier, str) or not _SESSION_ID.fullmatch(identifier):
            raise CanaryError("create_unknown")
        session_id = identifier
        chat_settled = True
        if (
            value.get("model") != selected.model or value.get("agentName") is not None
            or value.get("libraryDocumentIds") != []
            or value.get("toolOverrides") != {"added": [], "removed": []}
        ):
            raise CanaryError("invalid_response")
        report.mark("session", "pass", elapsed=created.elapsed, attempts=1)
        stage = "gateway"
        params: dict[str, Any] = {"max_tokens": MAX_OUTPUT_TOKENS}
        if selected.reasoning is not None:
            params["reasoning_effort"] = selected.reasoning
        chat_timeout = budget.timeout(55)
        report.chat_attempts = 1
        chat_settled = False
        answer = await transport.request(
            "POST", f"{config.web_origin}/api/chat", token=token,
            body=encoded({
                **chat_payload(session_id, selected.model), "region": region, "params": params,
                "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
            }),
            timeout=chat_timeout,
        )
        if answer.status != 200:
            if answer.status in (401, 403, 409, 422):
                chat_settled = True
            raise CanaryError("auth_rejected" if answer.status in (401, 403) else "dispatch_unknown")
        payload = answer.object()
        message = obj(payload.get("message"))
        if message.get("status") in ("complete", "error", "cancelled"):
            chat_settled = True
        stage = "model"
        message = inspect_message(payload, session_id, selected, book, report)
        report.mark("gateway", "pass", elapsed=answer.elapsed, attempts=1)
        report.mark("model", "pass", elapsed=answer.elapsed, attempts=1)
        stage = "persistence"
        stored = await transport.request(
            "GET", f"{config.web_origin}/api/sessions/{session_id}/messages",
            token=token, timeout=budget.timeout(15),
        )
        if stored.status != 200:
            raise CanaryError("persistence_failed")
        rows = stored.json()
        if (
            not isinstance(rows, list) or len(rows) != 2
            or sum(row == message for row in rows) != 1
            or sum(obj(row).get("role") == "user" for row in rows) != 1
        ):
            raise CanaryError("persistence_failed")
        report.mark("persistence", "pass", elapsed=stored.elapsed, attempts=1)
    except CanaryError as exc:
        definitive = exc.code in {
            "auth_rejected", "session_rejected", "model_failed", "persistence_failed",
            "network_unavailable", "deadline", "redirect_rejected", "invalid_response",
            "dispatch_unknown",
        }
        report.mark(
            stage, "fail" if definitive else "unknown", exc.code,
            elapsed=time.monotonic() - started, attempts=1,
        )
    except asyncio.CancelledError:
        report.mark(stage, "unknown", "cancelled", elapsed=time.monotonic() - started, attempts=1)
        raise
    finally:
        if session_id is not None:
            await _cleanup(
                transport, config, token, session_id, report, chat_settled=chat_settled, budget=budget,
            )
        elif not report.cleanup_safe:
            report.mark("cleanup", "unknown", "create_unknown")
    return advertised


async def realtime(
    transport: Transport, config: Configuration, token: str,
    source: dict[str, Any], advertised: dict[str, Any] | None, report: Report,
    *, budget: Budget | None = None,
) -> None:
    if not config.ga_enabled:
        report.mark("realtime", "not_run", "disabled")
        return
    if (
        advertised is None or not report.cleanup_safe
        or report.stages["posture"].outcome != "pass"
    ):
        report.mark("realtime", "not_run", "prior_stage")
        return
    started = time.monotonic()
    budget = budget or Budget.start(config)
    try:
        response = await transport.request(
            "GET", f"{config.api_origin}/api/voice/live/config", timeout=budget.timeout(15),
        )
        runtime = require_response(response)
        providers = runtime.get("enabledProviderIds")
        if (
            runtime.get("openaiRealtimeProtocol") != "ga"
            or not isinstance(providers, list) or "azure_openai" not in providers
        ):
            report.mark("realtime", "not_run", "ga_not_selected", elapsed=response.elapsed, attempts=1)
            return
        source_ids = {
            row.get("name") for row in source.get("catalog", [])
            if obj(row).get("category") == "realtime"
        }
        models = sorted(
            row["id"] for row in advertised.get("models", [])
            if obj(row).get("category") == "realtime" and row.get("id") in source_ids
        )
        if not models:
            raise CanaryError("ga_unavailable")
        model = models[0]
        query = urlencode({"provider": "azure_openai", "model": model})
        url = f"{config.api_origin.replace('https://', 'wss://', 1)}/api/voice/live?{query}"
        client = transport.client(url, websocket=True)
        state = SetupState()
        total_bytes = 0
        report.realtime_attempts = 1
        async with asyncio.timeout(budget.timeout(15)):
            async with client.ws_connect(
                url, protocols=("ai4ia-bearer", token), origin=config.web_origin,
                autoping=True, max_msg_size=16 * 1024,
            ) as ws:
                if ws.protocol != "ai4ia-bearer" or transport.handshake_protocol != "ga":
                    raise CanaryError("protocol_mismatch")
                await ws.send_str(encoded({
                    "type": "session.update",
                    "session": {"turn_detection": None, "modalities": ["text"], "max_response_output_tokens": 64},
                }).decode("ascii"))
                async for event in ws:
                    state.received_frames += 1
                    if state.received_frames > 32:
                        raise CanaryError("event_limit")
                    if event.type != aiohttp.WSMsgType.TEXT:
                        raise CanaryError("closed")
                    raw = event.data.encode("utf-8")
                    total_bytes += len(raw)
                    if total_bytes > 64 * 1024:
                        raise CanaryError("event_limit")
                    payload = obj(strict_json(raw, limit=16 * 1024))
                    if payload.get("type") == "error":
                        raise CanaryError("protocol_error")
                    if payload.get("type") not in ("session.created", "session.updated"):
                        raise CanaryError("protocol_error")
                    if payload.get("type") == "session.created" and state.created:
                        raise CanaryError("event_order")
                    if acknowledge_setup(payload, state):
                        await ws.close(code=1000, message=b"canary-complete")
                        report.mark("realtime", "pass", elapsed=time.monotonic() - started, attempts=1)
                        return
                raise CanaryError("closed")
    except (SetupOrderError, CanaryError) as exc:
        report.mark(
            "realtime", "fail",
            exc.code if isinstance(exc, CanaryError) else "event_order",
            elapsed=time.monotonic() - started, attempts=report.realtime_attempts,
        )
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        report.mark(
            "realtime", "fail", "deadline" if isinstance(exc, TimeoutError) else "network_unavailable",
            elapsed=time.monotonic() - started, attempts=report.realtime_attempts,
        )
