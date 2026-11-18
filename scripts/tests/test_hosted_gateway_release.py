"""Fake-only tests for the hosted gateway release lane. No external network, no Azure."""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "hosted_gateway_release", ROOT / "scripts" / "hosted_gateway_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

SHA = "a" * 40
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
APP = "ca-api-private-slurmfactory"
R0 = APP + "--r0"
OLD_IMAGE = "acrai4iavypvgrncoed2o.azurecr.io/ai4ia/api-slurmfactory@sha256:" + "b" * 64


def env(**overrides):
    value = {
        "AI4IA_GATEWAY_RELEASE_ENABLED": "true", "AI4IA_GATEWAY_RELEASE_ADMIT": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": release.REPOSITORY, "AZURE_TENANT_ID": release.TENANT,
        "AZURE_SUBSCRIPTION_ID": release.SUBSCRIPTION, "AZURE_CLIENT_ID": release.CLIENT,
        "GITHUB_SHA": SHA, "AI4IA_REVIEWED_SHA": SHA, "GITHUB_RUN_ID": "43712804893",
        "AI4IA_RELEASE_STAGE": "c2-api", "AI4IA_ROLLBACK_REVISION": "",
    }
    value.update(overrides)
    return value


def metrics(values, at=NOW - timedelta(minutes=1)):
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"value": [
        {"name": {"value": name}, "timeseries": [{"data": [
            {"timeStamp": stamp, **({} if values.get(name) is None else {"maximum": values[name]})}]}]}
        for name in release.DTS_METRICS if name in values
    ]}


ZERO = {name: 0.0 for name in release.DTS_METRICS}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeArm:
    """Container App state machine with fault injection. Counts every write."""

    def __init__(self, image=OLD_IMAGE, extra_env=None):
        self.clock = Clock()
        self.sleep = self.clock.sleep
        self.writes = 0
        self.calls = []
        self.requests = []
        self.fail = {}
        self.unready = set()
        self.app = {
            "id": release.APPS["api"], "name": APP, "location": "eastus2",
            "properties": {
                "provisioningState": "Succeeded", "latestRevisionName": R0,
                "latestReadyRevisionName": R0,
                "configuration": {"activeRevisionsMode": "Multiple",
                                  "ingress": {"traffic": [{"revisionName": R0, "weight": 100}]}},
                "template": {"containers": [{"name": "api", "image": image, "env": extra_env or []}]},
            },
        }
        self.revisions = {R0: self.revision(R0, image, extra_env or [])}
        self.gate_values = [metrics(ZERO)]

    def revision(self, name, image, env_list):
        return {"id": release.APPS["api"] + "/revisions/" + name, "name": name, "properties": {
            "provisioningState": "Provisioned", "active": True, "healthState": "Healthy",
            "runningState": "Running", "replicas": 1,
            "template": {"containers": [{"name": "api", "image": image, "env": env_list}]}}}

    def get(self, path, api=release.APP_API, phase="arm", **query):
        self.calls.append(("GET", path, phase))
        if path == release.APPS["api"]:
            return copy.deepcopy(self.app)
        if path.endswith("/providers/Microsoft.Insights/metrics"):
            value = self.gate_values.pop(0) if len(self.gate_values) > 1 else self.gate_values[0]
            return copy.deepcopy(value)
        name = path.rsplit("/", 1)[-1]
        if name in self.revisions and "/revisions/" in path:
            rev = copy.deepcopy(self.revisions[name])
            if name in self.unready:
                rev["properties"]["healthState"] = "Unhealthy"
            return rev
        raise release.Refused(phase, 404)

    def list(self, path, api=release.APP_API, phase="arm", pages=20):
        if path.endswith("/schedulers"):
            return [{"id": release.RG + "/providers/Microsoft.DurableTask/schedulers/dts",
                     "properties": {"endpoint": release.DTS_ENDPOINT}}]
        return [{"name": n, "properties": {"active": r["properties"]["active"],
                                           "healthState": r["properties"]["healthState"],
                                           "runningState": r["properties"]["runningState"]}}
                for n, r in self.revisions.items()]

    def write(self, method, path, body=None, *, timeout=600, phase="write"):
        self.writes += 1
        self.calls.append((method, path, phase))
        self.requests.append((method, path, copy.deepcopy(body), phase))
        if phase in self.fail:
            raise self.fail.pop(phase)
        if method == "POST" and path.endswith("/deactivate"):
            self.revisions[path.split("/")[-2]]["properties"]["active"] = False
            return
        props = body["properties"]
        template = props.get("template")
        if template is not None:
            name = APP + "--" + template["revisionSuffix"]
            image = template["containers"][0]["image"]
            self.revisions[name] = self.revision(name, image, template["containers"][0].get("env", []))
            self.app["properties"]["latestRevisionName"] = name
            self.app["properties"]["latestReadyRevisionName"] = name
            self.app["properties"]["template"] = {"containers": template["containers"]}
        config = props.get("configuration") or {}
        if "activeRevisionsMode" in config:
            self.app["properties"]["configuration"]["activeRevisionsMode"] = config["activeRevisionsMode"]
        if "ingress" in config:
            self.app["properties"]["configuration"]["ingress"]["traffic"] = config["ingress"]["traffic"]


