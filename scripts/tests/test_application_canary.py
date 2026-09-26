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
from scripts.canaries import resolution as resolution_cli
from scripts.canaries.configuration import Configuration, RealtimeActor, current_run
from scripts.canaries.contracts import (
    CanaryError, INTERVAL_SECONDS, MAX_HTTP_BYTES, MAX_OUTPUT_TOKENS,
    Report, Run, STAGES, digest, encoded, public_origin, stamp, strict_json,
)
from scripts.canaries.github import artifact_name, attested_runs, locate
from scripts.canaries.identity import acquire, validate_api_token
from scripts.canaries.monitor import Budget, chat, realtime
from scripts.canaries.resolution import Attested, Resolution
from scripts.canaries.state import Counter, State, admit, finish, resolve
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
CLEANUP_FIXTURE = json.loads(
    (ROOT / "scripts" / "fixtures" / "conversation-cleanup.json").read_text(encoding="utf-8")
)
SID = CLEANUP_FIXTURE["session"]["id"]


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
    return copy.deepcopy(CLEANUP_FIXTURE["verified" if complete else "pending"])


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
        self.cleanup_passes = 0

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
            self.cleanup_passes += 1
            if self.cleanup_passes == 1:
                return response(copy.deepcopy(CLEANUP_FIXTURE["progress"]), 202)
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

    async def test_runtime_catalog_gate_and_shared_v1_proof_both_apply(self):
        for enabled in (False, True):
            for verified in (False, True):
                with self.subTest(runtimeEnabled=enabled, messagesVerified=verified):
                    source = copy.deepcopy(SOURCE)
                    source["catalog"][0]["runtimeEnabled"] = enabled
                    fake = FakeApp()
                    proof = {**deletion_status(), "messagesVerified": verified}
                    path = f"/api/sessions/{SID}"
                    fake.overrides["POST", f"{path}/deletion/reconcile"] = response(proof)
                    fake.overrides["GET", f"{path}/deletion"] = response(proof)
                    report = Report(RUN, stamp(NOW))
                    await chat(fake, CONFIG, "secret", source, report, pricing=BOOK)
                    calls = [(method, urlsplit(url).path) for method, url, _ in fake.calls]
                    if not enabled:
                        self.assertEqual(calls, [("GET", "/api/models")])
                        self.assertEqual(report.stages["catalog"].code, "no_compatible_model")
                        self.assertEqual(report.chat_attempts, 0)
                        continue
                    self.assertEqual(calls.count(("POST", "/api/sessions")), 1)
                    self.assertEqual(calls.count(("POST", "/api/chat")), 1)
                    self.assertEqual(report.stages["model"].outcome, "pass")
                    self.assertEqual(report.cleanup_safe, verified)
                    expected = [("DELETE", path), ("POST", f"{path}/deletion/reconcile")]
                    if verified:
                        expected.append(("GET", f"{path}/deletion"))
                        self.assertEqual(report.stages["cleanup"].code, "cleanup_verified")
                    self.assertEqual(
                        [call for call in calls if call[1].startswith(path) and call[1] != f"{path}/messages"],
                        expected,
                    )

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

    def test_resolution_is_dispatch_only_and_reaches_only_admission(self):
        inputs = self.workflow["on" if "on" in self.workflow else True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["operation"]["options"], ["observe", "bootstrap", "resolve"])
        self.assertEqual((inputs["resolution_sha256"]["type"], inputs["resolution_sha256"]["default"]), ("string", ""))
        prepare, observe = self.workflow["jobs"]["prepare"], self.workflow["jobs"]["observe"]
        self.assertEqual(prepare["env"]["AI4IA_CANARY_RESOLUTION"], "${{ vars.AI4IA_CANARY_RESOLUTION }}")
        self.assertEqual(prepare["env"]["CANARY_RESOLUTION_SHA256"], "${{ inputs.resolution_sha256 || '' }}")
        self.assertEqual(prepare["permissions"], {"contents": "read", "actions": "read"})
        for key in ("AI4IA_CANARY_RESOLUTION", "CANARY_RESOLUTION_SHA256"):
            self.assertNotIn(key, observe["env"])
        for job in self.workflow["jobs"].values():
            for step in job["steps"]:
                # Dispatch values reach Python through env, never a shell string.
                self.assertNotRegex(step.get("run", ""), r"\$\{\{")

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
                    patch("scripts.canaries.monitor.load_model_pricing", return_value=BOOK),
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
            # The block still records the configured lease, so resolving it must retire that lease.
            self.assertEqual(state.approval_digest, CONFIG.approval_digest)
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
                    patch("scripts.canaries.monitor.load_model_pricing", return_value=BOOK),
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


