"""Operational canaries under HTTP/WS/clock/Actions fakes. Never invoke a live model."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import aiohttp
import yaml

from app.api.src.ai4ia_api.usage.pricing import PriceRate, PricingBook
from scripts.canaries import __main__ as cli
from scripts.canaries.configuration import Configuration, RealtimeActor, current_run
from scripts.canaries.contracts import (
    CanaryError, INTERVAL_SECONDS, MAX_HTTP_BYTES, MAX_OUTPUT_TOKENS,
    Report, Run, STAGES, encoded, public_origin, stamp, strict_json,
)
from scripts.canaries.github import artifact_name, locate
from scripts.canaries.identity import acquire, validate_api_token
from scripts.canaries.monitor import Budget, chat, realtime
from scripts.canaries.state import Counter, State, admit, finish
from scripts.canaries.transport import PublicResolver, Response, Transport
from scripts.tests import test_gating_workflows as gating

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "application-canaries.yml"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
RUN = Run("owner/repository", 700, 200, 2, 1, "a" * 40)
CONFIG = Configuration(
    tenant_id="11111111-1111-1111-1111-111111111111",
    client_id="22222222-2222-2222-2222-222222222222",
    object_id="33333333-3333-3333-3333-333333333333",
    audience="api://44444444-4444-4444-4444-444444444444",
    web_origin="https://web.example.test",
    api_origin="https://api.example.test",
    approval_id="55555555-5555-5555-5555-555555555555",
    expires_at=stamp(NOW + timedelta(days=1)),
    approved_runs=4,
    interval_seconds=INTERVAL_SECONDS,
    acknowledge_no_hard_bill_cap=True,
    actor_ready=True,
    cleanup_approved=True,
    ga_enabled=False,
)
SOURCE = {
    "catalog": [
        {"name": "test-chat", "category": "chat-fast", "api": "chat",
         "deployments": [{"version": "test-version", "region": "eastus2"}]},
        {"name": "test-realtime", "category": "realtime", "deployments": [{"version": "test-version"}]},
    ],
}
ADVERTISED = {"models": [
    {
        "id": "test-chat", "category": "chat-fast", "api": "chat", "conversational": True,
        "supportsSampling": True, "reasoningEffortOptions": [], "maxOutputTokens": 1024,
        "options": [{"deploymentName": "test-deployment", "region": "eastus2"}],
    },
    {"id": "test-realtime", "category": "realtime", "options": [
        {"deploymentName": "test-realtime-deployment", "region": "eastus2"},
    ]},
]}
BOOK = PricingBook(
    {"test-chat": PriceRate(1, 2)}, currency="USD", version="synthetic-v1",
)
SID = "c" * 32


def environment(run=RUN, config=CONFIG):
    return {
        "GITHUB_REPOSITORY": run.repository,
        "GITHUB_REPOSITORY_ID": str(run.repository_id),
        "GITHUB_RUN_ID": str(run.run_id), "GITHUB_RUN_NUMBER": str(run.number),
        "GITHUB_RUN_ATTEMPT": str(run.attempt), "GITHUB_SHA": run.sha,
        "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": f"{run.repository}/.github/workflows/application-canaries.yml@refs/heads/main",
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_API_URL": "https://api.github.com",
        "AI4IA_CANARY_ENABLED": "true", "AI4IA_CANARY_CONFIG": json.dumps(asdict(config)),
        "DEPLOY_CLIENT_ID": "66666666-6666-6666-6666-666666666666",
        "CANARY_OPERATION": "observe",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipelines.actions.githubusercontent.com/token?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-runner-token",
    }


def response(value, status=200, content_type="application/json"):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return Response(status, body, 0.01, content_type)


def message():
    return {
        "id": "d" * 32, "sessionId": SID, "userId": "never-export-this-owner",
        "role": "assistant", "status": "complete", "model": "test-deployment",
        "content": "ready", "agent": None, "attachments": [], "pendingApprovals": None,
        "executionReceipt": {
            "version": 1, "status": "complete", "partial": False, "truncated": False,
            "toolCallCount": 0, "toolsOfferedCount": 0, "toolCalls": [], "toolsOffered": [],
            "delegations": [], "iterations": 1,
            "prompt": [{"content": {"text": "NEVER EXPORT THE PROMPT"}}],
            "runtime": {
                "modelId": "test-chat", "api": "chat", "modelCallCount": 1,
                "modelCalls": [{
                    "modelId": "test-chat", "api": "chat", "coverage": "recorded",
                    "providerCompleted": True, "parameters": {"maxOutputTokens": 64},
                    "httpAttempts": 1, "usageKnown": True, "usageComplete": True,
                    "promptTokens": 12, "completionTokens": 1,
                    "cost": {
                        "coverage": "known", "currency": "USD", "priceVersion": "synthetic-v1",
                        "priceInputPer1M": 1, "priceOutputPer1M": 2, "estCostMicroUsd": 14,
                    },
                }],
            },
        },
    }


def ready_capability():
    return {
        "version": 1, "ready": True, "model": "test-chat", "api": "chat", "region": "eastus2",
        "constraints": {
            "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
            "maxOutputTokens": 64, "libraryDocumentIds": [],
        },
    }


def deletion_status(*, complete=True):
    return {
        "sessionId": SID, "state": "cleanup_verified" if complete else "pending",
        "phase": "complete" if complete else "messages",
        "requestedAt": stamp(NOW), "updatedAt": stamp(NOW),
        "lastVerifiedAt": stamp(NOW) if complete else None,
        "messagesVerified": complete, "documentsVerified": complete, "attachmentsVerified": complete,
        "pendingUploads": [], "pendingUploadsTruncated": False, "retryReason": None,
        "attempts": 1, "scope": "conversation_content_and_inline_originals",
        "backupsErased": False, "coordinationRetained": True, "autonomousCleanup": False,
    }


def realtime_capability():
    return {
        "version": 1, "ready": True, "model": "test-realtime", "region": "eastus2",
        "constraints": {
            "provider": "azure_openai", "protocol": "ga", "setupOnly": True,
            "allowAudio": False, "allowResponses": False, "allowTools": False, "maxSeconds": 15,
        },
    }


def previous_state(*, report=None, config=CONFIG):
    if report is None:
        report = Report(replace(RUN, run_id=100, number=1), stamp(NOW - timedelta(hours=6)))
        report.unobserved("bootstrap")
    return finish(report, config, None, control="bootstrap")


class FakeApp:
    def __init__(self):
        self.calls = []
        self.message = message()
        self.overrides = {}
        self.handshake_protocol = "ga"
        self.websocket = FakeWebSocket()
        self.sockets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        key = (method, urlsplit(url).path)
        if key in self.overrides:
            value = self.overrides[key]
            if isinstance(value, BaseException):
                raise value
            return value
        if key == ("GET", "/api/models"):
            return response(copy.deepcopy(ADVERTISED))
        if key == ("GET", "/api/canary/capabilities"):
            return response(ready_capability())
        if key == ("GET", "/api/canary/realtime-capabilities"):
            return response(realtime_capability())
        if key == ("POST", "/api/sessions"):
            return response({
                "id": SID, "model": "test-chat", "agentName": None, "libraryDocumentIds": [],
                "toolOverrides": {"added": [], "removed": []},
            }, 201)
        if key == ("POST", "/api/chat"):
            return response({"sessionId": SID, "message": self.message})
        if key == ("GET", f"/api/sessions/{SID}/messages"):
            return response([{"role": "user"}, self.message])
        if key == ("DELETE", f"/api/sessions/{SID}"):
            return response(deletion_status(complete=False), 202)
        if key == ("POST", f"/api/sessions/{SID}/deletion/reconcile"):
            return response(deletion_status(), 200)
        if key == ("GET", f"/api/sessions/{SID}/deletion"):
            return response(deletion_status(), 200)
        if key == ("GET", f"/api/sessions/{SID}"):
            return response({"detail": "Session not found"}, 404)
        if key == ("GET", "/api/voice/live/config"):
            return response({"openaiRealtimeProtocol": "ga", "enabledProviderIds": ["azure_openai"]})
        raise AssertionError(f"Unexpected method/path: {method} {urlsplit(url).path}")

    def client(self, url, *, websocket=False):
        assert websocket
        return self

    def ws_connect(self, url, **kwargs):
        self.sockets.append((url, kwargs))
        return self.websocket


class FakeWebSocket:
    protocol = "ai4ia-bearer"

    def __init__(self):
        self.sent = []
        self.closed = False
        self.events = [{"type": "session.created"}, {"type": "session.updated"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def send_str(self, value):
        self.sent.append(json.loads(value))

    async def close(self, **_):
        self.closed = True

    def __aiter__(self):
        async def iterate():
            for value in self.events:
                yield SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    data=value if isinstance(value, str) else json.dumps(value),
                )
        return iterate()


class StrictContractTests(unittest.TestCase):
    def test_configuration_requires_exact_scope_and_finite_lease(self):
        self.assertEqual(Configuration.load(environment(), NOW), CONFIG)
        changes = [
            {"client_id": environment()["DEPLOY_CLIENT_ID"]},
            {"object_id": CONFIG.client_id},
            {"actor_ready": False}, {"cleanup_approved": False},
            {"acknowledge_no_hard_bill_cap": False},
            {"approved_runs": 29}, {"approved_runs": True},
            {"interval_seconds": 3600}, {"ga_enabled": "true"},
            {"expires_at": stamp(NOW)}, {"expires_at": stamp(NOW + timedelta(days=8))},
            {"audience": "https://graph.microsoft.com"}, {"web_origin": "http://web.example.test"},
        ]
        for change in changes:
            with self.subTest(change=change):
                env = environment()
                env["AI4IA_CANARY_CONFIG"] = json.dumps({**asdict(CONFIG), **change})
                with self.assertRaises(CanaryError):
                    Configuration.load(env, NOW)

    def test_default_off_never_needs_identity_config(self):
        for enabled in ("", "false"):
            self.assertIsNone(Configuration.load({"AI4IA_CANARY_ENABLED": enabled}, NOW))
        env = environment()
        env["AI4IA_CANARY_HARD_USD_CAP"] = "1.00"
        with self.assertRaisesRegex(CanaryError, "hard_bill_cap_unsupported"):
            Configuration.load(env, NOW)

    def test_ga_never_reuses_monitor_or_deployment_identity(self):
        distinct = RealtimeActor(
            "88888888-8888-8888-8888-888888888888",
            "99999999-9999-9999-9999-999999999999",
        )
        configured = replace(CONFIG, ga_enabled=True, realtime_actor=distinct)
        parsed = Configuration.load(environment(config=configured), NOW)
        self.assertEqual(parsed.for_realtime().client_id, distinct.client_id)
        self.assertEqual(parsed.for_realtime().object_id, distinct.object_id)
        for actor in (
            None, RealtimeActor(CONFIG.client_id, distinct.object_id),
            RealtimeActor(distinct.client_id, CONFIG.object_id),
            RealtimeActor(environment()["DEPLOY_CLIENT_ID"], distinct.object_id),
        ):
            with self.subTest(actor=actor), self.assertRaises(CanaryError):
                Configuration.load(environment(config=replace(CONFIG, ga_enabled=True, realtime_actor=actor)), NOW)

    def test_main_first_attempt_and_exact_workflow_are_required(self):
        self.assertEqual(current_run(environment()), RUN)
        for key, value in [
            ("GITHUB_REF", "refs/tags/main"), ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_WORKFLOW_REF", "owner/repository/other.yml@refs/heads/main"),
            ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_API_URL", "https://attacker.test"),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(CanaryError):
                current_run({**environment(), key: value})

    def test_json_rejects_duplicates_nonfinite_depth_bytes_and_bad_utf8(self):
        self.assertEqual(strict_json(b'{"a":[1,2]}'), {"a": [1, 2]})
        for raw in (
            b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}',
            b'{"a":"\xff"}', b"[" * 18 + b"0" + b"]" * 18, b" " * 32769,
        ):
            with self.subTest(raw=raw[:30]), self.assertRaises(CanaryError):
                strict_json(raw)

    def test_report_cannot_copy_arbitrary_fields_or_fake_complete_coverage(self):
        report = Report(RUN, stamp(NOW))
        report.unobserved("disabled")
        self.assertEqual(set(Report.parse(report.document()).stages), set(STAGES))
        for change in ({"reply": "private"}, {"coverage": "complete"}, {"usage_known": True}):
            with self.subTest(change=change), self.assertRaises(CanaryError):
                Report.parse({**report.document(), **change})

    def test_only_public_dns_https_origins_are_admitted(self):
        self.assertEqual(public_origin("https://web.example.test"), CONFIG.web_origin)
        for value in (
            "https://localhost", "https://127.0.0.1", "https://[::1]",
            "https://a.local", "https://u:p@example.test", "https://a.test/path",
            "https://a.test?x=1", "https://a.test#x", "https://a.test:444",
            "https://a.test\\@evil.test", "https://a.test\n",
        ):
            with self.subTest(value=value), self.assertRaises(CanaryError):
                public_origin(value)


class StateTests(unittest.TestCase):
    def test_consecutive_failure_transition_and_recovery_controls(self):
        count = Counter()
        count = count.advance("fail")
        self.assertEqual(count, Counter(1, False, "none"))
        count = count.advance("fail")
        self.assertFalse(count.alerting)
        count = count.advance("fail")
        self.assertEqual(count, Counter(3, True, "firing"))
        self.assertEqual(count.advance("fail"), Counter(3, True, "none"))
        unknown = count.advance("unknown")
        self.assertEqual(unknown, Counter(None, True, "none"))
        recovered = unknown.advance("pass")
        self.assertEqual(recovered, Counter(0, False, "recovered"))
        self.assertEqual(recovered.advance("pass"), Counter(0, False, "none"))
        self.assertEqual(Counter(2).advance("unknown").advance("fail").failures, 1)

    def test_missing_state_is_not_bootstrapped_or_counted_as_zero(self):
        with self.assertRaisesRegex(CanaryError, "state_missing"):
            admit(CONFIG, RUN, None, NOW, bootstrap=False)
        with self.assertRaisesRegex(CanaryError, "state_missing"):
            admit(CONFIG, RUN, None, NOW, bootstrap=True)
        admit(CONFIG, replace(RUN, number=1), None, NOW, bootstrap=True)
        self.assertIsNone(previous_state().chat.failures)

    def test_predecessor_duplicate_loop_attempt_staleness_and_scope_are_rejected(self):
        before = previous_state()
        admit(CONFIG, RUN, before, NOW, bootstrap=False)
        mutants = [
            replace(before, report=replace(before.report, run=RUN)),
            replace(before, report=replace(before.report, run=replace(before.report.run, number=0))),
            replace(before, report=replace(before.report, observed_at=stamp(NOW - timedelta(hours=19)))),
            replace(before, blocked=True),
            replace(before, scope_digest="f" * 64),
            replace(before, previous_run_id=before.report.run.run_id),
        ]
        for mutant in mutants:
            with self.subTest(mutant=mutant), self.assertRaises(CanaryError):
                admit(CONFIG, RUN, mutant, NOW, bootstrap=False)

    def test_cadence_and_run_count_cannot_be_reset_with_the_same_approval(self):
        before = replace(previous_state(), observations=1, last_attempt_at=stamp(NOW - timedelta(minutes=1)))
        before.report.observed_at = stamp(NOW - timedelta(minutes=1))
        with self.assertRaisesRegex(CanaryError, "cadence"):
            admit(CONFIG, RUN, before, NOW, bootstrap=False)
        with self.assertRaisesRegex(CanaryError, "state_invalid"):
            admit(CONFIG, RUN, before, NOW, bootstrap=True)
        changed_budget = replace(CONFIG, approved_runs=8)
        with self.assertRaises(CanaryError):
            admit(changed_budget, RUN, before, NOW, bootstrap=True)
        new_approval = replace(CONFIG, approval_id="77777777-7777-7777-7777-777777777777")
        admit(new_approval, RUN, before, NOW, bootstrap=True)
        report = Report(RUN, stamp(NOW))
        report.unobserved("bootstrap")
        renewed = finish(report, new_approval, before, control="bootstrap")
        self.assertEqual(renewed.last_attempt_at, before.last_attempt_at)
        with self.assertRaisesRegex(CanaryError, "cadence"):
            admit(new_approval, replace(RUN, number=3, run_id=300), renewed, NOW, bootstrap=False)
        exhausted = replace(
            previous_state(), observations=CONFIG.approved_runs,
            last_attempt_at=stamp(NOW - timedelta(hours=6)),
        )
        with self.assertRaisesRegex(CanaryError, "lease_exhausted"):
            admit(CONFIG, RUN, exhausted, NOW, bootstrap=False)

    def test_control_cannot_launder_an_unresolved_write(self):
        before = replace(previous_state(), blocked=True)
        before.report.cleanup_safe = False
        report = Report(RUN, stamp(NOW))
        report.unobserved("disabled")
        after = finish(report, None, before, control="disabled")
        self.assertTrue(after.blocked)
        with self.assertRaisesRegex(CanaryError, "state_blocked"):
            admit(CONFIG, replace(RUN, number=3, run_id=300), after, NOW, bootstrap=True)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = patch("scripts.canaries.monitor.utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    async def test_valid_chat_uses_real_request_shapes_once_and_owned_cleanup(self):
        fake = FakeApp()
        report = Report(RUN, stamp(NOW))
        await chat(fake, CONFIG, "private-token", SOURCE, report, pricing=BOOK)
        self.assertTrue(all(report.stages[key].outcome == "pass" for key in (
            "platform", "auth", "catalog", "posture", "session", "gateway", "model", "persistence",
        )), report.document())
        self.assertEqual(report.chat_attempts, 1)
        self.assertTrue(report.cleanup_safe)
        self.assertEqual(report.stages["cleanup"].outcome, "pass")
        self.assertEqual(report.stages["cleanup"].code, "cleanup_verified")
        creates = [call for call in fake.calls if call[0] == "POST" and call[1].endswith("/sessions")]
        turns = [call for call in fake.calls if call[0] == "POST" and call[1].endswith("/chat")]
        deletes = [call for call in fake.calls if call[0] == "DELETE"]
        self.assertEqual((len(creates), len(turns), len(deletes)), (1, 1, 1))
        body = json.loads(turns[0][2]["body"])
        self.assertFalse(body["allowTools"])
        self.assertFalse(body["allowAutomaticMemory"])
        self.assertTrue(body["requireFreshSession"])
        self.assertEqual(body["params"]["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(urlsplit(deletes[0][1]).path, f"/api/sessions/{SID}")
        serialized = encoded(report.document()).decode()
        for forbidden in (
            "private-token", SID, "never-export-this-owner", "NEVER EXPORT THE PROMPT",
            "Reply with", "test-chat", "test-deployment", CONFIG.web_origin,
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_auth_catalog_posture_failures_have_full_unscored_denominator(self):
        for method, path, failure in [
            ("GET", "/api/models", response({"detail": "do not log credentials"}, 401)),
            ("GET", "/api/models", response(b'{"models":[],"models":[]}')),
            ("GET", "/api/models", response({"models": []})),
            ("GET", "/api/canary/capabilities", response({**ready_capability(), "ready": False})),
            ("GET", "/api/canary/capabilities", response(ready_capability(), 302)),
        ]:
            with self.subTest(path=path, failure=failure):
                fake = FakeApp()
                fake.overrides[method, path] = failure
                report = Report(RUN, stamp(NOW))
                await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK)
                self.assertFalse(any(call[0] != "GET" for call in fake.calls))
                self.assertEqual(set(report.stages), set(STAGES))
                self.assertEqual(report.stages["model"].outcome, "not_run")

    async def test_unpriced_path_refuses_before_creating_session(self):
        for priced in (False, True):
            fake = FakeApp()
            report = Report(RUN, stamp(NOW))
            book = BOOK if priced else PricingBook({}, currency="USD", version="synthetic-v1")
            await chat(fake, CONFIG, "secret", SOURCE, report, pricing=book)
            self.assertEqual(any(call[0] == "POST" for call in fake.calls), priced)
            if not priced:
                self.assertEqual(report.stages["catalog"].code, "unpriced")

    async def test_ambiguous_create_and_chat_are_never_replayed_or_unsafely_deleted(self):
        for path in ("/api/sessions", "/api/chat"):
            for failure in (CanaryError("deadline"), response({"detail": "unknown"}, 503)):
                with self.subTest(path=path, failure=failure):
                    fake = FakeApp()
                    fake.overrides["POST", path] = failure
                    report = Report(RUN, stamp(NOW))
                    await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK)
                    self.assertEqual(sum(call[0] == "POST" and urlsplit(call[1]).path == path for call in fake.calls), 1)
                    self.assertFalse(any(call[0] == "DELETE" for call in fake.calls))
                    self.assertFalse(report.cleanup_safe)
                    self.assertTrue(finish(report, CONFIG, previous_state(), control="observe", attempted=True).blocked)

    async def test_partial_receipts_and_unknown_usage_are_not_passes(self):
        for change in ("usage", "parameters", "tools", "reply", "persistence", "price"):
            with self.subTest(change=change):
                fake = FakeApp()
                if change == "usage":
                    fake.message["executionReceipt"]["runtime"]["modelCalls"][0]["usageKnown"] = False
                elif change == "parameters":
                    fake.message["executionReceipt"]["runtime"]["modelCalls"][0]["parameters"]["maxOutputTokens"] = 128
                elif change == "tools":
                    fake.message["executionReceipt"]["toolCallCount"] = 1
                elif change == "reply":
                    fake.message["content"] = "An unrelated response"
                elif change == "price":
                    fake.message["executionReceipt"]["runtime"]["modelCalls"][0]["cost"]["priceVersion"] = "unknown"
                else:
                    fake.overrides["GET", f"/api/sessions/{SID}/messages"] = response([])
                report = Report(RUN, stamp(NOW))
                await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK)
                self.assertNotEqual(report.stages["persistence"].outcome, "pass")
                self.assertEqual(report.chat_attempts, 1)
                self.assertTrue(report.cleanup_safe)

    async def test_v1_pending_cleanup_and_failed_legacy_cleanup_block_future_mutations(self):
        for result in (response({"state": "pending"}, 202), response({}, 500), CanaryError("deadline")):
            fake = FakeApp()
            fake.overrides["DELETE", f"/api/sessions/{SID}"] = result
            report = Report(RUN, stamp(NOW))
            await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK)
            self.assertFalse(report.cleanup_safe)
            self.assertTrue(finish(report, CONFIG, previous_state(), control="observe", attempted=True).blocked)
            self.assertFalse(any("reconcile" in call[1] or "initializations" in call[1] for call in fake.calls))

    async def test_legacy_204_and_exact_404_are_partial_and_block_the_next_run(self):
        fake = FakeApp()
        fake.overrides["DELETE", f"/api/sessions/{SID}"] = response(b"", 204)
        report = Report(RUN, stamp(NOW))
        await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK)
        self.assertEqual(report.stages["cleanup"].code, "logical_deleted")
        self.assertFalse(report.cleanup_safe)
        self.assertTrue(finish(report, CONFIG, previous_state(), control="observe", attempted=True).blocked)

    async def test_v1_resume_is_bounded_and_does_not_accept_future_or_partial_proof(self):
        for mutation in (
            {"messagesVerified": False}, {"sessionId": "another-session"},
            {"pendingUploads": [{"id": "unsettled"}]}, {"attempts": True},
            {"lastVerifiedAt": "2999-01-01T00:00:00Z"},
        ):
            fake = FakeApp()
            fake.overrides["POST", f"/api/sessions/{SID}/deletion/reconcile"] = response({
                **deletion_status(), **mutation,
            })
            report = Report(RUN, stamp(NOW))
            await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK)
            self.assertFalse(report.cleanup_safe)
            self.assertLessEqual(sum("reconcile" in call[1] for call in fake.calls), 2)
        fake = FakeApp()
        fake.overrides["POST", f"/api/sessions/{SID}/deletion/reconcile"] = response(deletion_status(complete=False), 202)
        report = Report(RUN, stamp(NOW))
        await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK)
        self.assertFalse(report.cleanup_safe)
        self.assertEqual(sum("reconcile" in call[1] for call in fake.calls), 2)

    async def test_cancelled_chat_is_not_replayed_and_leaves_cleanup_unknown(self):
        fake = FakeApp()
        fake.overrides["POST", "/api/chat"] = asyncio.CancelledError()
        report = Report(RUN, stamp(NOW))
        with self.assertRaises(asyncio.CancelledError):
            await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK)
        self.assertFalse(report.cleanup_safe)
        self.assertEqual(report.stages["cleanup"].code, "cleanup_pending")
        self.assertFalse(any(call[0] == "DELETE" for call in fake.calls))

    async def test_cancelled_persistence_cannot_start_cleanup_after_run_or_approval_deadline(self):
        for boundary in ("run", "approval", "allowed"):
            clock = [0.0]
            budget = Budget(
                105 if boundary != "approval" else 999,
                NOW + timedelta(seconds=121), lambda: clock[0],
                lambda: NOW + timedelta(seconds=clock[0]),
            )

            class DeadlineApp(FakeApp):
                async def request(self, method, url, **kwargs):
                    if url.endswith("/messages"):
                        clock[0] = 104 if boundary == "allowed" else 106 if boundary == "run" else 122
                        if boundary != "allowed":
                            self.calls.append((method, url, kwargs))
                            raise asyncio.CancelledError()
                    return await super().request(method, url, **kwargs)

            fake = DeadlineApp()
            report = Report(RUN, stamp(NOW))
            if boundary == "allowed":
                await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK, budget=budget)
                self.assertTrue(report.cleanup_safe)
                self.assertTrue(any(call[0] == "DELETE" for call in fake.calls))
            else:
                with self.assertRaises(asyncio.CancelledError):
                    await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK, budget=budget)
                self.assertFalse(report.cleanup_safe)
                self.assertFalse(any(call[0] == "DELETE" or "reconcile" in call[1] for call in fake.calls))
                self.assertEqual(report.stages["cleanup"].attempts, 0)
                self.assertEqual(report.stages["cleanup"].code, "deadline" if boundary == "run" else "approval_expired")

    async def test_expiry_between_cleanup_steps_refuses_the_next_mutation(self):
        clock = [0.0]
        budget = Budget(
            999, NOW + timedelta(seconds=121), lambda: clock[0],
            lambda: NOW + timedelta(seconds=clock[0]),
        )

        class ExpiringCleanup(FakeApp):
            async def request(self, method, url, **kwargs):
                result = await super().request(method, url, **kwargs)
                if method == "DELETE":
                    clock[0] = 122
                return result

        fake = ExpiringCleanup()
        report = Report(RUN, stamp(NOW))
        await chat(fake, CONFIG, "secret", SOURCE, report, pricing=BOOK, budget=budget)
        self.assertTrue(any(call[0] == "DELETE" for call in fake.calls))
        self.assertFalse(any("reconcile" in call[1] for call in fake.calls))
        self.assertEqual(report.stages["cleanup"].attempts, 1)
        self.assertFalse(report.cleanup_safe)

    async def test_ga_requires_both_server_selection_and_actual_handshake_protocol(self):
        for protocol, enabled, expected_sockets in (("preview", True, 0), ("ga", False, 0), ("ga", True, 1)):
            fake = FakeApp()
            fake.overrides["GET", "/api/voice/live/config"] = response({
                "openaiRealtimeProtocol": protocol, "enabledProviderIds": ["azure_openai"],
            })
            report = Report(RUN, stamp(NOW))
            report.mark("posture", "pass")
            await realtime(fake, replace(CONFIG, ga_enabled=enabled), "secret", SOURCE, ADVERTISED, report)
            self.assertEqual(len(fake.sockets), expected_sockets)
            self.assertEqual(report.stages["realtime"].outcome == "pass", bool(expected_sockets))
            if expected_sockets:
                self.assertEqual(len(fake.websocket.sent), 1)
                self.assertEqual(fake.websocket.sent[0]["type"], "session.update")
                self.assertIsNone(fake.websocket.sent[0]["session"]["turn_detection"])
                self.assertIn("/api/voice/live?", fake.sockets[0][0])
                self.assertNotIn("protocol=", fake.sockets[0][0])
                self.assertTrue(fake.websocket.closed)
        fake = FakeApp()
        fake.handshake_protocol = "preview"
        report = Report(RUN, stamp(NOW))
        report.mark("posture", "pass")
        await realtime(fake, replace(CONFIG, ga_enabled=True), "secret", SOURCE, ADVERTISED, report)
        self.assertEqual(report.stages["realtime"].code, "protocol_mismatch")
        self.assertEqual(fake.websocket.sent, [])

    async def test_ga_invalid_events_cannot_claim_pass_or_trigger_a_response(self):
        cases = [
            [{"type": "session.updated"}, {"type": "session.created"}],
            [{"type": "error", "error": {"message": "private-data"}}],
            ['{"type":"session.created","type":"session.updated"}'],
            [{"type": "session.created"}] * 33,
        ]
        for events in cases:
            fake = FakeApp()
            fake.websocket.events = events
            report = Report(RUN, stamp(NOW))
            report.mark("posture", "pass")
            await realtime(fake, replace(CONFIG, ga_enabled=True), "secret", SOURCE, ADVERTISED, report)
            self.assertNotEqual(report.stages["realtime"].outcome, "pass")
            self.assertEqual(len(fake.sockets), 1)
            self.assertFalse(any(event["type"] == "response.create" for event in fake.websocket.sent))
            self.assertNotIn("private-data", encoded(report.document()).decode())


def jwt_token(payload):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'RS256'})}.{encode(payload)}.synthetic-signature"


def api_claims():
    return {
        "iss": f"https://login.microsoftonline.com/{CONFIG.tenant_id}/v2.0",
        "tid": CONFIG.tenant_id, "aud": CONFIG.audience.removeprefix("api://"),
        "azp": CONFIG.client_id, "oid": CONFIG.object_id, "idtyp": "app",
        "iat": int(NOW.timestamp()), "exp": int((NOW + timedelta(hours=1)).timestamp()),
    }


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_oidc_exchange_is_fixed_and_application_token_stays_in_memory(self):
        env = environment()
        payload = {
            "iss": "https://token.actions.githubusercontent.com", "aud": "api://AzureADTokenExchange",
            "sub": f"repo:{RUN.repository}:ref:refs/heads/main",
            "repository": RUN.repository, "repository_id": str(RUN.repository_id),
            "ref": "refs/heads/main", "workflow_ref": env["GITHUB_WORKFLOW_REF"],
            "run_id": str(RUN.run_id), "run_attempt": "1", "sha": RUN.sha,
            "iat": int(NOW.timestamp()), "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        }
        calls = []
        expected = jwt_token(api_claims())

        async def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return response({"value": jwt_token(payload)} if method == "GET" else {
                "token_type": "Bearer", "access_token": expected,
            })

        output = io.StringIO()
        with redirect_stdout(output):
            actual = await acquire(CONFIG, RUN, env, NOW, transport=SimpleNamespace(request=request))
        self.assertEqual(actual, expected)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual([call[0] for call in calls], ["GET", "POST"])
        self.assertIn("/oauth2/v2.0/token", calls[-1][1])
        self.assertNotIn("graph", str(calls).lower())
        payload["run_attempt"] = "2"
        calls.clear()
        with self.assertRaises(CanaryError):
            await acquire(CONFIG, RUN, env, NOW, transport=SimpleNamespace(request=request))
        self.assertEqual(len(calls), 1)

    def test_wrong_identity_audience_role_tenant_and_expiration_refuse(self):
        validate_api_token(jwt_token(api_claims()), CONFIG, NOW)
        for change in (
            {"oid": CONFIG.client_id}, {"azp": environment()["DEPLOY_CLIENT_ID"]},
            {"tid": "wrong"}, {"aud": "https://graph.microsoft.com"},
            {"roles": ["admin"]}, {"scp": "user_impersonation"}, {"idtyp": "user"},
            {"email": "person@example.test"}, {"exp": int(NOW.timestamp())},
            {"appid": "different-app"},
        ):
            with self.subTest(change=change), self.assertRaises(CanaryError):
                validate_api_token(jwt_token({**api_claims(), **change}), CONFIG, NOW)


def run_metadata(run):
    return {
        "id": run.run_id, "run_number": run.number, "run_attempt": run.attempt,
        "head_sha": run.sha, "workflow_id": 123, "head_branch": "main",
        "repository": {"id": run.repository_id, "full_name": run.repository},
        "head_repository": {"id": run.repository_id}, "event": "schedule",
        "status": "completed", "conclusion": "failure",
        "created_at": stamp(NOW - timedelta(hours=6, minutes=1)),
        "updated_at": stamp(NOW - timedelta(hours=6) + timedelta(minutes=1)),
    }


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.before = previous_state()
        previous_run = self.before.report.run
        self.artifact = {
            "id": 99, "name": artifact_name(previous_run), "expired": False, "size_in_bytes": 2000,
            "workflow_run": {
                "id": previous_run.run_id, "repository_id": RUN.repository_id,
                "head_repository_id": RUN.repository_id, "head_branch": "main",
                "head_sha": previous_run.sha,
            },
        }
        self.previous_metadata = run_metadata(previous_run)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urlsplit(url).path
        if path.endswith("/application-canaries.yml"):
            return response({"id": 123, "path": ".github/workflows/application-canaries.yml"})
        if path.endswith("/runs/200"):
            return response(run_metadata(RUN))
        if path.endswith("/123/runs"):
            return response({"workflow_runs": [run_metadata(RUN), self.previous_metadata]})
        if path.endswith("/artifacts"):
            return response({"total_count": 1, "artifacts": [self.artifact]})
        raise AssertionError(path)

    async def test_reads_immediate_predecessor_even_if_health_alert_made_run_red(self):
        found = await locate(RUN, "repo-token", transport=SimpleNamespace(request=self.request))
        self.assertEqual(found.run, self.before.report.run)
        found.validate_state(self.before, RUN, NOW)
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(call[0] == "GET" for call in self.calls))

    async def test_wrong_repository_workflow_sha_expired_or_wrong_artifact_is_rejected(self):
        for field, value in (
            ("expired", True), ("name", "application-canary-state-unrelated"),
            ("size_in_bytes", 100000),
        ):
            saved = self.artifact.copy()
            self.artifact[field] = value
            with self.subTest(field=field), self.assertRaises(CanaryError):
                await locate(RUN, "repo-token", transport=SimpleNamespace(request=self.request))
            self.artifact = saved
        for field, value in (
            ("head_sha", "f" * 40), ("repository_id", 99), ("head_branch", "other"),
            ("id", RUN.run_id),
        ):
            saved = self.artifact["workflow_run"].copy()
            self.artifact["workflow_run"][field] = value
            with self.subTest(field=field), self.assertRaises(CanaryError):
                await locate(RUN, "repo-token", transport=SimpleNamespace(request=self.request))
            self.artifact["workflow_run"] = saved
        self.previous_metadata["workflow_id"] = 999
        with self.assertRaises(CanaryError):
            await locate(RUN, "repo-token", transport=SimpleNamespace(request=self.request))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_pins_only_the_approved_public_answers(self):
        resolver = PublicResolver({"web.example.test"})
        fixture = [{
            "hostname": "web.example.test", "host": "8.8.8.8", "port": 443,
            "family": socket.AF_INET, "proto": 0, "flags": 0,
        }]
        resolver._delegate.resolve = AsyncMock(return_value=fixture)
        result = await resolver.resolve("web.example.test", 443)
        self.assertEqual(result, fixture)
        for bad_ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.0.2.1", "::1"):
            resolver._delegate.resolve.return_value = [
                *fixture, {**fixture[0], "host": bad_ip},
            ]
            with self.subTest(bad_ip=bad_ip), self.assertRaises(CanaryError):
                await resolver.resolve("web.example.test", 443)
        resolver._delegate.resolve.reset_mock()
        with self.assertRaises(CanaryError):
            await resolver.resolve("unapproved.example.test", 443)
        resolver._delegate.resolve.assert_not_called()
        await resolver.close()

    async def test_redirect_and_payload_caps_stop_before_followup_or_json(self):
        class Content:
            def iter_chunked(self, _):
                async def iterate():
                    yield b"x" * (MAX_HTTP_BYTES + 1)
                return iterate()

        class HttpResponse:
            status = 200
            content_length = None
            headers = {}
            content_type = "application/json"
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

        result = HttpResponse()
        sent = []

        def request(method, url, **kwargs):
            sent.append((method, url, kwargs))
            return result

        transport = Transport({CONFIG.web_origin})
        transport._client = SimpleNamespace(request=request)
        with self.assertRaisesRegex(CanaryError, "response_too_large"):
            await transport.request("GET", CONFIG.web_origin + "/api/models", token="secret")
        self.assertFalse(sent[0][2]["allow_redirects"])
        result.status = 302
        with self.assertRaisesRegex(CanaryError, "redirect_rejected"):
            await transport.request("GET", CONFIG.web_origin + "/api/models", token="secret")
        self.assertEqual(len(sent), 2)
        with self.assertRaisesRegex(CanaryError, "redirect_rejected"):
            await transport._reject_redirect(None, None, None)
        with self.assertRaises(CanaryError):
            await transport.request("GET", "https://other.test/api/models", token="secret")
        self.assertEqual(len(sent), 2)


class WorkflowAndCliTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_default_off_main_only_job_scoped_permissions_and_pinned_actions(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertFalse(self.workflow["concurrency"]["cancel-in-progress"])
        self.assertEqual(self.workflow["on" if "on" in self.workflow else True]["schedule"], [
            {"cron": "23 */6 * * *"},
        ])
        gating.WorkflowPermissionBoundaryTests().assert_permission_boundary(self.workflow)
        for job in self.workflow["jobs"].values():
            self.assertIn("refs/heads/main", job["if"])
            self.assertLessEqual(job["timeout-minutes"], 5)
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                self.assertNotRegex(step.get("run", ""), r"\baz(?:d)?\s|workflow run|curl|response\.create")
        observe = self.workflow["jobs"]["observe"]
        self.assertIn("vars.AI4IA_CANARY_ENABLED == 'true'", observe["if"])
        self.assertEqual(observe["permissions"], {"contents": "read", "id-token": "write"})
        self.assertNotIn("environment", observe)
        upload = next(index for index, step in enumerate(observe["steps"]) if step["name"] == "Retain final content-free state")
        notify = next(index for index, step in enumerate(observe["steps"]) if step["name"] == "Publish coverage and alert transitions")
        self.assertLess(upload, notify)

    def test_new_permission_consumers_are_load_bearing(self):
        checker = gating.WorkflowPermissionBoundaryTests()
        for command, permission in (("locate", {"actions": "read"}), ("observe", {"id-token": "write"})):
            job = {"steps": [{"run": f"python -m scripts.canaries {command} --directory \"$RUNNER_TEMP/x\""}]}
            self.assertEqual(checker.required_permissions(job), permission)
            document = {"permissions": {}, "jobs": {"test": {**job, "permissions": permission}}}
            checker.assert_permission_boundary(document)
            document["jobs"]["test"]["permissions"] = {}
            with self.assertRaises(AssertionError):
                checker.assert_permission_boundary(document)

    def test_actual_prepare_workflow_command_records_disabled_full_coverage_without_oidc(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "application-canary"
            first = replace(RUN, number=1)
            cli.write_json(directory / "history.json", {
                "run": asdict(first), "code": "ok", "predecessor": None,
            })
            env = {
                **os.environ, **environment(first), "RUNNER_TEMP": temporary,
                "AI4IA_CANARY_ENABLED": "false",
            }
            # Execute the actual YAML command, replacing only the shell variable
            # with its explicitly controlled fixture directory.
            step = next(item for item in self.workflow["jobs"]["prepare"]["steps"] if item.get("id") == "prepare")
            command = step["run"].replace('"$RUNNER_TEMP/application-canary"', str(directory))
            argv = command.split()
            argv[0] = sys.executable
            result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = State.parse(cli.read_json(directory / "state.json"))
            self.assertEqual(set(state.report.stages), set(STAGES))
            self.assertTrue(all(row.code == "disabled" for row in state.report.stages.values()))
            self.assertEqual(state.observations, 0)
            self.assertIsNone(state.chat.failures)

    def test_missing_predecessor_produces_durable_unknown_not_new_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cli.write_json(directory / "history.json", {
                "run": asdict(RUN), "code": "state_missing", "predecessor": None,
            })
            with patch.object(cli, "utc_now", return_value=NOW):
                self.assertEqual(cli.prepare_command(directory, environment()), 0)
            state = State.parse(cli.read_json(directory / "state.json"))
            self.assertTrue(state.blocked)
            self.assertFalse((directory / "handoff.json").exists())
            self.assertIsNone(state.chat.failures)

    def test_notifications_fire_after_state_is_written_and_do_not_echo_payloads(self):
        report = Report(RUN, stamp(NOW))
        report.mark("auth", "fail", "auth_rejected")
        state = replace(
            finish(report, CONFIG, previous_state(), control="observe", attempted=True),
            chat=Counter(3, True, "firing"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cli.write_state(directory, state)
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.notify_command(directory, environment())
            self.assertEqual(result, 3)
            self.assertIn("threshold reached", output.getvalue())
            self.assertNotIn(CONFIG.client_id, output.getvalue())
            self.assertNotIn(SID, output.getvalue())
            self.assertEqual(State.parse(cli.read_json(directory / "state.json")).chat, state.chat)


class ObserveCliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = patch("scripts.canaries.monitor.utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    async def test_actual_observe_entrypoint_uses_validated_handoff_and_cannot_reset_a_lost_one(self):
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                before = previous_state()
                metadata = run_metadata(before.report.run)
                cli.write_json(directory / "handoff.json", {
                    "run": asdict(RUN), "prepared_at": stamp(NOW),
                    "scope_digest": CONFIG.scope_digest if valid else "f" * 64,
                    "previous": before.document(),
                    "predecessor": {
                        "run": asdict(before.report.run), "artifact_id": 99,
                        "created_at": metadata["created_at"], "updated_at": metadata["updated_at"],
                    },
                })
                original_read = cli.read_json

                def read_source(path, **kwargs):
                    if path == cli.ROOT / "infra" / "models.json":
                        return copy.deepcopy(SOURCE)
                    return original_read(path, **kwargs)

                app = FakeApp()
                acquire_token = AsyncMock(return_value="never-export-token")
                with (
                    patch.object(cli, "read_json", side_effect=read_source),
                    patch.object(cli, "utc_now", return_value=NOW),
                    patch("scripts.canaries.identity.acquire", acquire_token),
                    patch("scripts.canaries.transport.Transport", return_value=app),
                    patch("scripts.canaries.monitor.load_pricing", return_value=BOOK),
                ):
                    self.assertEqual(await cli.observe_command(directory, environment()), 0)
                state = State.parse(original_read(directory / "state.json"))
                if valid:
                    acquire_token.assert_awaited_once()
                    self.assertEqual(sum(call[0] == "POST" and call[1].endswith("/chat") for call in app.calls), 1)
                    self.assertEqual(state.observations, 1)
                    self.assertEqual(state.chat.failures, 0)
                    self.assertFalse(state.blocked)
                else:
                    acquire_token.assert_not_awaited()
                    self.assertEqual(app.calls, [])
                    self.assertTrue(state.blocked)
                    with self.assertRaises(CanaryError):
                        admit(CONFIG, replace(RUN, number=3, run_id=300), state, NOW, bootstrap=True)
                self.assertNotIn("never-export-token", encoded(state.document()).decode())

    async def test_absent_handoff_never_becomes_a_fresh_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            acquire_token = AsyncMock()
            with (
                patch.object(cli, "utc_now", return_value=NOW),
                patch("scripts.canaries.identity.acquire", acquire_token),
            ):
                self.assertEqual(await cli.observe_command(directory, environment()), 0)
            state = State.parse(cli.read_json(directory / "state.json"))
            self.assertTrue(state.blocked)
            self.assertIsNone(state.chat.failures)
            acquire_token.assert_not_awaited()

    async def test_malformed_predecessor_metadata_is_never_promoted_to_trusted_state(self):
        for field, value in (("artifact_id", None), ("created_at", "malformed"), ("run", None)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                before = previous_state()
                metadata = run_metadata(before.report.run)
                predecessor = {
                    "run": asdict(before.report.run), "artifact_id": 99,
                    "created_at": metadata["created_at"], "updated_at": metadata["updated_at"],
                }
                if value is None:
                    predecessor.pop(field)
                else:
                    predecessor[field] = value
                cli.write_json(directory / "handoff.json", {
                    "run": asdict(RUN), "prepared_at": stamp(NOW),
                    "scope_digest": CONFIG.scope_digest,
                    "previous": before.document(), "predecessor": predecessor,
                })
                acquire_token = AsyncMock()
                with (
                    patch.object(cli, "utc_now", return_value=NOW),
                    patch("scripts.canaries.identity.acquire", acquire_token),
                ):
                    self.assertEqual(await cli.observe_command(directory, environment()), 0)
                acquire_token.assert_not_awaited()
                state = State.parse(cli.read_json(directory / "state.json"))
                self.assertTrue(state.blocked)
                self.assertFalse(state.report.cleanup_safe)
                self.assertIsNone(state.previous_run_id)
                with self.assertRaises(CanaryError):
                    admit(CONFIG, replace(RUN, number=3, run_id=300), state, NOW, bootstrap=False)
                with self.assertRaises(CanaryError):
                    admit(CONFIG, replace(RUN, number=3, run_id=300), state, NOW, bootstrap=True)

    async def test_ga_uses_a_distinct_token_only_after_reported_ga_and_its_own_preflight(self):
        actor = RealtimeActor(
            "88888888-8888-8888-8888-888888888888",
            "99999999-9999-9999-9999-999999999999",
        )
        config = replace(CONFIG, ga_enabled=True, realtime_actor=actor)
        for protocol in ("preview", "ga"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                before = previous_state(config=config)
                metadata = run_metadata(before.report.run)
                cli.write_json(directory / "handoff.json", {
                    "run": asdict(RUN), "prepared_at": stamp(NOW), "scope_digest": config.scope_digest,
                    "previous": before.document(),
                    "predecessor": {
                        "run": asdict(before.report.run), "artifact_id": 99,
                        "created_at": metadata["created_at"], "updated_at": metadata["updated_at"],
                    },
                })
                app = FakeApp()
                app.overrides["GET", "/api/voice/live/config"] = response({
                    "openaiRealtimeProtocol": protocol, "enabledProviderIds": ["azure_openai"],
                })
                selected_clients = []

                async def token(selected, *_args, **_kwargs):
                    selected_clients.append(selected.client_id)
                    return "ga-token" if selected.client_id == actor.client_id else "monitor-token"

                original_read = cli.read_json

                def read_source(path, **kwargs):
                    return copy.deepcopy(SOURCE) if path == cli.ROOT / "infra" / "models.json" else original_read(path, **kwargs)

                with (
                    patch.object(cli, "read_json", side_effect=read_source),
                    patch.object(cli, "utc_now", return_value=NOW),
                    patch("scripts.canaries.identity.acquire", side_effect=token),
                    patch("scripts.canaries.transport.Transport", return_value=app),
                    patch("scripts.canaries.monitor.load_pricing", return_value=BOOK),
                ):
                    self.assertEqual(await cli.observe_command(directory, environment(config=config)), 0)
                self.assertEqual(selected_clients, [CONFIG.client_id] + ([actor.client_id] if protocol == "ga" else []))
                self.assertEqual(len(app.sockets), int(protocol == "ga"))
                if protocol == "ga":
                    self.assertEqual(app.sockets[0][1]["protocols"], ("ai4ia-bearer", "ga-token"))
                    ga_requests = [
                        call for call in app.calls
                        if call[1].startswith(CONFIG.api_origin) and "/voice/live/config" not in call[1]
                    ]
                    self.assertTrue(ga_requests)
                    self.assertTrue(all(call[2]["token"] == "ga-token" for call in ga_requests))
                state = State.parse(original_read(directory / "state.json"))
                self.assertNotIn("ga-token", encoded(state.document()).decode())
                self.assertNotIn("monitor-token", encoded(state.document()).decode())


if __name__ == "__main__":
    unittest.main()