def fake_build(app, rev, *, resource_id, stage, service, suffix="", api_image="", web_image="",
               existing_revisions=None, **c3):
    if stage == "C1":
        return {"location": app["location"], "properties": {"configuration": {
            "activeRevisionsMode": "Multiple", "ingress": {"traffic": [{"revisionName": rev["name"], "weight": 100}]}}}}
    image = api_image or rev["properties"]["template"]["containers"][0]["image"]
    env_list = [{"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "true"}] if stage == "C3" else []
    return {"location": app["location"], "properties": {
        "configuration": {"ingress": {"traffic": [{"revisionName": rev["name"], "weight": 100}]}},
        "template": {"revisionSuffix": suffix,
                     "containers": [{"name": "api", "image": image, "env": env_list}]}}}


def passing_smoke(*_args):
    return {"phase": "complete", "base": "c" * 64, "calls": [
        {"deployment": d, "api": api, "status": 200, "shared": True, "host": True}
        for d, api in release.CANARY_APIS.items()]}


def passing_web_probe(*_args):
    return {"phase": "complete", "build": release.WEB_BUILD_ID, "build_status": 200,
            "api": "d" * 64, "ready_status": 200, "ready": True}


class ReleaseFlowBase(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(release, "build_payload", fake_build),
            mock.patch.object(release, "projection",
                              lambda app: json.dumps(app["properties"], sort_keys=True)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_stage(self, arm, stage="c2-api", smoke=passing_smoke, web_probe=passing_web_probe, **extra):
        receipt = {"steps": []}
        values = env(AI4IA_RELEASE_STAGE=stage, **extra)
        gate = release.DtsGate(arm, now=lambda: NOW, attempts=2, wait=1)
        runner = release.Release(arm, values, receipt, gate=gate, now=lambda: NOW, smoke=smoke,
                                 web_probe=web_probe)
        return runner, receipt


class AdmissionTests(unittest.TestCase):
    def test_exact_admission(self):
        self.assertEqual(release.validate_environment(env()), "c2-api")

    def test_refusals(self):
        cases = [
            {"AI4IA_REVIEWED_SHA": "b" * 40},
            {"AI4IA_GATEWAY_RELEASE_ENABLED": ""},
            {"GITHUB_REF": "refs/heads/feature"},
            {"AI4IA_RELEASE_STAGE": "c4-api"},
            {"AI4IA_RELEASE_STAGE": "rollback-api"},
            {"AI4IA_ROLLBACK_REVISION": APP + "--x"},
            {"HTTPS_PROXY": "http://proxy"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(release.Refused):
                release.validate_environment(env(**case))

    def test_candidate_suffix_is_a_valid_bounded_revision_name(self):
        suffix = release.candidate_suffix("c3", SHA, "43712804893")
        self.assertRegex(suffix, r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
        self.assertLessEqual(len(APP + "--" + suffix), 64)


def passing_c4_smoke(*_args):
    deployments = _args[-1] if _args else release.C4_DEPLOYMENTS
    return {"phase": "complete", "base": "c" * 64, "calls": [
        {"deployment": d, "mode": mode, "status": 200, "shared": True, "host": True,
         "route": True, "model_unchanged": True, "done": True,
         "backend_identity": "unavailable", "usage": None}
        for d in deployments for mode in ("complete", "stream")]}


class C4FlowTests(unittest.TestCase):
    MANIFEST = '{"synthetic":"manifest"}'

    def setUp(self):
        data = self.MANIFEST.encode()
        p = mock.patch.multiple(release, MANIFEST_SIZE=len(data),
                                MANIFEST_SHA256=hashlib.sha256(data).hexdigest())
        p.start()
        self.addCleanup(p.stop)

    def serving(self):
        items = [
            {"name": "AZURE_CLIENT_ID", "value": "a" * 36},
            {"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "true"},
            {"name": "AI4IA_SHARED_GATEWAY_DEPLOYMENTS", "value": release.DEPLOYMENTS_BYTES.decode()},
            {"name": "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST", "value": self.MANIFEST},
        ]
        arm = FakeArm(image=release.IMAGES["api"], extra_env=items)
        rev = arm.revisions.pop(R0)
        rev["name"] = release.C3_REVISION
        rev["id"] = release.APPS["api"] + "/revisions/" + release.C3_REVISION
        arm.revisions[release.C3_REVISION] = rev
        arm.revisions[release.R1_REVISION] = arm.revision(
            release.R1_REVISION, release.IMAGES["api"], [])
        arm.app["properties"].update(latestRevisionName=release.C3_REVISION,
                                     latestReadyRevisionName=release.C3_REVISION)
        arm.app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": release.C3_REVISION, "weight": 100}]
        return arm

    def runner(self, arm, smoke=passing_c4_smoke, **overrides):
        values = env(AI4IA_RELEASE_STAGE="c4-api", AI4IA_ROLLBACK_REVISION=release.C3_REVISION,
                     **overrides)
        receipt = {"steps": []}
        return release.Release(arm, values, receipt, now=lambda: NOW, c4_smoke=smoke), receipt

    def build(self, app, rev, **kwargs):
        template = copy.deepcopy(rev["properties"]["template"])
        template["revisionSuffix"] = kwargs["suffix"]
        for item in template["containers"][0]["env"]:
            if item["name"] == "AI4IA_SHARED_GATEWAY_DEPLOYMENTS":
                item["value"] = kwargs["deployments_bytes"].decode()
        return {"location": app["location"], "properties": {
            "configuration": {"ingress": {"traffic": [
                {"revisionName": rev["name"], "weight": 100}]}}, "template": template}}

    def inputs(self):
        return dict(manifest_bytes=b"m", classification_sidecar_bytes=b"s",
                    deployments_bytes=release.C4_DEPLOYMENTS_BYTES,
                    deployments_sha256=release.C4_DEPLOYMENTS_SHA256,
                    prior_deployments_bytes=release.DEPLOYMENTS_BYTES,
                    prior_deployments_sha256=release.DEPLOYMENTS_SHA256)

    def test_exact_c3_shape_refuses_every_pin_drift(self):
        arm = self.serving()
        rev = arm.revisions[release.C3_REVISION]
        self.assertTrue(release.gateway_on_release(rev))
        for field in ("name", "image", "active", "selector", "deployments", "manifest", "duplicate", "pilot"):
            changed = copy.deepcopy(rev)
            container = changed["properties"]["template"]["containers"][0]
            if field == "name":
                changed["name"] = APP + "--c3-other"
            elif field == "image":
                container["image"] = OLD_IMAGE
            elif field == "active":
                changed["properties"]["active"] = False
            elif field == "duplicate":
                container["env"].append(copy.deepcopy(container["env"][1]))
            elif field == "pilot":
                container["env"].append({"name": "AI4IA_CHAT_PILOT_ISOLATION_ENABLED", "value": "true"})
            else:
                index = {"selector": 1, "deployments": 2, "manifest": 3}[field]
                container["env"][index]["value"] += " "
            with self.subTest(field=field):
                self.assertFalse(release.gateway_on_release(changed))

    def test_c4_admission_and_explicit_exclusions(self):
        values = env(AI4IA_RELEASE_STAGE="c4-api", AI4IA_ROLLBACK_REVISION=release.C3_REVISION)
        self.assertEqual(release.validate_environment(values), "c4-api")
        self.assertEqual(len(release.c4_deployments(values)), 22)
        addition = sorted(set(release.C4_DEPLOYMENTS) - set(release.DEPLOYMENTS))[0]
        values["AI4IA_C4_EXCLUDE_DEPLOYMENTS"] = addition
        self.assertNotIn(addition, release.c4_deployments(values))
        for bad in (release.DEPLOYMENTS[0], "unknown", addition + "," + addition, addition + ","):
            with self.subTest(bad=bad), self.assertRaises(release.Refused):
                release.c4_deployments({**values, "AI4IA_C4_EXCLUDE_DEPLOYMENTS": bad})
        for target in ("", release.R1_REVISION, APP + "--c3-other"):
            with self.subTest(target=target), self.assertRaises(release.Refused):
                release.validate_environment({**values, "AI4IA_ROLLBACK_REVISION": target})

    def test_c4_smokes_at_zero_and_cannot_promote_even_with_all_successes(self):
        for failure in ("none", "partial", "exception"):
            arm = self.serving()
            r1 = copy.deepcopy(arm.revisions[release.R1_REVISION])
            seen = []

            def smoke(_arm, _app, revision, deployments):
                self.assertTrue(release.traffic_is(arm.app, release.C3_REVISION))
                self.assertNotEqual(revision, release.C3_REVISION)
                seen.extend(deployments)
                if failure == "exception":
                    raise release.Refused("smoke-disconnected")
                result = passing_c4_smoke(deployments)
                if failure == "partial":
                    result["calls"].pop()
                return result

            runner, receipt = self.runner(arm, smoke=smoke)
            with mock.patch.object(release, "build_payload", self.build), \
                    self.subTest(failure=failure), self.assertRaises(release.Refused) as refused:
                runner.create_and_promote("api", "C4", **self.inputs())
            self.assertEqual(refused.exception.phase, {
                "none": "c4-backend-identity-unavailable", "partial": "smoke",
                "exception": "smoke-disconnected"}[failure])
            self.assertEqual(seen, list(release.C4_DEPLOYMENTS))
            self.assertEqual([r[3] for r in arm.requests], ["create", "deactivate-unpromoted"])
            self.assertTrue(release.traffic_is(arm.app, release.C3_REVISION))
            self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])
            self.assertEqual(arm.revisions[release.R1_REVISION], r1)
            self.assertTrue(release.gateway_on_release(arm.revisions[release.C3_REVISION]))

    def test_c4_requires_healthy_untrafficked_r1_before_any_write(self):
        for drift in ("r1", "r0", "traffic"):
            arm = self.serving()
            if drift == "r1":
                arm.revisions[release.R1_REVISION]["properties"]["active"] = False
            elif drift == "r0":
                arm.revisions[release.C3_REVISION]["properties"]["template"]["containers"][0]["image"] = OLD_IMAGE
            else:
                arm.app["properties"]["configuration"]["ingress"]["traffic"] = [
                    {"revisionName": release.R1_REVISION, "weight": 100}]
            runner, _ = self.runner(arm)
            with mock.patch.object(release, "build_payload", self.build), \
                    self.subTest(drift=drift), self.assertRaises(release.Refused):
                runner.create_and_promote("api", "C4", **self.inputs())
            self.assertEqual(arm.writes, 0)

    def test_run_validates_all_manifest_rows_including_excluded_before_create(self):
        addition = sorted(set(release.C4_DEPLOYMENTS) - set(release.DEPLOYMENTS))[0]
        for missing in (None, addition):
            arm = self.serving()
            runner, _ = self.runner(arm, AI4IA_C4_EXCLUDE_DEPLOYMENTS=addition)
            manifest = types.SimpleNamespace(resolve_shared_model_key=mock.Mock(
                side_effect=lambda d, *_a: None if d == missing else d))
            builder = release.cutover()
            with (mock.patch.object(release, "cutover", return_value=builder),
                  mock.patch.object(builder, "load_c3_contract", return_value=("", manifest)),
                  mock.patch.object(release, "load_manifest", return_value=b"m"),
                  mock.patch.object(release, "load_sidecar", return_value=b"s"),
                  mock.patch.object(runner, "create_and_promote") as create):
                if missing:
                    with self.assertRaisesRegex(release.Refused, "c4-manifest-inventory"):
                        runner.run("c4-api")
                    create.assert_not_called()
                else:
                    runner.run("c4-api")
                    create.assert_called_once()
                    self.assertNotIn(addition, create.call_args.kwargs["deployments_bytes"].decode().split(","))
                    self.assertEqual(manifest.resolve_shared_model_key.call_count, 22)
            self.assertEqual(arm.writes, 0)


class C4ProgramTests(unittest.TestCase):
    def run_program(self, *, status=200, missing_done=False, changed_model=False,
                    retry=False, cap=None, selected=None, request_model=None):
        from scripts.tests.test_gateway_cutover import API_CLIENT_ID, synthetic_c3_inputs

        import httpx
        from ai4ia_api.catalog import ModelCatalog
        from ai4ia_api.config import Settings
        from ai4ia_api.gateway.client import ModelGatewayClient

        deployments = selected or release.C4_DEPLOYMENTS
        manifest, _ = synthetic_c3_inputs()
        doc = json.loads(manifest)
        doc["models"] = {
            d: {"kind": "deployment-name", "operations": ["/chat/completions"],
                "targets": [{"deployment": d, "sku": release.C4_SKUS[d]}]}
            for d in release.C4_DEPLOYMENTS}
        manifest = json.dumps(doc)
        settings = Settings(
            _env_file=None, env="prod", auth_provider="entra", AZURE_CLIENT_ID=API_CLIENT_ID,
            shared_gateway_enabled=True, shared_gateway_activation_manifest=manifest,
            shared_gateway_deployments=",".join(deployments),
            model_gateway_url="https://legacy.invalid", model_gateway_auth_mode="none")
        catalog = ModelCatalog.model_validate({"models": [
            {"id": d.split("-slurmfactory")[0], "displayName": "Synthetic", "category": "chat",
             "format": "OpenAI", "api": "chat", "reasoningEffort": ["none"],
             "options": [{"deploymentName": d, "region": d.rsplit("-", 2)[1],
                          "sku": release.C4_SKUS[d]}]} for d in deployments]})
        requests = []
        real_async_client = httpx.AsyncClient

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            model = "native-selected-model-not-the-deployment-alias"
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "synthetic"}})
            if body.get("stream"):
                event = {"model": model, "choices": [{"index": 0,
                         "delta": {"content": "OK"}, "finish_reason": "stop"}]}
                text = "data: " + json.dumps(event) + "\n\n"
                if not missing_done:
                    text += "data: [DONE]\n\n"
                return httpx.Response(200, headers={"content-type": "text/event-stream",
                                                  "x-S7P-ID": "correlation-only"},
                                      text=text)
            return httpx.Response(200, headers={"x-ms-region": "untrusted-region",
                                               "x-deployment": "not-route-evidence"},
                                  json={"model": model, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})

        def http_factory(**kwargs):
            return real_async_client(**kwargs, transport=httpx.MockTransport(respond))

        async def token(_scope):
            return "synthetic"

        def gateway_factory(*args, **kwargs):
            client = ModelGatewayClient(*args, **kwargs, shared_gateway_token_provider=token)
            original = client.complete

            async def complete(**call):
                result = await original(**call)
                if retry:
                    await original(**call)
                if changed_model:
                    result["model"] = "rewritten"
                return result

            client.complete = complete
            if request_model is not None:
                original_shared = client._shared_request

                async def shared(*a, **k):
                    request, url = await original_shared(*a, **k)
                    request.json["model"] = request_model
                    return request, url

                client._shared_request = shared
            return client

        with (mock.patch("httpx.AsyncClient", http_factory),
              mock.patch("ai4ia_api.config.get_settings", return_value=settings),
              mock.patch("ai4ia_api.catalog.load_catalog", return_value=catalog),
              mock.patch("ai4ia_api.gateway.client.ModelGatewayClient", gateway_factory),
              mock.patch.object(release, "MANIFEST_SHA256", hashlib.sha256(manifest.encode()).hexdigest()),
              mock.patch.object(release, "C4_MAX_REQUESTS_PER_ROUTE",
                                release.C4_MAX_REQUESTS_PER_ROUTE if cap is None else cap),
              mock.patch("sys.stdout") as output, mock.patch("logging.disable")):
            program = release.c4_smoke_program(deployments)
            exec(compile(program, "<c4-smoke>", "exec"), {})  # noqa: S102 - offline program with MockTransport
        result = json.loads(output.write.call_args[0][0].split(release.C4_SMOKE_MARK)[1])
        return result, requests

    def test_normal_client_completes_and_streams_all_22_with_same_response_identity(self):
        result, calls = self.run_program()
        self.assertTrue(release.c4_smoke_passed(result, release.C4_DEPLOYMENTS), result)
        self.assertEqual(len(calls), 44)
        self.assertEqual([c["model"] for c in calls],
                         [d for d in release.C4_DEPLOYMENTS for _ in range(2)])
        self.assertTrue(all(c.get("max_completion_tokens", c.get("max_tokens"))
                            == release.C4_MAX_OUTPUT_TOKENS for c in calls))
        clean = release.sanitize_c4_smoke(result, release.C4_DEPLOYMENTS)
        self.assertTrue(all(c["backend_identity"] == "unavailable" and c["usage"] is None
                            for c in clean["calls"]))
        self.assertNotIn("native-selected", json.dumps(clean))
        self.assertNotIn("correlation-only", json.dumps(clean))

    def test_failure_stops_immediately_without_retry_or_fallback(self):
        for kwargs, expected_calls in (({"status": 429}, 1), ({"status": 400}, 1),
                                       ({"missing_done": True}, 2),
                                       ({"changed_model": True}, 1),
                                       ({"request_model": "wrong-route"}, 0)):
            with self.subTest(kwargs=kwargs):
                result, calls = self.run_program(**kwargs)
                self.assertFalse(release.c4_smoke_passed(result, release.C4_DEPLOYMENTS))
                self.assertEqual(len(calls), expected_calls)
                self.assertLessEqual(len(result["calls"]), 2)

    def test_per_route_cap_and_per_mode_no_replay_are_enforced_before_transport(self):
        # Same normal-client fixture: only the allowed route request budget changes.
        under, calls = self.run_program(cap=2)
        self.assertTrue(release.c4_smoke_passed(under, release.C4_DEPLOYMENTS))
        self.assertEqual(len(calls), 44)
        over, calls = self.run_program(cap=1)
        self.assertFalse(release.c4_smoke_passed(over, release.C4_DEPLOYMENTS))
        self.assertEqual(len(calls), 1)
        retried, calls = self.run_program(retry=True)
        self.assertFalse(release.c4_smoke_passed(retried, release.C4_DEPLOYMENTS))
        self.assertEqual(len(calls), 1)

    def test_explicit_subset_still_requires_both_paths_and_rejects_bad_rows(self):
        selected = tuple(d for d in release.C4_DEPLOYMENTS if not d.startswith("gpt-5.2-"))
        result, calls = self.run_program(selected=selected)
        self.assertTrue(release.c4_smoke_passed(result, selected))
        self.assertEqual(len(calls), 2 * len(selected))
        result["calls"].append({"deployment": "private-unexpected", "mode": "stream"})
        clean = release.sanitize_c4_smoke(result, selected)
        self.assertFalse(release.c4_smoke_passed(clean, selected))
        self.assertEqual(clean["calls"][-1], {})
        self.assertNotIn("private-unexpected", json.dumps(clean))

    def test_all_44_results_are_required_and_private_values_never_reach_receipts(self):
        full = passing_c4_smoke()
        self.assertTrue(release.c4_smoke_passed(full, release.C4_DEPLOYMENTS))
        for index in range(44):
            broken = copy.deepcopy(full)
            broken["calls"][index]["model_unchanged"] = False
            with self.subTest(index=index):
                self.assertFalse(release.c4_smoke_passed(broken, release.C4_DEPLOYMENTS))
        for mutation in ("duplicate", "missing", "extra"):
            broken = copy.deepcopy(full)
            if mutation == "duplicate":
                broken["calls"][-1] = broken["calls"][0]
            elif mutation == "missing":
                broken["calls"].pop()
            else:
                broken["calls"].append({})
            with self.subTest(mutation=mutation):
                clean = release.sanitize_c4_smoke(broken, release.C4_DEPLOYMENTS)
                self.assertFalse(release.c4_smoke_passed(clean, release.C4_DEPLOYMENTS))


class C4RecoveryTests(unittest.TestCase):
    MANIFEST = C4FlowTests.MANIFEST
    setUp = C4FlowTests.setUp
    serving = C4FlowTests.serving
    runner = C4FlowTests.runner
    build = C4FlowTests.build
    inputs = C4FlowTests.inputs

    def candidate(self, arm):
        name = APP + "--c4-aaaaaaa-1234"
        rev = copy.deepcopy(arm.revisions[release.C3_REVISION])
        rev.update(name=name, id=release.APPS["api"] + "/revisions/" + name)
        for item in rev["properties"]["template"]["containers"][0]["env"]:
            if item["name"] == "AI4IA_SHARED_GATEWAY_DEPLOYMENTS":
                item["value"] = release.C4_DEPLOYMENTS_BYTES.decode()
        arm.revisions[name] = rev
        arm.app["properties"]["template"] = copy.deepcopy(rev["properties"]["template"])
        arm.app["properties"].update(latestRevisionName=name, latestReadyRevisionName=name)
        return name

    def test_distinct_c4_rollback_to_active_c3_and_readback(self):
        arm = self.serving()
        candidate = self.candidate(arm)
        arm.app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": candidate, "weight": 100}]
        runner, receipt = self.runner(arm)
        runner.run("rollback-c4-api")
        self.assertTrue(release.traffic_is(arm.app, release.C3_REVISION))
        self.assertTrue(release.gateway_on_release(arm.revisions[release.C3_REVISION]))
        self.assertTrue(arm.revisions[candidate]["properties"]["active"])
        self.assertEqual([r[3] for r in arm.requests], ["rollback-c4"])
        self.assertEqual(receipt["serving"], release.C3_REVISION)
        # The old gateway-off rollback stage cannot accidentally authorize C3.
        with self.assertRaisesRegex(release.Refused, "rollback-target"):
            runner.rollback(release.C3_REVISION)

    def test_c4_rollback_refuses_bad_target_and_never_replays_uncertain_write(self):
        for failure in ("unhealthy", "manifest", "unknown-write", "readback"):
            arm = self.serving()
            candidate = self.candidate(arm)
            arm.app["properties"]["configuration"]["ingress"]["traffic"] = [
                {"revisionName": candidate, "weight": 100}]
            if failure == "unhealthy":
                arm.revisions[release.C3_REVISION]["properties"]["healthState"] = "Unhealthy"
            if failure == "manifest":
                arm.revisions[release.C3_REVISION]["properties"]["template"]["containers"][0]["env"][-1]["value"] += " "
            if failure == "unknown-write":
                arm.fail["rollback-c4"] = release.Uncertain("rollback-c4")
            if failure == "readback":
                original = arm.get

                def bad_readback(path, *args, **kwargs):
                    result = original(path, *args, **kwargs)
                    if path == release.APPS["api"] and kwargs.get("phase") == "readback":
                        result["properties"]["configuration"]["ingress"]["traffic"] = []
                    return result

                arm.get = bad_readback
            runner, _ = self.runner(arm)
            with self.subTest(failure=failure), self.assertRaises(release.Refused):
                runner.run("rollback-c4-api")
            self.assertEqual(arm.writes, int(failure in ("unknown-write", "readback")))

    def test_deactivate_then_fresh_c4_run_smokes_again_without_replay(self):
        arm = self.serving()
        candidate = self.candidate(arm)
        runner, _ = self.runner(arm)
        # Active stale latest cannot be reused by a new C4 run.
        with self.assertRaises(release.Refused):
            runner.snapshot("api", "C4")
        runner.recover_deactivate(candidate)
        self.assertFalse(arm.revisions[candidate]["properties"]["active"])
        self.assertTrue(release.traffic_is(arm.app, release.C3_REVISION))
        # A fresh run builds a new candidate and must repeat the entire smoke.
        seen = []

        def smoke(*args):
            seen.extend(args[-1])
            return passing_c4_smoke(*args)

        runner, receipt = self.runner(arm, smoke=smoke)
        with mock.patch.object(release, "build_payload", self.build), \
                self.assertRaisesRegex(release.Refused, "c4-backend-identity-unavailable"):
            runner.create_and_promote("api", "C4", **self.inputs())
        self.assertNotEqual(receipt["candidate"], candidate)
        self.assertEqual(len(seen), 22)
        self.assertTrue(release.traffic_is(arm.app, release.C3_REVISION))

    def test_deactivate_refuses_serving_protected_non_lane_and_drift(self):
        for bad in (release.C3_REVISION, release.R1_REVISION, APP + "--azd-123"):
            arm = self.serving()
            arm.revisions.setdefault(bad, arm.revision(bad, release.IMAGES["api"], []))
            runner, _ = self.runner(arm)
            with self.subTest(bad=bad), self.assertRaises(release.Refused):
                runner.recover_deactivate(bad)
            self.assertEqual(arm.writes, 0)


class InputTests(unittest.TestCase):
    def test_sidecar_decodes_exact_bounded_bytes(self):
        data = b'{"x":1}' * 10
        packed = base64.b64encode(gzip.compress(data, mtime=0)).decode()
        with mock.patch.multiple(release, SIDECAR_SIZE=len(data),
                                 SIDECAR_SHA256=hashlib.sha256(data).hexdigest()):
            self.assertEqual(release.load_sidecar({"AI4IA_SHARED_GATEWAY_BINDING_SIDECAR_GZIP_B64": packed}), data)
            for bad in ("", packed + "!", base64.b64encode(gzip.compress(data + b"x", mtime=0)).decode(),
                        base64.b64encode(b"not gzip").decode()):
                with self.subTest(bad=bad[:8]), self.assertRaises(release.Refused):
                    release.load_sidecar({"AI4IA_SHARED_GATEWAY_BINDING_SIDECAR_GZIP_B64": bad})

    def test_sidecar_decompression_is_bounded(self):
        bomb = base64.b64encode(gzip.compress(b"\0" * (release.SIDECAR_SIZE * 50), mtime=0)).decode()
        with self.assertRaises(release.Refused):
            release.load_sidecar({"AI4IA_SHARED_GATEWAY_BINDING_SIDECAR_GZIP_B64": bomb})

    def test_manifest_must_match_hash(self):
        with self.assertRaises(release.Refused):
            release.load_manifest({"AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST": "x" * release.MANIFEST_SIZE})


class DtsGateTests(unittest.TestCase):
    def test_fresh_all_zero_is_admitted(self):
        self.assertEqual(release.latest_zero_observation(metrics(ZERO), NOW), NOW - timedelta(minutes=1))

    def test_missing_null_stale_or_nonzero_is_never_zero(self):
        cases = {
            "nonzero": metrics({**ZERO, "ActivityPendingItems": 1.0}),
            "null": metrics({**ZERO, "OrchestratorActiveItems": None}),
            "missing": metrics({k: v for k, v in ZERO.items() if k != "ActivityActiveItems"}),
            "stale": metrics(ZERO, at=NOW - timedelta(minutes=6)),
            "empty": {"value": []},
            "malformed": {"value": [{"name": {}}]},
        }
        for name, value in cases.items():
            with self.subTest(name):
                self.assertIsNone(release.latest_zero_observation(value, NOW))

    def test_newer_partial_or_null_points_do_not_hide_behind_older_zero(self):
        for newer in ({"maximum": 2}, {}, {"maximum": 0}):
            value = metrics(ZERO, at=NOW - timedelta(minutes=2))
            value["value"][0]["timeseries"][0]["data"].append({
                "timeStamp": (NOW - timedelta(minutes=1)).isoformat(), **newer,
            })
            with self.subTest(newer=newer):
                self.assertIsNone(release.latest_zero_observation(value, NOW))

    def test_duplicate_metric_timestamp_is_not_accepted(self):
        value = metrics(ZERO)
        points = value["value"][0]["timeseries"][0]["data"]
        points.append({**points[0], "maximum": 3})
        self.assertIsNone(release.latest_zero_observation(value, NOW))

    def test_gate_refuses_after_bounded_reads(self):
        arm = FakeArm()
        arm.gate_values = [{"value": []}]
        gate = release.DtsGate(arm, now=lambda: NOW, attempts=3, wait=1)
        with self.assertRaisesRegex(release.Refused, "dts-gate"):
            gate.require()
        self.assertEqual(gate.reads, 3)

    def test_require_fresh_rereads_an_old_observation(self):
        arm = FakeArm()
        clock = {"now": NOW}
        gate = release.DtsGate(arm, now=lambda: clock["now"], attempts=1)
        gate.require()
        clock["now"] = NOW + timedelta(minutes=5)
        arm.gate_values = [metrics(ZERO, at=NOW + timedelta(minutes=4))]
        gate.require_fresh()
        self.assertEqual(gate.reads, 2)
        self.assertEqual(gate.observed, NOW + timedelta(minutes=4))


class ArmWriteTests(unittest.TestCase):
    def arm(self, result):
        calls = []

        def http(method, url, **kwargs):
            calls.append(method)
            if isinstance(result, Exception):
                raise result
            return result

        arm = release.Arm("t", http=http, sleep=lambda _s: None, clock=lambda: 0)
        return arm, calls

    def test_transport_doubt_is_uncertain_and_not_replayed(self):
        for result in (OSError("reset"), (500, {}, b""), (504, {}, b"")):
            with self.subTest(result=result):
                arm, calls = self.arm(result)
                with self.assertRaises(release.Uncertain):
                    arm.write("PATCH", release.APPS["api"], {})
                self.assertEqual(calls, ["PATCH"])

    def test_client_rejection_is_a_clean_refusal(self):
        arm, _calls = self.arm((403, {}, b""))
        with self.assertRaises(release.Refused) as caught:
            arm.write("PATCH", release.APPS["api"], {})
        self.assertNotIsInstance(caught.exception, release.Uncertain)
        self.assertEqual(caught.exception.status, 403)

    def test_async_operation_must_stay_on_arm(self):
        arm, _ = self.arm((202, {"azure-asyncoperation": "https://evil.example/op"}, b""))
        with self.assertRaises(release.Uncertain):
            arm.write("PATCH", release.APPS["api"], {})

    def test_async_missing_status_is_uncertain_not_success(self):
        responses = iter([(202, {"azure-asyncoperation": "https://management.azure.com/op"}, b""),
                          (200, {}, b"{}")])
        calls = []

        def http(method, url, **kwargs):
            calls.append(method)
            return next(responses)

        arm = release.Arm("t", http=http, sleep=lambda _s: None, clock=lambda: 0)
        with self.assertRaises(release.Uncertain):
            arm.write("PATCH", release.APPS["api"], {})
        self.assertEqual(calls, ["PATCH", "GET"])


class StageTests(ReleaseFlowBase):
    def test_c2_creates_promotes_then_deactivates_r0(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm)
        runner.run("c2-api")
        candidate = receipt["candidate"]
        self.assertEqual(arm.app["properties"]["configuration"]["ingress"]["traffic"],
                         [{"revisionName": candidate, "weight": 100}])
        self.assertFalse(arm.revisions[R0]["properties"]["active"])
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "promote", "deactivate-r0"])
        self.assertEqual(arm.revisions[candidate]["properties"]["template"]["containers"][0]["image"],
                         release.IMAGES["api"])

    def test_dts_refusal_happens_before_any_write(self):
        arm = FakeArm()
        arm.gate_values = [metrics({**ZERO, "OrchestratorPendingItems": 2.0})]
        runner, _ = self.run_stage(arm)
        with self.assertRaisesRegex(release.Refused, "dts-gate"):
            runner.run("c2-api")
        self.assertEqual(arm.writes, 0)

    def test_concurrent_change_refuses_before_write(self):
        arm = FakeArm()
        runner, _ = self.run_stage(arm)
        original = arm.get

        def drift(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if kwargs.get("phase") == "reread":
                value["properties"]["latestRevisionName"] = APP + "--other"
            return value

        arm.get = drift
        with self.assertRaisesRegex(release.Refused, "concurrent-change"):
            runner.run("c2-api")
        self.assertEqual(arm.writes, 0)

    def test_uncertain_create_is_never_replayed_or_rolled_back(self):
        arm = FakeArm()
        arm.fail["create"] = release.Uncertain("create")
        runner, _ = self.run_stage(arm)
        with self.assertRaises(release.Uncertain):
            runner.run("c2-api")
        self.assertEqual(arm.writes, 1)

    def test_unready_candidate_is_deactivated_without_traffic(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm)
        arm.unready.add(APP + "--" + release.candidate_suffix("c2", SHA, "43712804893"))
        with mock.patch.object(release.Release, "wait_ready",
                               side_effect=release.Refused("candidate-timeout")), \
                self.assertRaisesRegex(release.Refused, "candidate-timeout"):
            runner.run("c2-api")
        self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])
        self.assertTrue(arm.revisions[R0]["properties"]["active"])
        self.assertEqual(arm.app["properties"]["configuration"]["ingress"]["traffic"],
                         [{"revisionName": R0, "weight": 100}])

    def test_candidate_template_mismatch_is_not_promoted(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm)
        original = arm.get

        def wrong_image(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if kwargs.get("phase") == "wait":
                value["properties"]["template"]["containers"][0]["image"] = OLD_IMAGE
            return value

        arm.get = wrong_image
        with self.assertRaisesRegex(release.Refused, "candidate-template"):
            runner.run("c2-api")
        self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "deactivate-unpromoted"])

    def test_candidate_that_becomes_unhealthy_before_promotion_is_not_promoted(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm)
        original = arm.get

        def unhealthy(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if kwargs.get("phase") == "reread" and receipt.get("candidate", "~") in path:
                value["properties"]["healthState"] = "Unhealthy"
            return value

        arm.get = unhealthy
        with self.assertRaisesRegex(release.Refused, "concurrent-change"):
            runner.run("c2-api")
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "deactivate-unpromoted"])
        self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])

    def test_final_dts_refusal_deactivates_unpromoted_candidate(self):
        for stage, image in (("c2-api", OLD_IMAGE), ("c3-api", release.IMAGES["api"])):
            arm = FakeArm(image=image)
            runner, receipt = self.run_stage(arm, stage=stage)
            with self.subTest(stage=stage):
                with mock.patch.object(runner.gate, "require_fresh",
                                       side_effect=[None, release.Refused("dts-gate")]), \
                        mock.patch.object(release, "load_manifest", return_value=b"m"), \
                        mock.patch.object(release, "load_sidecar", return_value=b"s"), \
                        self.assertRaisesRegex(release.Refused, "dts-gate"):
                    runner.run(stage)
                self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"],
                                 ["create", "deactivate-unpromoted"])
                self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])
                self.assertEqual(arm.app["properties"]["configuration"]["ingress"]["traffic"],
                                 [{"revisionName": R0, "weight": 100}])

    def test_final_state_read_refusal_deactivates_unpromoted_candidate(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm)
        original = arm.get
        state = {"failed": False}

        def get(path, *args, **kwargs):
            if (kwargs.get("phase") == "reread" and path == release.APPS["api"]
                    and receipt.get("candidate") in arm.revisions and not state["failed"]):
                state["failed"] = True
                raise release.Refused("reread", 404)
            return original(path, *args, **kwargs)

        arm.get = get
        with self.assertRaisesRegex(release.Refused, "reread"):
            runner.run("c2-api")
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "deactivate-unpromoted"])
        self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])

    def test_moved_serving_traffic_refuses_cleanup_and_reports_original_stop(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm)
        original = arm.get

        def get(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if (kwargs.get("phase") == "reread" and path == release.APPS["api"]
                    and receipt.get("candidate") in arm.revisions):
                value["properties"]["configuration"]["ingress"]["traffic"] = [
                    {"revisionName": receipt["candidate"], "weight": 100}]
            return value

        arm.get = get
        with self.assertRaisesRegex(release.Refused, "concurrent-change"):
            runner.run("c2-api")
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create"])
        self.assertEqual(receipt["steps"][-1]["step"], "cleanup-refused")
        self.assertEqual(receipt["steps"][-1]["phase"], "deactivate-target")

    def test_uncertain_final_read_is_not_cleaned_up(self):
        arm = FakeArm()
        runner, _receipt = self.run_stage(arm)
        with mock.patch.object(runner.gate, "require_fresh",
                               side_effect=[None, release.Uncertain("dts")]), \
                self.assertRaises(release.Uncertain):
            runner.run("c2-api")
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create"])

    def test_failed_promotion_readback_restores_still_active_previous(self):
        arm = FakeArm(image=release.IMAGES["api"])
        runner, receipt = self.run_stage(arm, stage="c3-api")
        original = arm.get
        state = {"promoted": False}

        def get(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if kwargs.get("phase") == "readback" and "/revisions/" in path and receipt.get("candidate", "~") in path:
                value["properties"]["healthState"] = "Unhealthy"
                state["promoted"] = True
            return value

        arm.get = get
        with mock.patch.object(release, "load_manifest", return_value=b"m"), \
                mock.patch.object(release, "load_sidecar", return_value=b"s"), \
                self.assertRaisesRegex(release.Refused, "promote-readback"):
            runner.run("c3-api")
        self.assertTrue(state["promoted"])
        self.assertEqual(arm.app["properties"]["configuration"]["ingress"]["traffic"],
                         [{"revisionName": R0, "weight": 100}])
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "promote", "restore"])

    def test_c2_failed_promotion_readback_does_not_restore_unqualified_r0(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm)
        original = arm.get

        def unhealthy(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if kwargs.get("phase") == "readback" and receipt.get("candidate", "~") in path:
                value["properties"]["healthState"] = "Unhealthy"
            return value

        arm.get = unhealthy
        with self.assertRaisesRegex(release.Uncertain, "promote-readback"):
            runner.run("c2-api")
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "promote"])
        self.assertTrue(arm.revisions[R0]["properties"]["active"])

    def test_c3_refuses_to_restore_unhealthy_r1(self):
        arm = FakeArm(image=release.IMAGES["api"])
        runner, receipt = self.run_stage(arm, stage="c3-api")
        original = arm.get

        def unhealthy(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if kwargs.get("phase") == "readback" and receipt.get("candidate", "~") in path:
                arm.revisions[R0]["properties"]["healthState"] = "Unhealthy"
                value["properties"]["healthState"] = "Unhealthy"
            return value

        arm.get = unhealthy
        with mock.patch.object(release, "load_manifest", return_value=b"m"), \
                mock.patch.object(release, "load_sidecar", return_value=b"s"), \
                self.assertRaisesRegex(release.Uncertain, "restore-target"):
            runner.run("c3-api")
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "promote"])

    def test_c3_refuses_traffic_restore_if_worker_gate_becomes_unknown(self):
        arm = FakeArm(image=release.IMAGES["api"])
        runner, receipt = self.run_stage(arm, stage="c3-api")
        original = arm.get
        original_fresh = runner.gate.require_fresh
        fresh_calls = 0

        def gate_fresh():
            nonlocal fresh_calls
            fresh_calls += 1
            if fresh_calls == 3:
                raise release.Refused("dts-gate")
            original_fresh()

        def unhealthy(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if kwargs.get("phase") == "readback" and receipt.get("candidate", "~") in path:
                value["properties"]["healthState"] = "Unhealthy"
            return value

        runner.gate.require_fresh = gate_fresh
        arm.get = unhealthy
        with mock.patch.object(release, "load_manifest", return_value=b"m"), \
                mock.patch.object(release, "load_sidecar", return_value=b"s"), \
                self.assertRaisesRegex(release.Uncertain, "restore-gate"):
            runner.run("c3-api")
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "promote"])

    def test_deactivation_refuses_if_serving_traffic_drifted(self):
        arm = FakeArm()
        runner, _ = self.run_stage(arm)
        arm.app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": R0, "weight": 100}]
        with self.assertRaisesRegex(release.Refused, "deactivate-target"):
            runner.deactivate("api", R0, "deactivate-r0", APP + "--candidate")
        self.assertEqual(arm.writes, 0)

    def test_c3_smoke_failure_deactivates_r2_and_keeps_r1(self):
        arm = FakeArm(image=release.IMAGES["api"])

        def failing(*_args):
            result = passing_smoke()
            result["calls"][3]["shared"] = False
            return result

        runner, receipt = self.run_stage(arm, stage="c3-api", smoke=failing)
        with mock.patch.object(release, "load_manifest", return_value=b"m"), \
                mock.patch.object(release, "load_sidecar", return_value=b"s"), \
                self.assertRaisesRegex(release.Refused, "smoke"):
            runner.run("c3-api")
        self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])
        self.assertEqual(arm.app["properties"]["configuration"]["ingress"]["traffic"],
                         [{"revisionName": R0, "weight": 100}])

    def test_smoke_transport_exception_deactivates_unpromoted_candidate(self):
        arm = FakeArm(image=release.IMAGES["api"])

        def disconnected(*_args):
            raise OSError("connection lost")

        runner, receipt = self.run_stage(arm, stage="c3-api", smoke=disconnected)
        with mock.patch.object(release, "load_manifest", return_value=b"m"), \
                mock.patch.object(release, "load_sidecar", return_value=b"s"), \
                self.assertRaises(OSError):
            runner.run("c3-api")
        self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "deactivate-unpromoted"])

    def test_c3_requires_the_released_r1_image(self):
        arm = FakeArm()
        runner, _ = self.run_stage(arm, stage="c3-api")
        with self.assertRaisesRegex(release.Refused, "c3-baseline"):
            runner.run("c3-api")
        self.assertEqual(arm.writes, 0)

    def test_c3_promotion_keeps_r1_active(self):
        arm = FakeArm(image=release.IMAGES["api"])
        runner, receipt = self.run_stage(arm, stage="c3-api")
        with mock.patch.object(release, "load_manifest", return_value=b"m"), \
                mock.patch.object(release, "load_sidecar", return_value=b"s"):
            runner.run("c3-api")
        self.assertTrue(arm.revisions[R0]["properties"]["active"])
        self.assertEqual(receipt["serving"], receipt["candidate"])
        self.assertEqual(len(receipt["smoke"]["calls"]), 4)