OLD_APPROVAL, NEW_APPROVAL = CONFIG.approval_id, "77777777-7777-7777-7777-777777777777"
LEASE = replace(CONFIG, approval_id=NEW_APPROVAL)
LOST = Run(RUN.repository, RUN.repository_id, 200, 2, 1, "c" * 40)
BLOCKED_RUN = Run(RUN.repository, RUN.repository_id, 300, 3, 1, "d" * 40)
RESOLVE_RUN = Run(RUN.repository, RUN.repository_id, 400, 4, 1, "e" * 40)
NEXT_RUN = Run(RUN.repository, RUN.repository_id, 500, 5, 1, "f" * 40)
ATTESTED = [Attested(LOST, stamp(NOW - timedelta(hours=7)))]


def blocked_state(run=BLOCKED_RUN, *, approval=None, observed=NOW - timedelta(hours=6)):
    # The shape run 36272392656 retained after its predecessor's state was lost.
    report = Report(run, stamp(observed))
    report.unobserved("state_missing")
    report.cleanup_safe = False
    return replace(finish(report, None, None, control="blocked"), approval_digest=approval)


def record_text(**changes):
    document = {
        "version": 1, "blocked_run_id": BLOCKED_RUN.run_id, "lost_run_ids": [LOST.run_id],
        "evidence": "no_application_write", "approval_id": NEW_APPROVAL,
        "superseded_approval_digests": [digest(OLD_APPROVAL)],
    }
    document.update(changes)
    return json.dumps(document)


def resolved_state(previous=None, attested=ATTESTED):
    previous = previous or blocked_state()
    record = Resolution.load(record_text())
    report = Report(RESOLVE_RUN, stamp(NOW))
    report.unobserved("resolved")
    resolved = resolve(LEASE, record, attested, RESOLVE_RUN, previous, NOW, record.sha256)
    return finish(report, LEASE, previous, control="resolved", resolved=resolved)