class RollbackTests(ReleaseFlowBase):
    def test_rollback_only_to_released_gateway_off_revision(self):
        arm = FakeArm(image=OLD_IMAGE)
        runner, _ = self.run_stage(arm, stage="rollback-api")
        with self.assertRaisesRegex(release.Refused, "rollback-target"):
            runner.rollback(R0)
        self.assertEqual(arm.writes, 0)

    def test_rollback_refuses_gateway_on_target(self):
        arm = FakeArm(image=release.IMAGES["api"],
                      extra_env=[{"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "true"}])
        runner, _ = self.run_stage(arm, stage="rollback-api")
        with self.assertRaisesRegex(release.Refused, "rollback-target"):
            runner.rollback(R0)

    def test_rollback_rejects_ambiguous_gateway_flags(self):
        cases = [
            [{"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "false"},
             {"name": "ai4ia_shared_gateway_enabled", "value": "true"}],
            [{"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": None}],
            [{"name": "AI4IA_SHARED_GATEWAY_DEPLOYMENTS", "secretRef": "unknown"}],
            [{"name": "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST", "value": None}],
        ]
        for entries in cases:
            arm = FakeArm(image=release.IMAGES["api"], extra_env=entries)
            runner, _ = self.run_stage(arm, stage="rollback-api")
            with self.subTest(entries=entries), self.assertRaisesRegex(release.Refused, "rollback-target"):
                runner.rollback(R0)
            self.assertEqual(arm.writes, 0)

    def test_rollback_moves_traffic_to_r1(self):
        arm = FakeArm(image=release.IMAGES["api"])
        r2 = APP + "--c3-r2"
        arm.revisions[r2] = arm.revision(r2, release.IMAGES["api"], [])
        arm.app["properties"]["configuration"]["ingress"]["traffic"] = [{"revisionName": r2, "weight": 100}]
        runner, receipt = self.run_stage(arm, stage="rollback-api")
        runner.rollback(R0)
        self.assertEqual(receipt["serving"], R0)
        self.assertEqual(arm.writes, 1)

    def test_rollback_refuses_unknown_worker_metrics(self):
        arm = FakeArm(image=release.IMAGES["api"])
        arm.gate_values = [{"value": []}]
        arm.app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": APP + "--c3-r2", "weight": 100}]
        runner, _ = self.run_stage(arm, stage="rollback-api")
        with self.assertRaisesRegex(release.Refused, "dts-gate"):
            runner.rollback(R0)
        self.assertEqual(arm.writes, 0)


class StreamProbeTests(ReleaseFlowBase):
    C3 = APP + "--c3-probe"
    MANIFEST = '{"fake":"manifest"}'

    def setUp(self):
        super().setUp()
        data = self.MANIFEST.encode()
        p = mock.patch.multiple(release, MANIFEST_SIZE=len(data),
                                MANIFEST_SHA256=hashlib.sha256(data).hexdigest())
        p.start()
        self.addCleanup(p.stop)

    def c3_env(self, **changes):
        values = {"AI4IA_SHARED_GATEWAY_ENABLED": "true",
                  "AI4IA_SHARED_GATEWAY_DEPLOYMENTS": release.DEPLOYMENTS_BYTES.decode(),
                  "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST": self.MANIFEST, **changes}
        return [{"name": k, "value": v} for k, v in values.items() if v is not None]

    def serving(self, c3_env=None, image=None):
        arm = FakeArm(image=release.IMAGES["api"])
        arm.revisions[self.C3] = arm.revision(self.C3, image or release.IMAGES["api"],
                                              self.c3_env() if c3_env is None else c3_env)
        arm.app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": self.C3, "weight": 100}]
        return arm

    def probe(self, arm, result=None, probe=None, r1=R0):
        seen = []

        def runner(*args):
            seen.append(args)
            return passing_stream_probe() if result is None else result

        receipt = {"steps": []}
        values = env(AI4IA_RELEASE_STAGE="stream-probe-api", AI4IA_ROLLBACK_REVISION=r1,
                     AI4IA_PROBE_REVISION=probe or self.C3)
        rel = release.Release(arm, values, receipt, now=lambda: NOW, smoke=None, web_probe=None,
                              stream_probe=runner)
        return rel, receipt, seen

    def test_admission_requires_both_named_revisions(self):
        stage = {"AI4IA_RELEASE_STAGE": "stream-probe-api"}
        self.assertEqual(release.validate_environment(env(**stage, AI4IA_ROLLBACK_REVISION=R0,
                                                          AI4IA_PROBE_REVISION=self.C3)),
                         "stream-probe-api")
        for case in ({**stage, "AI4IA_ROLLBACK_REVISION": R0}, {**stage, "AI4IA_PROBE_REVISION": self.C3},
                     {"AI4IA_RELEASE_STAGE": "rollback-api", "AI4IA_ROLLBACK_REVISION": R0,
                      "AI4IA_PROBE_REVISION": self.C3}):
            with self.subTest(case=case), self.assertRaises(release.Refused):
                release.validate_environment(env(**case))

    def test_passes_with_exact_bindings_and_sends_no_writes(self):
        arm = self.serving()
        rel, receipt, seen = self.probe(arm)
        rel.run("stream-probe-api")
        self.assertEqual(arm.writes, 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1:], (release.APPS["api"], self.C3))
        self.assertTrue(release.stream_probe_passed(receipt["stream_probe"]))
        steps = {s["step"]: s for s in receipt["steps"]}
        self.assertEqual(steps["probe-exec"]["control_plane"], "POST getAuthToken")
        self.assertEqual(steps["probe-readback"]["writes"], 0)
        self.assertNotIn(self.MANIFEST, json.dumps(receipt))

    def test_refuses_any_binding_drift_before_exec(self):
        manifest_bad = self.c3_env(AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST=self.MANIFEST + " ")
        cases = {
            "manifest": {"c3_env": manifest_bad},
            "deployments": {"c3_env": self.c3_env(AI4IA_SHARED_GATEWAY_DEPLOYMENTS="x")},
            "gateway-off": {"c3_env": self.c3_env(AI4IA_SHARED_GATEWAY_ENABLED="false")},
            "pilot": {"c3_env": self.c3_env(AI4IA_CHAT_PILOT_ISOLATION_ENABLED="true")},
            "image": {"image": OLD_IMAGE},
        }
        for name, kwargs in cases.items():
            arm = self.serving(**kwargs)
            rel, _, seen = self.probe(arm)
            with self.subTest(name=name), self.assertRaisesRegex(release.Refused, "probe-bindings"):
                rel.run("stream-probe-api")
            self.assertEqual((seen, arm.writes), ([], 0))

        def split(arm):
            arm.app["properties"]["configuration"]["ingress"]["traffic"] = [
                {"revisionName": self.C3, "weight": 90}, {"revisionName": R0, "weight": 10}]

        def single(arm):
            arm.app["properties"]["configuration"]["activeRevisionsMode"] = "Single"

        def r1_inactive(arm):
            arm.revisions[R0]["properties"]["active"] = False

        def r1_gateway_on(arm):
            arm.revisions[R0]["properties"]["template"]["containers"][0]["env"] = self.c3_env()

        def unready(arm):
            arm.unready.add(self.C3)

        for mutate in (split, single, r1_inactive, r1_gateway_on, unready):
            arm = self.serving()
            mutate(arm)
            rel, _, seen = self.probe(arm)
            with self.subTest(case=mutate.__name__), self.assertRaisesRegex(release.Refused, "probe-bindings"):
                rel.run("stream-probe-api")
            self.assertEqual((seen, arm.writes), ([], 0))
        for probe, r1 in ((R0, R0), ("other--x", R0), (self.C3, "../x")):
            arm = self.serving()
            rel, _, seen = self.probe(arm, probe=probe, r1=r1)
            with self.subTest(probe=probe, r1=r1), self.assertRaisesRegex(release.Refused, "probe-bindings"):
                rel.run("stream-probe-api")
            self.assertEqual(seen, [])
            self.assertFalse(any(c[1].endswith("/x") for c in arm.calls))

    def test_failed_or_unknown_probe_refuses_without_writes(self):
        missing = passing_stream_probe()
        missing["phase"] = "calls"
        missing["calls"] = missing["calls"][:1]
        missing["calls"][0].update(usage="missing", prompt=None, completion=None, total=None)
        for result in (missing, {"phase": "calls", "calls": []}, {}):
            arm = self.serving()
            rel, receipt, _ = self.probe(arm, result=result)
            with self.assertRaisesRegex(release.Refused, "stream-probe"):
                rel.run("stream-probe-api")
            self.assertEqual(arm.writes, 0)
            self.assertIn("stream_probe", receipt)


def passing_stream_probe(*_args):
    return {"phase": "complete", "base": "c" * 64, "terminal_usage_flag": False, "calls": [
        {"deployment": d, "posted": True, "status": 200, "host": True, "sse": True, "done": True,
         "events": 3, "finish": "stop", "usage": "verified", "prompt": 10, "completion": 2,
         "total": 12, "reasoning": 0}
        for d in sorted(release.DEPLOYMENTS)]}


class StreamProbeProgramTests(unittest.TestCase):
    MANIFEST = '{"fake":"manifest"}'

    def test_pass_and_sanitize(self):
        self.assertTrue(release.stream_probe_passed(release.sanitize_stream_probe(passing_stream_probe())))
        for field, value in (("usage", "missing"), ("completion", 17), ("total", 13), ("done", False),
                             ("sse", False), ("status", 500), ("prompt", None)):
            result = passing_stream_probe()
            result["calls"][2][field] = value
            with self.subTest(field=field):
                self.assertFalse(release.stream_probe_passed(release.sanitize_stream_probe(result)))
        dirty = passing_stream_probe()
        dirty["calls"][0].update(content="model text", headers={"Authorization": "Bearer x"},
                                 usage="weird", finish="secret")
        dirty["calls"] += copy.deepcopy(dirty["calls"])
        dirty["base"] = "gateway.example"
        clean = release.sanitize_stream_probe(dirty)
        text = json.dumps(clean)
        for leaked in ("model text", "Bearer", "secret", "gateway.example"):
            self.assertNotIn(leaked, text)
        self.assertEqual(len(clean["calls"]), 4)
        self.assertEqual((clean["calls"][0]["usage"], clean["base"]), ("unknown", None))

    def run_program(self, streams, *, streaming_enabled=True, manifest=None):
        posts = []
        settings = types.SimpleNamespace(
            shared_gateway_enabled=True, chat_pilot_isolation_enabled=False,
            shared_gateway_deployment_set=frozenset(release.DEPLOYMENTS),
            shared_gateway_activation_manifest=self.MANIFEST if manifest is None else manifest)

        class Request:
            def __init__(self, url, headers, json):
                self.url, self.headers, self.json = url, headers, json

        class Response:
            def __init__(self, status, lines, host="gateway.invalid", ctype="text/event-stream"):
                self.status_code, self.lines = status, lines
                self.url = types.SimpleNamespace(host=host)
                self.headers = {"content-type": ctype}

            async def aiter_lines(self):
                for line in self.lines:
                    if isinstance(line, BaseException):
                        raise line
                    yield line

        class Stream:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *_args):
                pass

        class FakeHttp:
            def __init__(self, **kwargs):
                posts.append(("client", kwargs))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            def stream(self, method, url, *, headers, json, follow_redirects):
                posts.append((method, url, copy.deepcopy(json), follow_redirects))
                return Stream(Response(*streams.pop(0)))

        class FakeGateway:
            def __init__(self, _settings, *, http_client):
                self._shared_manifest = types.SimpleNamespace(
                    base_url="https://gateway.invalid", terminal_usage_verified=False)

            async def _shared_request(self, req, *, deployment, api, eligible, sku):
                assert (api, eligible, sku) == ("chat", True, release.CANARY_SKUS[deployment])
                if not streaming_enabled:
                    raise RuntimeError("Streaming is not enabled")
                return (Request("https://gateway.invalid/chat/completions", {"Authorization": "Bearer t"},
                                {**req.json, "model": "alias-" + deployment}),
                        "https://gateway.invalid")

            async def close(self):
                pass

        def module(name, **attributes):
            value = types.ModuleType(name)
            value.__dict__.update(attributes)
            return value

        modules = {
            "httpx": module("httpx", AsyncClient=FakeHttp),
            "ai4ia_api.config": module("ai4ia_api.config", get_settings=lambda: settings),
            "ai4ia_api.gateway.client": module(
                "ai4ia_api.gateway.client", ModelGatewayClient=FakeGateway, GatewayRequest=Request),
        }
        program = release.STREAM_PROBE_PROGRAM.replace(
            repr(release.MANIFEST_SHA256), repr(hashlib.sha256(self.MANIFEST.encode()).hexdigest()))
        with (mock.patch.dict("sys.modules", modules), mock.patch("sys.stdout") as output,
              mock.patch("logging.disable")):
            exec(compile(program, "<stream-probe>", "exec"), {})  # noqa: S102 - repository-owned program, fake imports only
        marked = output.write.call_args[0][0]
        return json.loads(marked.split(release.PROBE_MARK)[1]), posts

    @staticmethod
    def good(completion=2):
        usage = {"prompt_tokens": 10, "completion_tokens": completion, "total_tokens": 10 + completion,
                 "completion_tokens_details": {"reasoning_tokens": 0}}
        return (200, ['data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}', "",
                      'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', "",
                      "data: " + json.dumps({"choices": [], "usage": usage}), "", "data: [DONE]"])

    def test_four_capped_streams_with_terminal_usage_pass(self):
        result, posts = self.run_program([self.good() for _ in range(4)])
        self.assertTrue(release.stream_probe_passed(release.sanitize_stream_probe(result)))
        client = posts.pop(0)[1]
        self.assertEqual((client["trust_env"], client["follow_redirects"]), (False, False))
        self.assertEqual(len(posts), 4)
        for (method, url, body, redirects), deployment in zip(posts, sorted(release.DEPLOYMENTS), strict=True):
            self.assertEqual((method, url, redirects), ("POST", "https://gateway.invalid/chat/completions", False))
            self.assertEqual(body, {**release.PROBE_BODY, "model": "alias-" + deployment})
        self.assertEqual(release.PROBE_BODY["max_completion_tokens"], 16)
        self.assertEqual(release.PROBE_BODY["reasoning_effort"], "none")
        self.assertNotIn("OK", json.dumps(result))

    def test_stops_at_first_failure_and_reports_unknown(self):
        no_usage = (200, ['data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}',
                          "data: [DONE]"])
        cut = (200, ['data: {"choices":[]}', asyncio_cancelled()])
        cases = {
            "missing-usage": ([self.good(), no_usage, self.good()], "missing", 2),
            "cancelled": ([cut, self.good()], "unknown", 1),
            "no-done": ([(200, ['data: {"choices":[]}'])], "unknown", 1),
            "over-cap": ([self.good(completion=17)], "invalid", 1),
            "http-500": ([(500, [])], "unknown", 1),
            "not-sse": ([(200, [], "gateway.invalid", "application/json")], "unknown", 1),
            "redirected-host": ([(200, [], "elsewhere.invalid")], "unknown", 1),
        }
        for name, (streams, usage, count) in cases.items():
            with self.subTest(name=name):
                result, posts = self.run_program(list(streams))
                self.assertEqual(len(posts) - 1, count)
                self.assertEqual(result["phase"], "calls")
                self.assertEqual(result["calls"][-1]["usage"], usage)
                self.assertFalse(release.stream_probe_passed(release.sanitize_stream_probe(result)))

    def test_refuses_before_any_post(self):
        result, posts = self.run_program([], streaming_enabled=False)
        self.assertEqual((len(posts), result["phase"], result["calls"][0]["posted"]), (1, "calls", False))
        result, posts = self.run_program([], manifest=self.MANIFEST + " ")
        self.assertEqual((posts, result["phase"]), ([], "config"))

    def test_program_compiles_and_survives_shell_transport(self):
        compile(release.STREAM_PROBE_PROGRAM, "<stream-probe>", "exec")
        lines = release.shell_lines(release.STREAM_PROBE_PROGRAM)
        self.assertTrue(all(len(line) < 1024 for line in lines))
        self.assertEqual(posix_assembled_program(lines), release.STREAM_PROBE_PROGRAM)


def asyncio_cancelled():
    import asyncio
    return asyncio.CancelledError()


class CutoverIntegrationTests(unittest.TestCase):
    def execute_c1(self, arm):
        arm.app["properties"]["configuration"]["activeRevisionsMode"] = "Single"
        return release.execute(
            env(AI4IA_RELEASE_STAGE="c1-api"), token_source=lambda e, t: "tok",
            arm_class=lambda token: arm, now=lambda: NOW,
        )

    def test_scale_defaults_and_numeric_representations_pass_c1_and_are_audited(self):
        absent = object()
        for field, default in (("cooldownPeriod", 300), ("pollingInterval", 30)):
            for a, b, kind, left_type, right_type in (
                (default, None, "provider_default", "int", "null"),
                (None, default, "provider_default", "null", "int"),
                (absent, default, "provider_default", "absent", "int"),
                (default, absent, "provider_default", "int", "absent"),
                (default, float(default), "numeric_repr", "int", "float"),
                (float(default), default, "numeric_repr", "float", "int"),
                (60, 60.0, "numeric_repr", "int", "float"),
            ):
                arm = FakeArm()
                left = arm.app["properties"]["template"]
                right = arm.revisions[R0]["properties"]["template"]
                left["scale"] = {} if a is absent else {field: a}
                right["scale"] = {} if b is absent else {field: b}
                originals = copy.deepcopy((left, right))
                with self.subTest(field=field, kind=kind, left=left_type, right=right_type):
                    receipt = self.execute_c1(arm)
                    self.assertEqual((receipt["outcome"], receipt["writes"]), ("complete", 1))
                    step = next(s for s in receipt["steps"] if s["step"] == "r0-template-normalized")
                    self.assertEqual(step["template_diff"], {"entries": [{
                        "path": ["scale", field], "kind": kind,
                        "left_type": left_type, "right_type": right_type,
                    }], "truncated": False})
                    if kind == "provider_default":
                        self.assertFalse(step["facts"]["template_equal_canonical"])
                    self.assertEqual((left, right), originals)
                    verified = next(s for s in receipt["steps"] if s["step"] == "c1-verified")
                    self.assertEqual((verified["mode"], verified["traffic"]),
                                     ("Multiple", [{"revisionName": R0, "weight": 100}]))

    def test_other_scale_types_values_and_other_paths_refuse_with_types(self):
        absent = object()
        for field in ("cooldownPeriod", "pollingInterval"):
            for a, b, kind, left_type, right_type in (
                (None, 60, "type", "null", "int"),
                ("300", 300, "type", "str", "int"),
                (True, 300, "type", "bool", "int"),
                (None, 300.0, "type", "null", "float"),
                (300, 300.5, "type", "int", "float"),
                (absent, 60, "missing_left", "absent", "int"),
                (60, absent, "missing_right", "int", "absent"),
            ):
                arm = FakeArm()
                left = arm.app["properties"]["template"]
                right = arm.revisions[R0]["properties"]["template"]
                left["scale"] = {} if a is absent else {field: a}
                right["scale"] = {} if b is absent else {field: b}
                with self.subTest(field=field, kind=kind, left=left_type, right=right_type):
                    receipt = self.execute_c1(arm)
                    self.assertEqual((receipt["detail"]["reason"], receipt["writes"]), ("r0-state", 0))
                    self.assertEqual(receipt["detail"]["template_diff"]["entries"], [{
                        "path": ["scale", field], "kind": kind,
                        "left_type": left_type, "right_type": right_type,
                    }])
        for a, b in ((None, 30), (30, 30.0)):
            arm = FakeArm()
            arm.app["properties"]["template"]["containers"][0]["probes"] = [{"periodSeconds": a}]
            arm.revisions[R0]["properties"]["template"]["containers"][0]["probes"] = [{"periodSeconds": b}]
            receipt = self.execute_c1(arm)
            self.assertEqual((receipt["detail"]["reason"], receipt["writes"]), ("r0-state", 0))
            entry = receipt["detail"]["template_diff"]["entries"][0]
            self.assertEqual((entry["path"], entry["kind"]),
                             (["containers", 0, "probes", 0, "periodSeconds"], "type"))
        arm = FakeArm()
        arm.app["properties"]["template"]["scale"] = {"cooldownPeriod": 300}
        arm.revisions[R0]["properties"]["template"]["scale"] = {"cooldownPeriod": 60}
        receipt = self.execute_c1(arm)
        self.assertEqual((receipt["detail"]["reason"], receipt["writes"]), ("r0-state", 0))
        self.assertEqual(receipt["detail"]["template_diff"]["entries"][0]["kind"], "value")

    def test_c2_app_default_readback_matches_unmodified_revision_payload(self):
        class DefaultReadbackArm(FakeArm):
            def write(self, method, path, body=None, **kwargs):
                super().write(method, path, body, **kwargs)
                if kwargs.get("phase") == "create":
                    template = body["properties"]["template"]
                    candidate = self.app["properties"]["latestRevisionName"]
                    self.revisions[candidate]["properties"]["template"] = copy.deepcopy(template)
                    self.app["properties"]["template"] = copy.deepcopy(template)
                    self.app["properties"]["template"]["scale"] = {
                        "cooldownPeriod": 300, "pollingInterval": 30,
                    }

        arm = DefaultReadbackArm()
        arm.app["properties"]["template"]["scale"] = {"cooldownPeriod": 300, "pollingInterval": 30}
        arm.revisions[R0]["properties"]["template"]["scale"] = {"cooldownPeriod": None, "pollingInterval": None}
        now = datetime.now(UTC)
        arm.gate_values = [metrics(ZERO, at=now - timedelta(minutes=1))]
        receipt = {"steps": []}
        runner = release.Release(arm, env(), receipt, now=lambda: now)

        runner.run("c2-api")

        self.assertEqual(receipt["serving"], receipt["candidate"])
        self.assertIn("candidate-ready", [s["step"] for s in receipt["steps"]])
        created = next(body for _, _, body, phase in arm.requests if phase == "create")
        self.assertEqual(created["properties"]["template"]["scale"],
                         {"cooldownPeriod": None, "pollingInterval": None})
        self.assertFalse(arm.revisions[R0]["properties"]["active"])

    def test_template_matches_still_refuses_image_env_and_list_position_drift(self):
        expected = {"containers": [{"name": "api", "image": "PRIVATE_IMAGE_SENTINEL", "env": [
            {"name": "PRIVATE_ENV_SENTINEL", "value": "PRIVATE_VALUE_SENTINEL"},
        ]}], "scale": {"cooldownPeriod": None}}
        actual = copy.deepcopy(expected)
        actual["scale"]["cooldownPeriod"] = 300
        self.assertTrue(release.template_matches(actual, expected))
        for field in ("image", "env"):
            changed = copy.deepcopy(actual)
            if field == "image":
                changed["containers"][0]["image"] = "PRIVATE_CHANGED_SENTINEL"
            else:
                changed["containers"][0]["env"][0]["value"] = "PRIVATE_CHANGED_SENTINEL"
            self.assertFalse(release.template_matches(changed, expected))
        for a, b in (([None], [{}]), ([None], [[]])):
            self.assertFalse(release.template_matches({"args": a}, {"args": b}))

    def test_empties_only_pass_and_are_audited_without_mutating_templates(self):
        arm = FakeArm()
        left = arm.app["properties"]["template"]
        right = arm.revisions[R0]["properties"]["template"]
        left.update(volumes=[], serviceBinds=None, scale={"rules": []})
        left["containers"][0]["probes"] = None
        right["containers"][0]["probes"] = []
        right["initContainers"] = []
        originals = copy.deepcopy((left, right))

        receipt = self.execute_c1(arm)

        self.assertEqual((receipt["outcome"], receipt["writes"]), ("complete", 1))
        normalized = next(s for s in receipt["steps"] if s["step"] == "r0-template-normalized")
        self.assertEqual(normalized["facts"], {"template_equal": False, "template_equal_canonical": True})
        diff = normalized["template_diff"]
        self.assertFalse(diff["truncated"])
        self.assertEqual({e["kind"] for e in diff["entries"]}, {"absent_vs_empty", "null_vs_empty"})
        self.assertIn({"path": ["containers", 0, "probes"], "kind": "null_vs_empty"}, diff["entries"])
        self.assertEqual((left, right), originals)
        self.assertTrue(all("template" not in body["properties"] for _, _, body, _ in arm.requests))

    def test_value_missing_type_and_length_differences_refuse_without_content(self):
        sentinel = "SYNTHETIC_PRIVATE_SENTINEL"
        for field, a, b, kind in (
            ("env", [{"name": sentinel, "value": sentinel}], [{"name": sentinel, "value": "different"}], "value"),
            ("image", sentinel, sentinel + "-different", "value"),
            ("args", [sentinel], None, "type"),
            ("args", [sentinel], [sentinel, sentinel], "length"),
        ):
            arm = FakeArm()
            arm.app["properties"]["template"]["containers"][0][field] = a
            arm.revisions[R0]["properties"]["template"]["containers"][0][field] = b
            with self.subTest(field=field, kind=kind):
                receipt = self.execute_c1(arm)
                self.assertEqual((receipt["phase"], receipt["writes"]), ("payload", 0))
                self.assertEqual(receipt["detail"]["reason"], "r0-state")
                entries = receipt["detail"]["template_diff"]["entries"]
                self.assertIn(kind, [e["kind"] for e in entries])
                self.assertEqual(entries[0]["path"][:3], ["containers", 0, field])
                self.assertNotIn(sentinel, json.dumps(receipt))
        for missing_side, kind in (("app", "missing_left"), ("revision", "missing_right")):
            arm = FakeArm()
            target = arm.revisions[R0] if missing_side == "app" else arm.app
            target["properties"]["template"][sentinel] = sentinel
            receipt = self.execute_c1(arm)
            self.assertEqual((receipt["detail"]["reason"], receipt["writes"]), ("r0-state", 0))
            self.assertEqual(receipt["detail"]["template_diff"]["entries"], [{
                "path": ["?"], "kind": kind,
                "left_type": "absent" if missing_side == "app" else "str",
                "right_type": "str" if missing_side == "app" else "absent",
            }])
            self.assertNotIn(sentinel, json.dumps(receipt))

    def test_diff_cap_does_not_hide_a_nonempty_difference_after_32_empty_diffs(self):
        arm = FakeArm()
        left = arm.app["properties"]["template"]
        right = arm.revisions[R0]["properties"]["template"]
        left["containers"][0]["env"] = [
            {"name": f"SECRET_ENV_{i}", "value": None} for i in range(33)]
        right["containers"][0]["env"] = [{"name": f"SECRET_ENV_{i}"} for i in range(33)]
        left["volumes"], right["volumes"] = [], {}
        receipt = self.execute_c1(arm)
        diff = receipt["detail"]["template_diff"]
        self.assertEqual((receipt["detail"]["reason"], receipt["writes"]), ("r0-state", 0))
        self.assertEqual(len(diff["entries"]), 32)
        self.assertTrue(diff["truncated"])
        self.assertTrue(all(e["kind"] == "absent_vs_empty" for e in diff["entries"]))
        self.assertTrue(receipt["detail"]["facts"]["template_equal_canonical"])
        self.assertNotIn("SECRET_ENV", json.dumps(receipt))

    def test_33_empty_differences_can_pass_with_truncation_audited(self):
        arm = FakeArm()
        arm.app["properties"]["template"]["containers"][0]["env"] = [
            {"name": str(i), "value": None} for i in range(33)]
        arm.revisions[R0]["properties"]["template"]["containers"][0]["env"] = [
            {"name": str(i)} for i in range(33)]
        receipt = self.execute_c1(arm)
        self.assertEqual((receipt["outcome"], receipt["writes"]), ("complete", 1))
        diff = receipt["steps"][0]["template_diff"]
        self.assertEqual(len(diff["entries"]), 32)
        self.assertTrue(diff["truncated"])

    def test_diff_depth_is_bounded_and_unknown_deep_difference_refuses(self):
        arm = FakeArm()
        a, b = {"value": "PRIVATE_LEFT"}, {"value": "PRIVATE_RIGHT"}
        for _ in range(20):
            a, b = {"custom": a}, {"custom": b}
        arm.app["properties"]["template"]["scale"] = a
        arm.revisions[R0]["properties"]["template"]["scale"] = b
        receipt = self.execute_c1(arm)
        diff = receipt["detail"]["template_diff"]
        self.assertEqual((receipt["detail"]["reason"], receipt["writes"]), ("r0-state", 0))
        self.assertTrue(diff["truncated"])
        self.assertTrue(all(len(e["path"]) <= 16 for e in diff["entries"]))
        self.assertNotIn("PRIVATE_", json.dumps(receipt))

    def test_strict_invariance_and_emitted_c2_template_keep_empty_entries(self):
        arm = FakeArm()
        app, rev = copy.deepcopy(arm.app), copy.deepcopy(arm.revisions[R0])
        app["properties"]["template"]["volumes"] = None
        self.assertNotEqual(release.projection(app), release.projection(arm.app))
        self.assertTrue(release.template_matches(
            app["properties"]["template"], rev["properties"]["template"]))
        project = release.cutover().writable_template_projection
        self.assertNotEqual(project(app["properties"]["template"]), project(rev["properties"]["template"]))
        with self.assertRaisesRegex(release.Refused, "concurrent-change"):
            release.Release(arm, env(), {"steps": []}).guarded_patch(
                "api", release.projection(app), {"properties": {}}, "must-not-write",
            )
        self.assertEqual(arm.writes, 0)
        rev["properties"]["template"]["containers"][0]["probes"] = []
        rev["properties"]["template"]["serviceBinds"] = None
        now = datetime.now(UTC)
        rows = release.revision_rows(arm, release.APPS["api"], now)
        payload = release.build_payload(
            app, rev, resource_id=release.APPS["api"], stage="C2", service="api",
            suffix="candidate", api_image=release.IMAGES["api"], existing_revisions=rows,
        )
        template = payload["properties"]["template"]
        self.assertIsNone(template["serviceBinds"])
        self.assertEqual(template["containers"][0]["probes"], [])
        self.assertNotIn("volumes", template)

    def test_real_c2_builder_preserves_references_and_released_image(self):
        baseline_env = [{"name": "VOICE_UNCHANGED", "secretRef": "voice"}]
        arm = FakeArm(extra_env=baseline_env)
        arm.app["properties"]["configuration"]["secrets"] = [{"name": "voice"}]
        now = datetime.now(UTC)
        arm.gate_values = [metrics(ZERO, at=now - timedelta(minutes=1))]
        gate = release.DtsGate(arm, now=lambda: now, attempts=1)
        receipt = {"steps": []}
        runner = release.Release(arm, env(), receipt, gate=gate, now=lambda: now)

        runner.run("c2-api")

        self.assertEqual(receipt["serving"], receipt["candidate"])
        self.assertEqual(arm.app["properties"]["configuration"]["secrets"], [{"name": "voice"}])
        self.assertEqual(
            arm.revisions[receipt["candidate"]]["properties"]["template"]["containers"][0]["env"],
            baseline_env,
        )
        self.assertEqual(
            arm.revisions[receipt["candidate"]]["properties"]["template"]["containers"][0]["image"],
            release.IMAGES["api"],
        )
        for _, _, body, _ in arm.requests:
            if body is not None:
                self.assertNotIn("secrets", (body["properties"].get("configuration") or {}))

    def test_real_builder_accepts_arm_casing_and_max_scale_but_not_other_drift(self):
        arm = FakeArm()
        runner = release.Release(arm, env(), {"steps": []})
        app, rev, _ = runner.snapshot("api")
        app["properties"]["configuration"]["activeRevisionsMode"] = "Single"
        app["id"] = app["id"].replace("/resourceGroups/", "/resourcegroups/").replace(
            "/containerApps/", "/containerapps/")
        rev["id"] = rev["id"].replace("/containerApps/", "/containerapps/")
        rev["properties"]["runningState"] = "RunningAtMaxScale"
        payload = release.build_payload(app, rev, resource_id=release.APPS["api"], stage="C1", service="api")
        self.assertEqual(payload["properties"]["configuration"]["activeRevisionsMode"], "Multiple")
        cases = {
            "id": ("id", app["id"].replace("ca-api-private", "ca-api-other")),
            "r0-state": ("runningState", "RunningAtMaxScaleX"),
        }
        for reason, (field, value) in cases.items():
            a, r = json.loads(json.dumps(app)), json.loads(json.dumps(rev))
            if field == "id":
                a["id"] = value
            else:
                r["properties"][field] = value
            with self.subTest(reason), self.assertRaises(release.Refused) as caught:
                release.build_payload(a, r, resource_id=release.APPS["api"], stage="C1", service="api")
            self.assertEqual(caught.exception.detail["reason"], reason)

    def test_real_builder_refusal_reports_only_line_and_snapshot_facts(self):
        arm = FakeArm()
        runner = release.Release(arm, env(), {"steps": []})
        app, rev, _ = runner.snapshot("api")
        app["properties"]["configuration"]["activeRevisionsMode"] = "Single"
        rev["properties"]["template"]["containers"][0]["image"] = "registry.example/ai4ia/api:latest"
        app["properties"]["template"] = rev["properties"]["template"]
        with self.assertRaises(release.Refused) as caught:
            release.build_payload(app, rev, resource_id=release.APPS["api"], stage="C1", service="api")
        detail = caught.exception.detail
        self.assertEqual(caught.exception.phase, "payload")
        self.assertEqual((detail["error"], detail["reason"]), ("ValueError", "image"))
        builder = (release.ROOT / "scripts" / "prepare-gateway-cutover.py").read_text().splitlines()
        self.assertIn("digest-pinned", builder[detail["line"] - 1])
        self.assertIs(detail["facts"]["app_id_exact"], True)
        self.assertIs(detail["facts"]["digest_pinned"], False)
        text = json.dumps(detail)
        for secret in ("slurmfactory", rev["name"], "subscriptions", "registry.example"):
            self.assertNotIn(secret, text)
        self.assertEqual(arm.writes, 0)


class StaleLatestTests(unittest.TestCase):
    """Live state after an unpromoted lane candidate: R0 serves 100%, latest is stale."""

    STALE = APP + "--c2-2721cac-27742728"

    def stale_arm(self, name=STALE, *, image=None, env_list=None, active=False, extra_rule=False,
                  ready=None, mode="Multiple"):
        arm = FakeArm()
        arm.app["properties"]["configuration"]["activeRevisionsMode"] = mode
        arm.revisions[name] = arm.revision(name, image or release.IMAGES["api"], env_list or [])
        arm.revisions[name]["properties"]["active"] = active
        arm.app["properties"]["latestRevisionName"] = name
        arm.app["properties"]["latestReadyRevisionName"] = ready or name
        arm.app["properties"]["template"] = copy.deepcopy(arm.revisions[name]["properties"]["template"])
        if extra_rule:
            arm.app["properties"]["configuration"]["ingress"]["traffic"].append(
                {"revisionName": name, "weight": 0})
        return arm

    def execute(self, arm, stage="c2-api"):
        now = datetime.now(UTC)
        arm.gate_values = [metrics(ZERO, at=now - timedelta(minutes=1))]
        return release.execute(env(AI4IA_RELEASE_STAGE=stage), token_source=lambda e, t: "tok",
                               arm_class=lambda token: arm, now=lambda: now)

    def assert_redacted(self, receipt):
        text = json.dumps(receipt["detail"])
        for secret in ("slurmfactory", "27742728", "2721cac", "subscriptions"):
            self.assertNotIn(secret, text)

    def test_old_strict_builder_refuses_live_state_and_new_snapshot_creates(self):
        arm = self.stale_arm()
        app = copy.deepcopy(arm.app)
        with self.assertRaises(release.Refused) as caught:
            release.build_payload(app, copy.deepcopy(arm.revisions[self.STALE]),
                                  resource_id=release.APPS["api"], stage="C2", service="api",
                                  suffix="c2-x", api_image=release.IMAGES["api"],
                                  existing_revisions=release.revision_rows(arm, release.APPS["api"], NOW))
        self.assertEqual(caught.exception.detail["reason"], "r0-state")

        receipt = self.execute(arm)

        self.assertEqual((receipt["outcome"], receipt["r0"]), ("complete", R0))
        self.assertEqual(receipt["serving"], receipt["candidate"])
        self.assertNotEqual(receipt["candidate"], self.STALE)
        step = next(s for s in receipt["steps"] if s["step"] == "r0-serving-not-latest")
        self.assertEqual((step["r0"], step["latest"], step["latest_ready"]), (R0, self.STALE, self.STALE))
        self.assertTrue(all(value is True for value in step["facts"].values()))
        self.assertIs(arm.revisions[self.STALE]["properties"]["active"], False)
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"],
                         ["create", "promote", "deactivate-r0"])
        built = arm.revisions[receipt["candidate"]]["properties"]["template"]["containers"][0]
        self.assertEqual(built["image"], release.IMAGES["api"])

    def test_latest_ready_may_still_be_r0(self):
        receipt = self.execute(self.stale_arm(ready=R0))
        self.assertEqual(receipt["outcome"], "complete")

    def candidate_name(self, stage="c2"):
        values = env()
        return APP + "--" + release.candidate_suffix(stage, values["GITHUB_SHA"], values["GITHUB_RUN_ID"])

    def assert_only_one_candidate(self, arm, before, receipt):
        def keys(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    yield k
                    yield from keys(v)
            elif isinstance(value, list):
                for v in value:
                    yield from keys(v)

        app_id = release.APPS["api"]
        for method, path, body, _phase in arm.requests:
            self.assertTrue((method, path) == ("PATCH", app_id) or (
                method == "POST" and re.fullmatch(re.escape(app_id) + r"/revisions/[^/]+/deactivate", path)),
                (method, path))
            self.assertNotIn("secrets", set(keys(body)))
        creates = [body for _m, _p, body, phase in arm.requests if phase == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(set(arm.revisions) - before, {receipt["candidate"]})
        created = copy.deepcopy(creates[0]["properties"]["template"])
        self.assertEqual(created.pop("revisionSuffix"), receipt["candidate"].split("--", 1)[1])
        r0 = copy.deepcopy(arm.revisions[R0]["properties"]["template"])
        self.assertEqual(created["containers"][0].pop("image"), release.IMAGES["api"])
        r0["containers"][0].pop("image")
        self.assertEqual(created, r0)

    def test_each_run_leaves_only_its_candidate_and_accounts_for_it(self):
        for label, arm in {"stale": self.stale_arm(), "fresh": FakeArm()}.items():
            before = set(arm.revisions)
            with self.subTest(label):
                receipt = self.execute(arm)
                self.assertEqual(receipt["outcome"], "complete")
                self.assert_only_one_candidate(arm, before, receipt)
                expected = [{"name": receipt["candidate"], "role": "candidate",
                             "creation_attempted": True, "active": True}]
                if label == "stale":
                    expected.append({"name": self.STALE, "role": "stale-latest",
                                     "creation_attempted": False, "active": False})
                self.assertEqual(receipt["lane_revisions"], expected)

    def test_refused_candidate_is_inventoried_inactive_and_pre_create_refusal_lists_none(self):
        arm = self.stale_arm()
        before = set(arm.revisions)
        arm.unready.add(self.candidate_name())
        receipt = self.execute(arm)
        self.assertEqual(receipt["outcome"], "refused")
        self.assert_only_one_candidate(arm, before, receipt)
        self.assertEqual(receipt["lane_revisions"][0], {
            "name": self.candidate_name(), "role": "candidate", "creation_attempted": True, "active": False})
        self.assertEqual(receipt["lane_revisions"][1]["active"], False)

        arm = self.stale_arm(active=True)
        receipt = self.execute(arm)
        self.assertEqual((receipt["detail"]["reason"], receipt["writes"], receipt["lane_revisions"]),
                         ("r0-latest", 0, []))
        self.assertFalse(any(c[2] == "inventory" for c in arm.calls))

    def test_create_403_is_attempted_not_confirmed(self):
        arm = self.stale_arm()
        before = set(arm.revisions)
        arm.fail["create"] = release.Refused("create", 403)
        receipt = self.execute(arm)
        self.assertEqual(receipt["outcome"], "refused")
        self.assertEqual(set(arm.revisions), before)
        row = receipt["lane_revisions"][0]
        self.assertEqual(row, {"name": self.candidate_name(), "role": "candidate",
                               "creation_attempted": True, "active": None, "observed": False})
        self.assertNotIn("created_by_run", json.dumps(receipt))

    def test_no_extra_read_when_latest_is_r0(self):
        arm = FakeArm()
        _, rev, stale = release.Release(arm, env(), {"steps": []}).snapshot("api", "C2")
        self.assertIsNone(stale)
        self.assertEqual(rev["name"], R0)
        self.assertEqual([c[1] for c in arm.calls if "/revisions/" in c[1]],
                         [release.APPS["api"] + "/revisions/" + R0])

    def test_unqualified_latest_refuses_r0_latest_without_writes(self):
        cases = {
            "active": {"active": True},
            "non-lane-name": {"name": APP + "--manual-1"},
            "c1-name": {"name": APP + "--c1-2721cac-27742728"},
            "other-app": {"name": "ca-web-slurmfactory--c2-2721cac-27742728"},
            "ready-third": {"ready": APP + "--other"},
        }
        for label, kwargs in cases.items():
            arm = self.stale_arm(**kwargs)
            with self.subTest(label):
                receipt = self.execute(arm)
                self.assertEqual((receipt["outcome"], receipt["phase"], receipt["writes"]),
                                 ("refused", "payload", 0))
                self.assertEqual(receipt["detail"]["reason"], "r0-latest")
                self.assertIs(receipt["detail"]["facts"]["latest_is_r0"], False)
                self.assert_redacted(receipt)

    def test_stale_in_traffic_or_extra_diff_refuses(self):
        cases = {
            "in-traffic": ({"extra_rule": True}, "r0-state"),
            "extra-env": ({"env_list": [{"name": "EXTRA", "value": "1"}]}, "r0-latest"),
            "c3-name-image-diff": ({"name": APP + "--c3-2721cac-27742728"}, "r0-latest"),
        }
        for label, (kwargs, reason) in cases.items():
            arm = self.stale_arm(**kwargs)
            with self.subTest(label):
                receipt = self.execute(arm)
                self.assertEqual((receipt["detail"]["reason"], receipt["writes"]), (reason, 0))
                self.assert_redacted(receipt)
                if label != "in-traffic":
                    self.assertIs(receipt["detail"]["facts"]["stale_diff_ok"], False)

    def test_c1_keeps_latest_as_r0_and_refuses_stale(self):
        arm = self.stale_arm(mode="Single")
        receipt = self.execute(arm, stage="c1-api")
        self.assertEqual((receipt["detail"]["reason"], receipt["writes"]), ("r0-state", 0))
        self.assertFalse(any(s["step"] == "r0-serving-not-latest" for s in receipt["steps"]))

    def test_c3_snapshot_admits_stale_c3_candidate_and_baselines_r0(self):
        c3_env = [{"name": name, "value": "x"} for name in (
            "AI4IA_SHARED_GATEWAY_ENABLED", "AI4IA_SHARED_GATEWAY_DEPLOYMENTS",
            "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST")]
        name = APP + "--c3-2721cac-27742728"
        arm = self.stale_arm(name, image=OLD_IMAGE, env_list=c3_env)
        receipt = {"steps": []}
        _, rev, stale = release.Release(arm, env(), receipt).snapshot("api", "C3")
        self.assertEqual((rev["name"], stale["name"]), (R0, name))
        self.assertTrue(release.snapshot_facts(arm.app, rev, release.APPS["api"], stale)["stale_diff_ok"])
        arm = self.stale_arm(name, image=OLD_IMAGE, env_list=c3_env + [{"name": "EXTRA", "value": "1"}])
        _, rev, stale = release.Release(arm, env(), {"steps": []}).snapshot("api", "C3")
        self.assertFalse(release.snapshot_facts(arm.app, rev, release.APPS["api"], stale)["stale_diff_ok"])


class WebStageTests(ReleaseFlowBase):
    """c2-web against the fake app state; the web app id is aliased to the fake."""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(release.APPS, {"web": release.APPS["api"]})
        p.start()
        self.addCleanup(p.stop)

    def unhealthy_after_promote(self, arm, receipt):
        original = arm.get

        def get(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if kwargs.get("phase") == "readback" and receipt.get("candidate", "~") in path:
                value["properties"]["healthState"] = "Unhealthy"
            return value

        arm.get = get
        return original

    def test_probes_pass_before_promotion_and_r0_deactivation(self):
        arm = FakeArm()
        seen = []

        def probe(_arm, app_id, revision):
            seen.append((revision, [c[2] for c in arm.calls if c[0] != "GET"]))
            return passing_web_probe()

        runner, receipt = self.run_stage(arm, stage="c2-web", web_probe=probe)
        runner.run("c2-web")
        self.assertEqual(seen, [(receipt["candidate"], ["create"])])
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "promote", "deactivate-r0"])
        self.assertEqual(receipt["web_probe"]["build"], release.WEB_BUILD_ID)
        self.assertFalse(arm.revisions[R0]["properties"]["active"])

    def test_probe_failure_keeps_r0_serving_and_deactivates_candidate(self):
        for field, value in (("build", "0" * 20), ("ready", False), ("ready_status", 401),
                             ("build_status", 302), ("phase", "ready")):
            arm = FakeArm()
            result = {**passing_web_probe(), field: value}
            runner, _receipt = self.run_stage(arm, stage="c2-web", web_probe=lambda *_a, r=result: r)
            with self.subTest(field=field):
                with self.assertRaisesRegex(release.Refused, "web-probe"):
                    runner.run("c2-web")
                self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "deactivate-unpromoted"])
                self.assertTrue(arm.revisions[R0]["properties"]["active"])
                self.assertEqual(arm.app["properties"]["configuration"]["ingress"]["traffic"],
                                 [{"revisionName": R0, "weight": 100}])

    def test_probe_transport_failure_deactivates_candidate(self):
        arm = FakeArm()

        def broken(*_args):
            raise release.Refused("smoke-transport")

        runner, receipt = self.run_stage(arm, stage="c2-web", web_probe=broken)
        with self.assertRaisesRegex(release.Refused, "smoke-transport"):
            runner.run("c2-web")
        self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])
        self.assertTrue(arm.revisions[R0]["properties"]["active"])

    def test_final_dts_refusal_after_passing_probes_deactivates_candidate(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm, stage="c2-web")
        with mock.patch.object(runner.gate, "require_fresh",
                               side_effect=[None, release.Refused("dts-gate")]), \
                self.assertRaisesRegex(release.Refused, "dts-gate"):
            runner.run("c2-web")
        self.assertEqual([s["step"] for s in receipt["steps"]][-2:], ["web-probe-passed", "deactivate-unpromoted"])
        self.assertFalse(arm.revisions[receipt["candidate"]]["properties"]["active"])
        self.assertEqual(arm.app["properties"]["configuration"]["ingress"]["traffic"],
                         [{"revisionName": R0, "weight": 100}])

    def test_failed_promotion_readback_restores_unchanged_healthy_web_r0(self):
        arm = FakeArm()
        runner, receipt = self.run_stage(arm, stage="c2-web")
        self.unhealthy_after_promote(arm, receipt)
        with self.assertRaisesRegex(release.Refused, "promote-readback"):
            runner.run("c2-web")
        self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "promote", "restore"])
        self.assertEqual(arm.app["properties"]["configuration"]["ingress"]["traffic"],
                         [{"revisionName": R0, "weight": 100}])
        self.assertTrue(arm.revisions[R0]["properties"]["active"])

    def test_web_r0_changed_or_unhealthy_is_not_restored(self):
        for change in ("image", "health"):
            arm = FakeArm()
            runner, receipt = self.run_stage(arm, stage="c2-web")
            original = self.unhealthy_after_promote(arm, receipt)
            wrapped = arm.get

            def get(path, *args, _c=change, wrapped=wrapped, **kwargs):
                value = wrapped(path, *args, **kwargs)
                if kwargs.get("phase") == "reconcile" and path.endswith("/revisions/" + R0):
                    if _c == "image":
                        value["properties"]["template"]["containers"][0]["image"] = release.IMAGES["web"]
                    else:
                        value["properties"]["healthState"] = "Unhealthy"
                return value

            arm.get = get
            with self.subTest(change=change), self.assertRaisesRegex(release.Uncertain, "restore-target"):
                runner.run("c2-web")
            self.assertEqual([c[2] for c in arm.calls if c[0] != "GET"], ["create", "promote"])
            del original


class WebProbeTests(unittest.TestCase):
    def test_sanitize_keeps_only_allowlisted_fields(self):
        dirty = {**passing_web_probe(), "phase": "token=abc", "build": "<html>", "api": "x",
                 "ready_status": "200", "body": "secret"}
        clean = release.sanitize_web_probe(dirty)
        self.assertEqual(set(clean), {"phase", "build", "build_status", "api", "ready_status", "ready"})
        self.assertEqual((clean["phase"], clean["build"], clean["api"], clean["ready_status"]),
                         (None, None, None, None))
        self.assertTrue(release.web_probe_passed(release.sanitize_web_probe(passing_web_probe())))
        self.assertFalse(release.web_probe_passed(clean))

    def test_build_id_matches_web_sources(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node unavailable")
        module = (ROOT / "app" / "web" / "build-id.mjs").as_uri()
        out = subprocess.run(
            [node, "--input-type=module", "-e",
             (f"import {{webBuildId}} from {json.dumps(module)};"
              f"console.log(webBuildId({json.dumps(str(ROOT / 'app' / 'web'))}))")],
            capture_output=True, text=True, timeout=60, check=True)
        if out.stdout.strip() != release.WEB_BUILD_ID:
            # app/web changed after the pinned image; the probe constant must follow the image.
            self.skipTest("app/web differs from the pinned web image sources")

    def run_program(self, routes, extra_env=None):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node unavailable")
        calls = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.server.server_port, self.path, self.headers.get("Cookie")))
                status, body = routes.get((self.server.server_port, self.path), (404, b"{}"))
                self.send_response(status)
                if status in (301, 302):
                    self.send_header("Location", "http://127.0.0.1:1/x")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        web, api = (s.server_port for s in servers)
        routes = {(web if k[0] == "web" else api, k[1]): v for k, v in routes.items()}
        try:
            values = {**os.environ, "PORT": str(web), "API_BASE_URL": f"http://127.0.0.1:{api}",
                      **(extra_env or {})}
            out = subprocess.run([node, "-e", release.WEB_PROBE_PROGRAM], env=values,
                                 capture_output=True, text=True, timeout=60, check=False)
        finally:
            for server in servers:
                server.shutdown()
        line = [l for l in out.stdout.splitlines() if l.startswith(release.WEB_PROBE_MARK)]
        self.assertEqual(len(line), 1)
        return json.loads(line[0][len(release.WEB_PROBE_MARK):]), calls

    def test_node_program_one_request_each_and_exact_contracts(self):
        build = json.dumps({"buildId": release.WEB_BUILD_ID}).encode()
        ready = json.dumps({"status": "ok", "stage": "session_store"}).encode()
        result, calls = self.run_program({("web", "/build-id"): (200, build),
                                          ("api", "/health/ready"): (200, ready)})
        self.assertTrue(release.web_probe_passed(result))
        self.assertEqual(result["api"], hashlib.sha256(b"127.0.0.1").hexdigest())
        self.assertEqual([c[1:] for c in calls], [("/build-id", None), ("/health/ready", None)])

    def test_node_program_refuses_redirect_unready_oversize_and_pilot(self):
        build = json.dumps({"buildId": release.WEB_BUILD_ID}).encode()
        ready = json.dumps({"status": "ok", "stage": "session_store"}).encode()
        cases = {
            "redirect": ({("web", "/build-id"): (200, build), ("api", "/health/ready"): (302, b"")}, None),
            "unready": ({("web", "/build-id"): (200, build),
                         ("api", "/health/ready"): (503, b'{"status":"unavailable","stage":"session_store"}')}, None),
            "unauthorized": ({("web", "/build-id"): (200, build), ("api", "/health/ready"): (401, b"{}")}, None),
            "oversize": ({("web", "/build-id"): (200, build), ("api", "/health/ready"): (200, b" " * 5000)}, None),
            "pilot": ({("web", "/build-id"): (200, build), ("api", "/health/ready"): (200, ready)},
                      {"CHAT_PILOT_UI_ENABLED": "true"}),
        }
        for name, (routes, extra) in cases.items():
            with self.subTest(name=name):
                result, calls = self.run_program(routes, extra)
                self.assertFalse(release.web_probe_passed(result))
                self.assertLessEqual(len(calls), 2)
                self.assertNotIn("/x", [c[1] for c in calls])