class ResolutionRecordTests(unittest.TestCase):
    def test_record_digest_is_canonical_and_ambiguous_records_refuse(self):
        record = Resolution.load(record_text())
        reordered = json.dumps(dict(reversed(json.loads(record_text()).items())), indent=2)
        self.assertEqual(Resolution.load(reordered).sha256, record.sha256)
        self.assertEqual(record.sha256, digest(record.document()))
        self.assertNotEqual(Resolution.load(record_text(lost_run_ids=[199])).sha256, record.sha256)
        missing = {key: value for key, value in json.loads(record_text()).items() if key != "evidence"}
        cases = {
            "unknown field": record_text(note="free text"),
            "missing field": json.dumps(missing),
            "version": record_text(version=2),
            "unsorted lost runs": record_text(lost_run_ids=[250, LOST.run_id]),
            "duplicate lost run": record_text(lost_run_ids=[LOST.run_id, LOST.run_id]),
            "lost run not before the block": record_text(lost_run_ids=[BLOCKED_RUN.run_id]),
            "no lost run": record_text(lost_run_ids=[]),
            "too many lost runs": record_text(lost_run_ids=[1, 2, 3, 4, 5]),
            "boolean run id": record_text(blocked_run_id=True),
            "unadmitted evidence": record_text(evidence="cleanup_done"),
            "malformed approval": record_text(approval_id="not-a-guid"),
            "new lease already superseded": record_text(superseded_approval_digests=[digest(NEW_APPROVAL)]),
            "unsorted superseded": record_text(superseded_approval_digests=["f" * 64, "0" * 64]),
            "superseded shape": record_text(superseded_approval_digests=["F" * 64]),
            "too many superseded": record_text(superseded_approval_digests=[f"{i:064x}" for i in range(9)]),
            "duplicate key": record_text()[:-1] + ', "version": 1}',
            "oversize": record_text() + " " * 5000,
            "not an object": "[]",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaisesRegex(CanaryError, "resolution_invalid"):
                Resolution.load(text)

    def test_digest_helper_runs_offline_and_approves_only_a_valid_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            path.write_text(record_text(), encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")}
            result = subprocess.run(
                [sys.executable, "-m", "scripts.canaries.resolution", str(path)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[-1], Resolution.load(record_text()).sha256)
            path.write_text(record_text(evidence="guessed"), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output), patch("sys.stderr", io.StringIO()):
                self.assertEqual(resolution_cli.main([str(path)]), 2)
            self.assertNotRegex(output.getvalue(), r"[0-9a-f]{64}")


class ResolutionTransitionTests(unittest.TestCase):
    def test_one_attested_resolution_starts_a_new_lease_exactly_once(self):
        blocked = blocked_state()
        record = Resolution.load(record_text())
        # Control: nothing but the attested resolution leaves a blocked chain.
        for bootstrap in (False, True):
            with self.assertRaisesRegex(CanaryError, "state_blocked"):
                admit(LEASE, RESOLVE_RUN, blocked, NOW, bootstrap=bootstrap)
        state = State.parse(resolved_state(blocked).document())
        self.assertEqual((state.blocked, state.control, state.observations), (False, "resolved", 0))
        self.assertEqual((state.scope_digest, state.approval_digest), (LEASE.scope_digest, LEASE.approval_digest))
        # The chain lost its last attempt, so the blocked observation bounds cadence.
        self.assertEqual((state.previous_run_id, state.last_attempt_at), (BLOCKED_RUN.run_id, blocked.report.observed_at))
        self.assertEqual(state.report.coverage, "unscored")
        self.assertIsNone(state.chat.failures)
        self.assertEqual(state.resolution, {
            "sha256": record.sha256, "blocked_run_id": BLOCKED_RUN.run_id,
            "lost_run_ids": [LOST.run_id], "evidence": "no_application_write",
        })
        # The new lease observes normally...
        later = NOW + timedelta(minutes=1)
        admit(LEASE, NEXT_RUN, state, later, bootstrap=False)
        # ...and the same approved record cannot resolve anything again.
        with self.assertRaisesRegex(CanaryError, "not_blocked"):
            resolve(LEASE, record, ATTESTED, NEXT_RUN, state, later, record.sha256)
        reblocked = blocked_state(replace(NEXT_RUN, run_id=600, number=6), observed=later)
        with self.assertRaisesRegex(CanaryError, "resolution_stale"):
            resolve(LEASE, record, ATTESTED, replace(NEXT_RUN, run_id=700, number=7), reblocked, later, record.sha256)

    def test_resolution_keeps_alerts_and_cadence_and_never_claims_health(self):
        alerting = replace(blocked_state(), chat=Counter(None, True, "none"))
        self.assertTrue(resolved_state(alerting).chat.alerting)
        recent = resolved_state(attested=[Attested(LOST, stamp(NOW - timedelta(hours=1)))])
        with self.assertRaisesRegex(CanaryError, "cadence"):
            admit(LEASE, NEXT_RUN, recent, NOW + timedelta(minutes=1), bootstrap=False)
        with self.assertRaisesRegex(ValueError, "admitted resolution"):
            finish(Report(RESOLVE_RUN, stamp(NOW)), LEASE, blocked_state(), control="resolved")

    def test_a_lost_attempt_time_is_bounded_by_the_block_not_by_the_listing(self):
        # Incident shape: the chain lost its last attempt. The listed run finished
        # seven hours ago, but the chain blocked one hour ago.
        recent_block = blocked_state(observed=NOW - timedelta(hours=1))
        self.assertIsNone(recent_block.last_attempt_at)
        floored = resolved_state(recent_block)
        self.assertEqual(floored.last_attempt_at, recent_block.report.observed_at)
        with self.assertRaisesRegex(CanaryError, "cadence"):
            admit(LEASE, NEXT_RUN, floored, NOW + timedelta(minutes=1), bootstrap=False)
        # Control: a chain that kept its last attempt anchors exactly there.
        known = replace(recent_block, last_attempt_at=stamp(NOW - timedelta(hours=7)))
        exact = resolved_state(known)
        self.assertEqual(exact.last_attempt_at, stamp(NOW - timedelta(hours=7)))
        admit(LEASE, NEXT_RUN, exact, NOW + timedelta(minutes=1), bootstrap=False)

    def test_legacy_states_parse_and_only_resolved_states_carry_a_resolution(self):
        legacy = previous_state().document()
        # Non-resolved states keep the previous schema's exact key set, so the
        # previous parser still reads them after a revert.
        self.assertNotIn("resolution", legacy)
        self.assertEqual(set(legacy), set(State.__dataclass_fields__) - {"resolution"})
        State.parse(legacy)
        valid = resolved_state().document()
        self.assertIn("resolution", valid)
        mutations = {
            "resolved without a resolution": lambda value: value.update(resolution=None),
            "resolved but blocked": lambda value: value.update(blocked=True),
            "resolution for another block": lambda value: value["resolution"].update(blocked_run_id=299),
            "resolved with observations": lambda value: value.update(observations=1),
            "resolved without a lease": lambda value: value.update(approval_digest=None),
        }
        for label, mutate in mutations.items():
            document = copy.deepcopy(valid)
            mutate(document)
            with self.subTest(label), self.assertRaises(CanaryError):
                State.parse(document)
        bootstrap = previous_state().document()
        bootstrap["resolution"] = valid["resolution"]
        with self.assertRaisesRegex(CanaryError, "state_invalid"):
            State.parse(bootstrap)


class ResolutionCliTests(unittest.TestCase):
    def prepare(self, *, record=None, approval=None, attested=ATTESTED, previous=None,
                config=LEASE, event="workflow_dispatch", enabled=True):
        record = record_text() if record is None else record
        approval = Resolution.load(record_text()).sha256 if approval is None else approval
        previous = previous or blocked_state()
        metadata = run_metadata(previous.report.run)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cli.write_json(directory / "history.json", {
                "run": asdict(RESOLVE_RUN), "code": "ok",
                "predecessor": {
                    "run": asdict(previous.report.run), "artifact_id": 99,
                    "created_at": metadata["created_at"], "updated_at": metadata["updated_at"],
                },
                "resolution": None if attested is None else {
                    "code": "ok", "runs": [row.document() for row in attested],
                },
            })
            cli.write_json(directory / "previous" / "state.json", previous.document())
            env = {
                **environment(RESOLVE_RUN, config), "GITHUB_EVENT_NAME": event,
                "CANARY_OPERATION": "resolve", "AI4IA_CANARY_RESOLUTION": record,
                "CANARY_RESOLUTION_SHA256": approval,
                "AI4IA_CANARY_ENABLED": "true" if enabled else "false",
            }
            with patch.object(cli, "utc_now", return_value=NOW):
                self.assertEqual(cli.prepare_command(directory, env), 0)
                self.assertFalse((directory / "handoff.json").exists(), "a resolution never observes")
                state = State.parse(cli.read_json(directory / "state.json"))
                output = io.StringIO()
                with redirect_stdout(output):
                    notified = cli.notify_command(directory, environment(RESOLVE_RUN, config))
        return state, notified, output.getvalue()

    def test_the_approved_record_resolves_and_every_stale_or_mismatched_one_stays_blocked(self):
        state, notified, output = self.prepare()
        self.assertEqual((state.blocked, state.control, notified), (False, "resolved", 0))
        self.assertIn("resolved by owner attestation", output)
        stale = record_text(blocked_run_id=250)
        cases = [
            ("resolution_unapproved", {"approval": ""}),
            ("resolution_unapproved", {"approval": "0" * 64}),
            ("resolution_stale", {"record": stale, "approval": Resolution.load(stale).sha256}),
            ("resolution_invalid", {"config": replace(LEASE, approval_id="88888888-8888-8888-8888-888888888888")}),
            ("resolution_invalid", {"previous": blocked_state(approval=digest("99999999-9999-9999-9999-999999999999"))}),
            ("resolution_invalid", {"attested": None}),
            ("resolution_invalid", {"attested": []}),
            ("resolution_invalid", {"attested": [Attested(replace(LOST, run_id=201), ATTESTED[0].updated_at)]}),
            ("resolution_invalid", {"attested": [Attested(replace(LOST, number=3), ATTESTED[0].updated_at)]}),
            ("resolution_invalid", {"attested": [Attested(LOST, stamp(NOW + timedelta(hours=1)))]}),
            ("resolution_invalid", {"record": record_text(evidence="cleanup_done")}),
            ("not_ready", {"enabled": False}),
            ("invalid_configuration", {"event": "schedule"}),
        ]
        for code, change in cases:
            with self.subTest(code=code, change=sorted(change)):
                state, notified, _ = self.prepare(**change)
                self.assertTrue(state.blocked)
                self.assertEqual(state.control, "blocked")
                self.assertEqual({row.code for row in state.report.stages.values()}, {code})
                self.assertEqual(notified, 3)

    def test_the_blocked_lease_must_be_superseded_by_the_record(self):
        acknowledged = blocked_state(approval=digest(OLD_APPROVAL))
        self.assertFalse(self.prepare(previous=acknowledged)[0].blocked)
        unacknowledged = record_text(superseded_approval_digests=[])
        state = self.prepare(
            previous=acknowledged, record=unacknowledged, approval=Resolution.load(unacknowledged).sha256,
        )[0]
        self.assertTrue(state.blocked)

    def test_a_chain_that_lost_its_lease_still_retires_it(self):
        # Run 36272392656's shape: its predecessor was lost, so it kept no lease and no attempt.
        incident = blocked_state()
        self.assertEqual((incident.approval_digest, incident.last_attempt_at), (None, None))
        # Control: naming #49's retired lease resolves into the new one.
        state = self.prepare(previous=incident)[0]
        self.assertEqual((state.blocked, state.approval_digest), (False, LEASE.approval_digest))

        def refused(record, config=LEASE):
            try:
                approval = Resolution.load(record).sha256
            except CanaryError:
                approval = "0" * 64  # an invalid record has no approvable digest
            state = self.prepare(previous=incident, record=record, approval=approval, config=config)[0]
            self.assertTrue(state.blocked)
            self.assertEqual({row.code for row in state.report.stages.values()}, {"resolution_invalid"})

        with self.subTest("no retired lease named"):
            refused(record_text(superseded_approval_digests=[]))
        with self.subTest("the used lease renewed, unlisted"):
            # The review's scenario: only expires_at edited, the used lease re-approved.
            refused(record_text(approval_id=OLD_APPROVAL, superseded_approval_digests=[]), config=CONFIG)
        with self.subTest("the used lease renewed, listed"):
            refused(record_text(approval_id=OLD_APPROVAL), config=CONFIG)

    def test_a_block_without_a_predecessor_records_the_configured_lease(self):
        def introduce(**changes):
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                cli.write_json(directory / "history.json", {
                    "run": asdict(RESOLVE_RUN), "code": "state_missing", "predecessor": None,
                    "resolution": None,
                })
                with patch.object(cli, "utc_now", return_value=NOW):
                    self.assertEqual(cli.prepare_command(directory, {**environment(RESOLVE_RUN), **changes}), 0)
                return State.parse(cli.read_json(directory / "state.json"))

        labelled = introduce()
        self.assertTrue(labelled.blocked)
        self.assertEqual(labelled.approval_digest, CONFIG.approval_digest)
        # Resolving it must now retire that lease; renewing it is refused.
        renewal = record_text(approval_id=OLD_APPROVAL, superseded_approval_digests=[])
        blocked = replace(labelled, report=replace(labelled.report, run=BLOCKED_RUN, observed_at=stamp(NOW - timedelta(hours=6))))
        state = self.prepare(previous=blocked, record=renewal, approval=Resolution.load(renewal).sha256, config=CONFIG)[0]
        self.assertTrue(state.blocked)
        # A disabled or unloadable configuration leaves the label unknown, never the block hidden.
        for changes in ({"AI4IA_CANARY_ENABLED": "false"}, {"AI4IA_CANARY_HARD_USD_CAP": "5"}):
            with self.subTest(changes=changes):
                state = introduce(**changes)
                self.assertTrue(state.blocked)
                self.assertIsNone(state.approval_digest)

    def test_an_inherited_block_never_adopts_the_lease_prepared_to_resolve_it(self):
        # The owner configures the new lease first; a schedule before the dispatch
        # must not record it, or the resolution could never admit it.
        incident = blocked_state()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = run_metadata(incident.report.run)
            cli.write_json(directory / "history.json", {
                "run": asdict(RESOLVE_RUN), "code": "ok", "resolution": None,
                "predecessor": {
                    "run": asdict(incident.report.run), "artifact_id": 99,
                    "created_at": metadata["created_at"], "updated_at": metadata["updated_at"],
                },
            })
            cli.write_json(directory / "previous" / "state.json", incident.document())
            with patch.object(cli, "utc_now", return_value=NOW):
                self.assertEqual(cli.prepare_command(directory, environment(RESOLVE_RUN, LEASE)), 0)
            inherited = State.parse(cli.read_json(directory / "state.json"))
        self.assertTrue(inherited.blocked)
        self.assertEqual(inherited.report.stages["platform"].code, "state_blocked")
        self.assertIsNone(inherited.approval_digest)
        # Control: the prepared lease still resolves the chain at the next run.
        record = record_text(blocked_run_id=RESOLVE_RUN.run_id)
        resolve(LEASE, Resolution.load(record), ATTESTED, NEXT_RUN, inherited,
                NOW + timedelta(minutes=1), Resolution.load(record).sha256)

    def test_a_mistaken_resolution_never_blocks_a_healthy_chain(self):
        report = Report(BLOCKED_RUN, stamp(NOW - timedelta(hours=6)))
        report.unobserved("bootstrap")
        healthy = finish(report, LEASE, None, control="bootstrap")
        state, notified, _ = self.prepare(previous=healthy)
        self.assertEqual((state.blocked, notified), (False, 3))
        self.assertEqual(state.report.stages["platform"].code, "not_blocked")
        # Control: the chain it left still admits the next observation.
        admit(LEASE, NEXT_RUN, state, NOW + timedelta(minutes=1), bootstrap=False)


class AttestedRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.metadata = run_metadata(LOST)

    async def request(self, method, url, **_):
        path = urlsplit(url).path
        if path.endswith("/application-canaries.yml"):
            return response({"id": 123, "path": ".github/workflows/application-canaries.yml"})
        if path.endswith(f"/runs/{LOST.run_id}"):
            return response(self.metadata)
        return response({"message": "Not Found"}, 404)

    async def test_only_exact_completed_first_attempt_runs_of_this_workflow_are_attested(self):
        transport = SimpleNamespace(request=self.request)
        rows = await attested_runs(RESOLVE_RUN, [LOST.run_id], "repo-token", transport=transport)
        self.assertEqual(rows, [Attested(LOST, self.metadata["updated_at"])])
        for field, value in (
            ("run_attempt", 2), ("workflow_id", 999), ("status", "in_progress"),
            ("head_branch", "other"), ("id", 201), ("run_number", RESOLVE_RUN.number),
            ("event", "pull_request"),
        ):
            saved = copy.deepcopy(self.metadata)
            self.metadata[field] = value
            with self.subTest(field=field), self.assertRaises(CanaryError):
                await attested_runs(RESOLVE_RUN, [LOST.run_id], "repo-token", transport=transport)
            self.metadata = saved
        for identifiers, token in (([LOST.run_id + 1], "repo-token"), ([LOST.run_id], "")):
            with self.subTest(identifiers=identifiers), self.assertRaises(CanaryError):
                await attested_runs(RESOLVE_RUN, identifiers, token, transport=transport)

    async def test_a_refused_attestation_never_hides_the_located_predecessor(self):
        current, outer = BLOCKED_RUN, self
        artifact = {
            "id": 99, "name": artifact_name(LOST), "expired": False, "size_in_bytes": 2000,
            "workflow_run": {
                "id": LOST.run_id, "repository_id": RUN.repository_id,
                "head_repository_id": RUN.repository_id, "head_branch": "main", "head_sha": LOST.sha,
            },
        }

        class GitHub:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def request(self, method, url, **_):
                path = urlsplit(url).path
                if path.endswith("/application-canaries.yml"):
                    return response({"id": 123, "path": ".github/workflows/application-canaries.yml"})
                if path.endswith(f"/runs/{current.run_id}"):
                    return response(run_metadata(current))
                if path.endswith("/123/runs"):
                    return response({"workflow_runs": [run_metadata(current), run_metadata(LOST)]})
                if path.endswith(f"/runs/{LOST.run_id}/artifacts"):
                    return response({"total_count": 1, "artifacts": [artifact]})
                if path.endswith(f"/runs/{LOST.run_id}"):
                    return response(outer.metadata)
                raise AssertionError(path)

        record = record_text(blocked_run_id=250)
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as temporary:
                # The inventory row stays valid; only the attested read differs.
                self.metadata = run_metadata(LOST) if valid else {**run_metadata(LOST), "run_attempt": 2}
                env = {**environment(current), "GH_TOKEN": "repo-token",
                       "CANARY_OPERATION": "resolve", "AI4IA_CANARY_RESOLUTION": record}
                with patch("scripts.canaries.transport.Transport", return_value=GitHub()):
                    self.assertEqual(await cli.locate_command(Path(temporary), env), 0)
                written = cli.read_json(Path(temporary) / "history.json")
                self.assertEqual(written["code"], "ok")
                self.assertEqual(written["predecessor"]["run"], asdict(LOST))
                self.assertEqual(written["resolution"]["code"], "ok" if valid else "resolution_invalid")
                self.assertEqual(len(written["resolution"]["runs"]), int(valid))


if __name__ == "__main__":
    unittest.main()