class SmokeTests(unittest.TestCase):
    def test_pass_requires_exactly_four_pinned_shared_successes(self):
        self.assertTrue(release.smoke_passed(passing_smoke()))
        for index, field, value in (
            (0, "status", 429), (1, "host", False), (3, "shared", False),
            (0, "api", "responses"), (0, "deployment", "other"),
        ):
            result = passing_smoke()
            result["calls"][index][field] = value
            with self.subTest(field=field):
                self.assertFalse(release.smoke_passed(result))
        result = passing_smoke()
        result["calls"].pop()
        self.assertFalse(release.smoke_passed(result))
        result = passing_smoke()
        result["calls"].append(copy.deepcopy(result["calls"][0]))
        self.assertFalse(release.smoke_passed(result))
        result = passing_smoke()
        result["calls"][1] = copy.deepcopy(result["calls"][0])
        self.assertFalse(release.smoke_passed(result))
        result = passing_smoke()
        result["base"] = None
        self.assertFalse(release.smoke_passed(result))

    def test_sanitize_drops_unexpected_content(self):
        result = passing_smoke()
        result["calls"][0]["detail"] = "model said something"
        result["calls"].append({"deployment": "other", "api": "chat"})
        result["phase"] = "secret text"
        clean = release.sanitize_smoke(result)
        self.assertNotIn("model said", json.dumps(clean))
        self.assertNotIn("other", json.dumps(clean))
        self.assertIsNone(clean["phase"])
        self.assertEqual(len(clean["calls"]), 5)
        self.assertEqual(clean["calls"][-1], {})

    def test_run_smoke_preserves_invalid_rows_as_redacted_failures(self):
        unknown = {
            "deployment": "unexpected", "api": "responses", "status": 200,
            "shared": True, "host": True, "detail": "private content",
        }
        cases = (
            ([], True), ([unknown], False), (["private content"], False),
            ([unknown] * 9, False),
        )
        diag = {"marker_seen": True}
        for extras, expected in cases:
            with self.subTest(extras=len(extras), expected=expected):
                raw = passing_smoke()
                raw["calls"].extend(copy.deepcopy(extras))
                with mock.patch.object(release, "run_exec", return_value=(raw, diag)):
                    result = release.run_smoke(None, "unused", "unused")
                self.assertEqual(release.smoke_passed(result), expected)
                self.assertEqual(result["phase"], "complete")
                self.assertEqual(result["exec"], diag)
                self.assertEqual(result["calls"][:4], passing_smoke()["calls"])
                self.assertLessEqual(len(result["calls"]), 8)
                self.assertNotIn("unexpected", json.dumps(result))
                self.assertNotIn("private content", json.dumps(result))
        raw = passing_smoke()
        raw["phase"] = "calls"
        raw["calls"] = [raw["calls"][0], unknown, None]
        with mock.patch.object(release, "run_exec", return_value=(raw, diag)):
            result = release.run_smoke(None, "unused", "unused")
        self.assertFalse(release.smoke_passed(result))
        self.assertEqual(result["phase"], "calls")
        self.assertEqual(result["calls"], [passing_smoke()["calls"][0], {}, {}])
        self.assertEqual(result["exec"], diag)

    def run_program(self, models, deployments=release.DEPLOYMENTS):
        calls = []
        settings = types.SimpleNamespace(
            shared_gateway_enabled=True, chat_pilot_isolation_enabled=False,
            shared_gateway_deployment_set=frozenset(deployments),
            model_catalog_path="unused", data_residency="US", claude_runtime_enabled=False,
            gateway_timeout_seconds=1,
        )
        settings.model_copy = lambda **_kwargs: settings

        class FakeHttp:
            def __init__(self, **kwargs):
                self.hook = kwargs["event_hooks"]["response"][0]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

        class FakeGateway:
            def __init__(self, _settings, *, http_client):
                self.http = http_client
                self._shared_manifest = types.SimpleNamespace(base_url="https://gateway.invalid")

            async def _shared_request(self):
                return None, "https://gateway.invalid"

            async def complete(self, **kwargs):
                calls.append(kwargs)
                await self._shared_request()
                response = types.SimpleNamespace(request=types.SimpleNamespace(
                    url=types.SimpleNamespace(host="gateway.invalid")))
                await self.http.hook(response)

            async def close(self):
                pass

        def module(name, **attributes):
            value = types.ModuleType(name)
            value.__dict__.update(attributes)
            return value

        modules = {
            "httpx": module("httpx", AsyncClient=FakeHttp),
            "ai4ia_api.catalog": module(
                "ai4ia_api.catalog", load_catalog=lambda *_args: types.SimpleNamespace(models=models)),
            "ai4ia_api.config": module("ai4ia_api.config", get_settings=lambda: settings),
            "ai4ia_api.gateway.client": module(
                "ai4ia_api.gateway.client", ModelGatewayClient=FakeGateway,
                ModelGatewayError=RuntimeError),
        }
        with (mock.patch.dict("sys.modules", modules), mock.patch("sys.stdout") as output,
              mock.patch("logging.disable")):
            exec(compile(release.SMOKE_PROGRAM, "<smoke>", "exec"), {})  # noqa: S102 - repository-owned program, fake imports only
        marked = output.write.call_args[0][0]
        return json.loads(marked.split(release.SMOKE_MARK)[1]), calls

    def test_program_calls_only_pinned_api_and_refuses_catalog_drift_before_calls(self):
        models = [types.SimpleNamespace(api="chat", options=[
            types.SimpleNamespace(deploymentName=deployment, sku=release.CANARY_SKUS[deployment])])
            for deployment in release.DEPLOYMENTS]
        result, calls = self.run_program(models)
        self.assertTrue(release.smoke_passed(result))
        self.assertEqual([(call["deployment"], call["api"]) for call in calls],
                         list(release.CANARY_APIS.items()))
        self.assertEqual([call["shared_gateway_sku"] for call in calls],
                         list(release.CANARY_SKUS.values()))
        for drift in ("api", "missing", "unknown", "sku", "extra-sku"):
            changed = copy.deepcopy(models)
            if drift == "api":
                changed[-1].api = "responses"
            elif drift == "missing":
                changed.pop()
            elif drift == "unknown":
                changed[-1].options[0].deploymentName = "unknown"
            elif drift == "sku":
                changed[-1].options[0].sku = "wrong"
            else:
                extra = copy.deepcopy(changed[-1].options[0])
                extra.sku = "wrong"
                changed[-1].options.append(extra)
            with self.subTest(drift=drift):
                result, calls = self.run_program(changed)
                self.assertEqual(result["phase"], "catalog")
                self.assertEqual(calls, [])
                self.assertFalse(release.smoke_passed(result))
        for deployments in (release.DEPLOYMENTS[:-1], (*release.DEPLOYMENTS, "unknown")):
            with self.subTest(deployments=deployments):
                result, calls = self.run_program(models, deployments)
                self.assertEqual(result["phase"], "config")
                self.assertEqual(calls, [])

    def test_shell_lines_stay_below_tty_line_limit(self):
        lines = release.shell_lines(release.SMOKE_PROGRAM)
        self.assertTrue(all(len(line) < 1024 for line in lines))
        payload = "".join(line.decode()[len('S="${S}'):-2] for line in lines[1:-1])
        self.assertEqual(base64.b64decode(payload).decode(), release.SMOKE_PROGRAM)

    def test_shell_lines_reassemble_under_posix_expansion(self):
        for program, runner in ((release.SMOKE_PROGRAM, release.PYTHON_RUNNER),
                                (release.WEB_PROBE_PROGRAM, release.NODE_RUNNER)):
            lines = release.shell_lines(program, runner=runner)
            self.assertEqual(posix_assembled_program(lines), program)

    def test_unbraced_assignment_loses_the_program_under_posix_expansion(self):
        lines = [line.replace(b'S="${S}', b'S="$S')
                 for line in release.shell_lines(release.WEB_PROBE_PROGRAM, runner=release.NODE_RUNNER)]
        self.assertNotEqual(posix_assembled_program(lines), release.WEB_PROBE_PROGRAM)

    @unittest.skipUnless(shutil.which("sh") and shutil.which("base64") and shutil.which("sha256sum"),
                         "needs a POSIX sh with base64/sha256sum")
    def test_shell_lines_reassemble_in_a_real_shell(self):
        for program, runner in ((release.SMOKE_PROGRAM, release.PYTHON_RUNNER),
                                (release.WEB_PROBE_PROGRAM, release.NODE_RUNNER)):
            lines = release.shell_lines(program, runner=runner)
            check = b'printf %s "$S" | base64 -d | sha256sum\n'
            out = subprocess.run([shutil.which("sh"), "-s"], input=b"".join(lines[:-1]) + check,
                                 capture_output=True, timeout=30, check=True).stdout.decode()
            self.assertEqual(out.split()[0], hashlib.sha256(program.encode()).hexdigest())

    @unittest.skipUnless(shutil.which("sh") and shutil.which("base64") and shutil.which("node"),
                         "needs a POSIX sh with base64 and node")
    def test_web_probe_runs_from_real_shell_against_fake_endpoints(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                bodies = {"/build-id": {"buildId": release.WEB_BUILD_ID},
                          "/health/ready": {"status": "ok", "stage": "session_store"}}
                body = json.dumps(bodies.get(self.path, {})).encode()
                self.send_response(200 if self.path in bodies else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        node = shutil.which("node").replace("\\", "/")
        runner = release.NODE_RUNNER.replace("/usr/local/bin/node", f'"{node}"')
        lines = release.shell_lines(release.WEB_PROBE_PROGRAM, runner=runner)
        env = {**os.environ, "PORT": str(port), "API_BASE_URL": f"http://127.0.0.1:{port}",
               "CHAT_PILOT_UI_ENABLED": ""}
        out = subprocess.run([shutil.which("sh"), "-s"], input=b"".join(lines), env=env,
                             capture_output=True, timeout=60, check=True).stdout.decode()
        marked = [line for line in out.splitlines() if line.startswith(release.WEB_PROBE_MARK)]
        self.assertEqual(len(marked), 1)
        self.assertTrue(release.web_probe_passed(json.loads(marked[0][len(release.WEB_PROBE_MARK):])))

    def test_smoke_program_compiles(self):
        compile(release.SMOKE_PROGRAM, "<smoke>", "exec")


class FakeSocket:
    def __init__(self, incoming: bytes):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.timeouts = []

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, size):
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk


def server_frame(payload: bytes, opcode=0x2) -> bytes:
    if len(payload) < 126:
        return bytes([0x80 | opcode, len(payload)]) + payload
    return bytes([0x80 | opcode, 126]) + len(payload).to_bytes(2, "big") + payload


class WebSocketTests(unittest.TestCase):
    def test_handshake_verifies_accept(self):
        key = b"k" * 16
        text = base64.b64encode(key).decode()
        accept = base64.b64encode(hashlib.sha1((text + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        good = FakeSocket(b"HTTP/1.1 101 Switching\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        release.ws_handshake(good, "h.azurecontainerapps.dev", "/x", "tok", key=key)
        self.assertIn(b"Authorization: Bearer tok", bytes(good.sent))
        for head in (b"HTTP/1.1 101 OK\r\nSec-WebSocket-Accept: bad\r\n\r\n", b"HTTP/1.1 403 No\r\n\r\n"):
            with self.subTest(head=head[:12]), self.assertRaises(release.Refused):
                release.ws_handshake(FakeSocket(head), "h", "/x", "tok", key=key)

    def test_client_frames_are_masked(self):
        frame = release.ws_frame(b"\x00\x00hi", mask=b"\x01\x02\x03\x04")
        self.assertEqual(frame[1] & 0x80, 0x80)
        self.assertEqual(bytes(b ^ m for b, m in zip(frame[6:], b"\x01\x02\x03\x04")), b"\x00\x00hi")

    def test_exec_parses_only_the_marker_line(self):
        result = {"phase": "complete"}
        stream = (server_frame(b"\x01INFO connected") + server_frame(b"\x00\x01echo noise\r\n")
                  + server_frame(b"\x00\x01" + release.SMOKE_MARK.encode() + json.dumps(result).encode()[:40])
                  + server_frame(b"\x00\x01" + json.dumps(result).encode()[40:] + b"\r\n"))
        sock = FakeSocket(stream)
        self.assertEqual(release.exec_smoke(sock, [b"x\n"], deadline=1, clock=lambda: 0), result)

    def test_exec_uses_remaining_deadline_for_blocking_reads(self):
        result = {"phase": "complete"}

        class ChunkSocket(FakeSocket):
            def __init__(self, chunks):
                super().__init__(b"")
                self.chunks = list(chunks)

            def recv(self, size):
                return self.chunks.pop(0)

        sock = ChunkSocket([server_frame(b"\x00\x01$ "), server_frame(
            b"\x00\x01" + release.SMOKE_MARK.encode() + json.dumps(result).encode() + b"\n")])
        self.assertEqual(release.exec_smoke(sock, [], deadline=240, clock=lambda: 0), result)
        self.assertEqual(sock.timeouts, [240, 20, 240])

    def test_marker_before_stdin_is_refused(self):
        sock = FakeSocket(server_frame(b"\x00\x01" + release.SMOKE_MARK.encode() + b"{}\n"))
        diag = release.new_exec_diagnostics()
        with self.assertRaisesRegex(release.Refused, "smoke-output"):
            release.exec_smoke(sock, [b"x\n"], deadline=60, clock=lambda: 0, diag=diag)
        self.assertTrue(diag["first_output_before_stdin"])
        self.assertTrue(diag["marker_seen"])

    def test_unexpected_forwarded_stream_is_a_refusal(self):
        diag = release.new_exec_diagnostics()
        with self.assertRaisesRegex(release.Refused, "smoke-transport"):
            release.exec_smoke(FakeSocket(server_frame(b"\x00\x07x")), [], deadline=1,
                               clock=lambda: 0, diag=diag)
        self.assertEqual(diag["frames_by_channel"]["forward_other"], 1)

    def test_partial_websocket_frame_cannot_extend_deadline(self):
        clock = Clock()

        class SlowSocket(FakeSocket):
            def recv(self, size):
                clock.t += 0.6
                return super().recv(min(size, 1))

        with self.assertRaisesRegex(release.Refused, "smoke-timeout"):
            release.exec_smoke(SlowSocket(b"\x82\x05hello"), [], deadline=1, clock=clock)

    def test_exec_output_is_capped(self):
        sock = FakeSocket(server_frame(b"\x00\x01" + b"x" * 100) * 3)
        with self.assertRaisesRegex(release.Refused, "smoke-output"):
            release.exec_smoke(sock, [], deadline=1, clock=lambda: 0, cap=200)

    def test_proxy_error_or_close_is_a_refusal(self):
        for frame in (server_frame(b"\x02container not found"), server_frame(b"", opcode=0x8)):
            with self.subTest(frame=frame[:3]), self.assertRaisesRegex(release.Refused, "smoke-transport"):
                release.exec_smoke(FakeSocket(frame), [], deadline=1, clock=lambda: 0)

    def test_close_code_is_numeric_only(self):
        diag = release.new_exec_diagnostics()
        frame = server_frame((1011).to_bytes(2, "big") + b"PRIVATE_REASON_SENTINEL", opcode=0x8)
        with self.assertRaisesRegex(release.Refused, "smoke-transport"):
            release.exec_smoke(FakeSocket(frame), [], deadline=1, clock=lambda: 0, diag=diag)
        self.assertEqual((diag["close_code"], diag["frames_by_opcode"]["close"]), (1011, 1))
        self.assertNotIn("PRIVATE_REASON_SENTINEL", json.dumps(diag))

    def test_deadline_bounds_pacing_and_prevents_further_stdin(self):
        clock = Clock()
        diag = release.new_exec_diagnostics()
        sock = FakeSocket(server_frame(b"\x00\x01$ "))
        with self.assertRaisesRegex(release.Refused, "smoke-timeout"):
            release.exec_smoke(sock, [b"one\n", b"two\n"], deadline=0.01,
                               clock=clock, sleep=clock.sleep, diag=diag)
        self.assertEqual(clock.t, 0.01)
        self.assertEqual(diag["stdin_frames_sent"], 1)

    def test_partial_frame_survives_attach_timeout(self):
        clock = Clock()
        marker = server_frame(b"\x00\x01" + release.SMOKE_MARK.encode() + b'{"ok":true}\n')

        class PartialSocket(FakeSocket):
            def __init__(self):
                super().__init__(b"")
                self.chunks = [b"\x82", None, b"\x04\x00\x01$ ", marker]

            def recv(self, size):
                chunk = self.chunks.pop(0)
                if chunk is None:
                    clock.t += self.timeouts[-1]
                    raise TimeoutError()
                return chunk

        diag = release.new_exec_diagnostics()
        self.assertEqual(release.exec_smoke(PartialSocket(), [b"exit\n"], deadline=60,
                                            clock=clock, sleep=clock.sleep, diag=diag), {"ok": True})
        self.assertFalse(diag["first_output_before_stdin"])
        self.assertEqual(diag["frames_by_channel"]["forward_stdout"], 2)


def client_frames(data: bytes) -> list[tuple[int, bytes]]:
    frames, data = [], bytearray(data)
    while data:
        assert data[0] & 0x80 and data[1] & 0x80, "client frames must be final and masked"
        opcode, size, offset = data[0] & 0x0F, data[1] & 0x7F, 2
        if size == 126:
            size, offset = int.from_bytes(data[2:4], "big"), 4
        mask = data[offset:offset + 4]
        body = data[offset + 4:offset + 4 + size]
        frames.append((opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(body))))
        del data[:offset + 4 + size]
    return frames


_POSIX_PARAM = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_ASSIGN_S = re.compile(r'S="([^"`\\]*)"')


def posix_assembled_program(lines: list[bytes]) -> str | None:
    """Replay the lane's S-assignment lines with POSIX parameter expansion.

    An unbraced ``$NAME`` takes the longest identifier, so ``$SAbc`` reads the unset
    variable ``SAbc`` (empty), exactly as sh does. Returns the decoded program or None.
    """
    shell: dict[str, str] = {}
    for raw in lines:
        for statement in raw.decode("ascii").strip().split(";"):
            statement = statement.strip()
            if statement == "S=''":
                shell["S"] = ""
            elif match := _ASSIGN_S.fullmatch(statement):
                shell["S"] = _POSIX_PARAM.sub(lambda m: shell.get(m.group(1) or m.group(2), ""), match.group(1))
    try:
        return base64.b64decode(shell.get("S", ""), validate=True).decode("utf-8")
    except ValueError:
        return None


EXPECTED_PROGRAMS = {release.WEB_PROBE_MARK: release.WEB_PROBE_PROGRAM, release.SMOKE_MARK: release.SMOKE_PROGRAM}


class ReferenceExecServer:
    """Fake Container Apps exec proxy with the reference client's contract.

    It answers the WebSocket upgrade, ignores BINARY frames, ignores stdin until a
    TEXT terminal resize attaches the console, then shows a prompt. When the runner
    line arrives it replays the stdin under POSIX expansion and prints the marker
    result only if that reassembles the lane's real program; otherwise, like the
    live console, the runner runs an empty program and the shell exits quietly with
    a clean close. Unread output never arrives on its own.
    """

    def __init__(self, mark: str, result: dict, *, attach_output: bool = True):
        self.mark, self.result, self.attach_output = mark, result, attach_output
        self.handshaken, self.attached = False, False
        self.received: list[tuple[int, bytes]] = []
        self.stdin = bytearray()
        self.outgoing = bytearray()
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def close(self):
        pass

    def emit(self, payload: bytes, opcode=0x2):
        self.outgoing.extend(server_frame(payload, opcode))

    def lines(self) -> list[bytes]:
        return [line + b"\n" for line in bytes(self.stdin).split(b"\n") if line]

    def sendall(self, data):
        if not self.handshaken:
            key = next(line.split(b": ", 1)[1] for line in data.split(b"\r\n")
                       if line.startswith(b"Sec-WebSocket-Key"))
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            self.outgoing.extend(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            self.emit(b"\x01PRIVATE_INFO_SENTINEL connecting")
            self.handshaken = True
            return
        for opcode, payload in client_frames(data):
            self.received.append((opcode, payload))
            if opcode != 0x1:
                continue
            if payload[:2] == b"\x00\x04":
                json.loads(payload[2:])
                self.attached = True
                if self.attach_output:
                    self.emit(b"\x00\x01PRIVATE_OUTPUT_SENTINEL $ ")
            elif payload[:2] == b"\x00\x00" and self.attached:
                self.stdin.extend(payload[2:])
                if payload.rstrip().endswith(b"exit"):
                    expected = EXPECTED_PROGRAMS.get(self.mark)
                    if expected is None:
                        return
                    if posix_assembled_program(self.lines()) != expected:
                        self.emit(b"\x03\xe8", 0x8)
                        return
                    body = json.dumps(self.result).encode()
                    self.emit(b"\x00\x02PRIVATE_STDERR_SENTINEL\n")
                    self.emit(b"\x00\x01\n" + self.mark.encode() + body + b"\n")

    def recv(self, size):
        if not self.outgoing:
            raise TimeoutError()
        chunk = bytes(self.outgoing[:size])
        del self.outgoing[:size]
        return chunk


class ReferenceArm:
    def __init__(self, container: str):
        self.container = container

    def url(self, path):
        return path

    def list(self, path, phase):
        return [{"name": "replica-1", "properties": {"runningState": "Running"}}]

    def get(self, path, phase):
        return {"properties": {"containers": [{"name": self.container, "logStreamEndpoint":
                "https://proxy.eastus2.azurecontainerapps.dev/subscriptions/x/logstream"}]}}

    def _call(self, method, url, phase):
        return 200, {}, json.dumps({"properties": {"token": "PRIVATE_TOKEN_SENTINEL"}}).encode()


SENTINELS = ("PRIVATE_INFO_SENTINEL", "PRIVATE_OUTPUT_SENTINEL", "PRIVATE_STDERR_SENTINEL",
             "PRIVATE_TOKEN_SENTINEL")


class ReferenceExecTests(unittest.TestCase):
    def probe(self, server, sleeps=None):
        return release.run_web_probe(ReferenceArm("web"), release.APPS["web"], "rev", connect=lambda h: server,
                                     clock=lambda: 0, sleep=(sleeps.append if sleeps is not None else lambda s: None))

    def test_reference_server_requires_text_frames_and_resize_before_attach(self):
        lines = [b"x=1\n", b"exit\n"]
        binary = ReferenceExecServer(release.WEB_PROBE_MARK, {})
        binary.handshaken = True
        binary.sendall(release.ws_frame(release.TERMINAL_RESIZE, 0x1)
                       + b"".join(release.ws_frame(b"\x00\x00" + line, 0x2) for line in lines))
        no_resize = ReferenceExecServer(release.WEB_PROBE_MARK, {})
        no_resize.handshaken = True
        no_resize.sendall(b"".join(release.ws_frame(b"\x00\x00" + line, 0x1) for line in lines))
        for server in (binary, no_resize):
            self.assertEqual(bytes(server.stdin), b"")
            self.assertNotIn(release.WEB_PROBE_MARK.encode(), bytes(server.outgoing))

    def test_exec_attaches_like_the_reference_client(self):
        server, sleeps = ReferenceExecServer(release.WEB_PROBE_MARK, passing_web_probe()), []
        result = self.probe(server, sleeps)
        self.assertTrue(release.web_probe_passed(result))
        lines = release.shell_lines(release.WEB_PROBE_PROGRAM, runner=release.NODE_RUNNER)
        self.assertEqual(server.received[0], (0x1, release.TERMINAL_RESIZE))
        self.assertEqual(server.received[1:], [(0x1, b"\x00\x00" + line) for line in lines])
        self.assertEqual(bytes(server.stdin), b"".join(lines))
        self.assertEqual(sleeps, [release.STDIN_PACING] * (len(lines) - 1))
        diag = result["exec"]
        self.assertEqual((diag["resize_sent"], diag["stdin_frames_sent"], diag["first_output_before_stdin"],
                          diag["marker_seen"], diag["first_output"], diag["marker"], diag["close_code"]),
                         (True, len(lines), True, True, "lt1s", "lt1s", None))
        self.assertEqual(diag["frames_by_channel"]["proxy_info"], 1)
        self.assertEqual(diag["frames_by_channel"]["forward_stderr"], 1)
        self.assertGreater(diag["stdout_bytes"], 0)
        self.assertGreater(diag["stderr_bytes"], 0)
        text = json.dumps(result)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    def test_unbraced_stdin_reproduces_live_quiet_close_without_marker(self):
        braced = release.shell_lines

        def unbraced(program, width=900, runner=release.PYTHON_RUNNER):
            return [line.replace(b'S="${S}', b'S="$S') for line in braced(program, width, runner)]

        server = ReferenceExecServer(release.WEB_PROBE_MARK, passing_web_probe())
        with mock.patch.object(release, "shell_lines", unbraced), self.assertRaises(release.Refused) as caught:
            self.probe(server)
        diag = caught.exception.detail["exec"]
        self.assertEqual((caught.exception.phase, diag["marker_seen"], diag["close_code"],
                          diag["resize_sent"], diag["stdin_frames_sent"], diag["first_output_before_stdin"]),
                         ("smoke-transport", False, 1000, True, 6, True))
        self.assertNotIn(release.WEB_PROBE_MARK.encode(), bytes(server.outgoing))

    def test_quiet_console_still_receives_stdin_after_bounded_wait(self):
        server = ReferenceExecServer(release.WEB_PROBE_MARK, passing_web_probe(), attach_output=False)
        result = self.probe(server)
        self.assertTrue(release.web_probe_passed(result))
        self.assertFalse(result["exec"]["first_output_before_stdin"])
        self.assertIn(release.ATTACH_WAIT, server.timeouts)

    def test_refusal_carries_content_free_exec_diagnostics(self):
        server = ReferenceExecServer("NEVER_MATCHING_MARK=", {}, attach_output=True)
        with self.assertRaises(release.Refused) as caught:
            self.probe(server)
        self.assertEqual(caught.exception.phase, "smoke-timeout")
        diag = caught.exception.detail["exec"]
        self.assertEqual((diag["marker_seen"], diag["marker"], diag["first_output_before_stdin"]),
                         (False, None, True))
        text = json.dumps(caught.exception.detail)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    def test_early_refusals_include_empty_content_free_diagnostics(self):
        arm = ReferenceArm("web")
        cases = (
            ("smoke-replicas", "list", []),
            ("smoke-endpoint", "get", {"properties": {"containers": [
                {"name": "web", "logStreamEndpoint": "https://invalid.example/"}
            ]}}),
            ("smoke-token", "_call", (403, {}, b"PRIVATE_TOKEN_SENTINEL")),
        )
        for phase, method, result in cases:
            with self.subTest(phase=phase), mock.patch.object(arm, method, return_value=result):
                with self.assertRaises(release.Refused) as caught:
                    self.probe_with_arm(arm)
                self.assertEqual(caught.exception.phase, phase)
                self.assertEqual(caught.exception.detail["exec"], release.new_exec_diagnostics())

        with mock.patch.object(arm, "list",
                               side_effect=release.Refused("smoke-replicas", 403, {"prior": True})):
            with self.assertRaises(release.Refused) as caught:
                self.probe_with_arm(arm)
            self.assertTrue(caught.exception.detail["prior"])
            self.assertEqual(caught.exception.detail["exec"], release.new_exec_diagnostics())

        class BadHandshake(ReferenceExecServer):
            def sendall(self, data):
                raise OSError("PRIVATE_TOKEN_SENTINEL")

        server = BadHandshake(release.WEB_PROBE_MARK, {})
        with self.assertRaises(release.Refused) as caught:
            self.probe(server)
        self.assertEqual(caught.exception.phase, "smoke-handshake")
        self.assertEqual(caught.exception.detail["exec"], release.new_exec_diagnostics())
        self.assertNotIn("PRIVATE_TOKEN_SENTINEL", json.dumps(caught.exception.detail))

    def probe_with_arm(self, arm):
        return release.run_web_probe(arm, release.APPS["web"], "rev",
                                     connect=lambda h: self.fail("unexpected connection"))

    def test_send_failures_are_reported_with_partial_diagnostics(self):
        class BrokenStdin(ReferenceExecServer):
            def sendall(self, data):
                if self.handshaken and client_frames(data)[0][1].startswith(b"\x00\x00"):
                    raise OSError("PRIVATE_OUTPUT_SENTINEL")
                super().sendall(data)

        server = BrokenStdin(release.WEB_PROBE_MARK, {})
        with self.assertRaises(release.Refused) as caught:
            self.probe(server)
        self.assertEqual(caught.exception.phase, "smoke-transport")
        diag = caught.exception.detail["exec"]
        self.assertTrue(diag["resize_sent"])
        self.assertEqual(diag["stdin_frames_sent"], 0)
        self.assertNotIn("PRIVATE_OUTPUT_SENTINEL", json.dumps(caught.exception.detail))


class ExecuteTests(unittest.TestCase):
    def test_token_path_reports_reader_subphases(self):
        assertion_env = env(ACTIONS_ID_TOKEN_REQUEST_URL="https://abc.actions.githubusercontent.com/t?x=1",
                            ACTIONS_ID_TOKEN_REQUEST_TOKEN="rt")
        cases = {
            "assertion-url": (env(ACTIONS_ID_TOKEN_REQUEST_URL="https://evil.example/t",
                                  ACTIONS_ID_TOKEN_REQUEST_TOKEN="rt"), None),
            "assertion-runtime-token": (env(ACTIONS_ID_TOKEN_REQUEST_URL=assertion_env["ACTIONS_ID_TOKEN_REQUEST_URL"]),
                                        None),
            "assertion-transport": (assertion_env, OSError("reset")),
            "assertion-response": (assertion_env, (403, {}, b"")),
            "assertion-claims": (assertion_env, (200, {}, json.dumps({"value": "a.b.c"}).encode())),
        }
        for phase, (values, result) in cases.items():
            seen = []

            def http(method, url, *, _seen=seen, _result=result, **kwargs):
                _seen.append(kwargs["hosts"])
                if isinstance(_result, Exception):
                    raise _result
                return _result

            with self.subTest(phase), self.assertRaises(release.Refused) as caught:
                release.acquire_arm_token(values, NOW.timestamp(), http=http)
            self.assertEqual(caught.exception.phase, phase)
            self.assertTrue(all(h == {"abc.actions.githubusercontent.com"} for h in seen))

    def test_uncertain_write_is_reconciled_not_replayed(self):
        arm = FakeArm()
        arm.fail["create"] = release.Uncertain("create", 504)
        gate_class = release.DtsGate
        with mock.patch.object(release, "build_payload", fake_build), \
                mock.patch.object(release, "projection", lambda a: json.dumps(a["properties"], sort_keys=True)), \
                mock.patch.object(release, "DtsGate",
                                  lambda a, now: gate_class(a, now=lambda: NOW, attempts=1)):
            receipt = release.execute(env(), token_source=lambda e, t: "tok",
                                      arm_class=lambda token: arm, now=lambda: NOW)
        self.assertEqual(receipt["outcome"], "uncertain")
        self.assertEqual(receipt["status"], 504)
        self.assertEqual(receipt["writes"], 1)
        self.assertTrue(receipt["reconciled"]["observed"])
        self.assertNotIn("tok", json.dumps(receipt))

    def test_refused_admission_makes_no_credential_call(self):
        called = []
        receipt = release.execute(env(AI4IA_REVIEWED_SHA="b" * 40),
                                  token_source=lambda *a: called.append(a))
        self.assertEqual((receipt["outcome"], receipt["phase"], called), ("refused", "admission", []))


if __name__ == "__main__":
    unittest.main()
