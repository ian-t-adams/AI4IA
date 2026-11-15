"""Fake-only tests for the hosted gateway release lane. No network, no Azure."""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import importlib.util
import json
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
        for d in release.DEPLOYMENTS for api in ("chat", "responses")]}


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

    def run_stage(self, arm, stage="c2-api", smoke=passing_smoke, **extra):
        receipt = {"steps": []}
        values = env(AI4IA_RELEASE_STAGE=stage, **extra)
        gate = release.DtsGate(arm, now=lambda: NOW, attempts=2, wait=1)
        runner = release.Release(arm, values, receipt, gate=gate, now=lambda: NOW, smoke=smoke)
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
        self.assertEqual(len(receipt["smoke"]["calls"]), 8)


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


class CutoverIntegrationTests(unittest.TestCase):
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


class SmokeTests(unittest.TestCase):
    def test_pass_requires_all_eight_shared_successes(self):
        self.assertTrue(release.smoke_passed(passing_smoke()))
        for index, field, value in ((0, "status", 429), (5, "host", False), (7, "shared", False)):
            result = passing_smoke()
            result["calls"][index][field] = value
            with self.subTest(field=field):
                self.assertFalse(release.smoke_passed(result))
        result = passing_smoke()
        result["calls"].pop()
        self.assertFalse(release.smoke_passed(result))

    def test_sanitize_drops_unexpected_content(self):
        result = passing_smoke()
        result["calls"][0]["detail"] = "model said something"
        result["calls"].append({"deployment": "other", "api": "chat"})
        result["phase"] = "secret text"
        clean = release.sanitize_smoke(result)
        self.assertNotIn("model said", json.dumps(clean))
        self.assertIsNone(clean["phase"])
        self.assertEqual(len(clean["calls"]), 8)

    def test_shell_lines_stay_below_tty_line_limit(self):
        lines = release.shell_lines(release.SMOKE_PROGRAM)
        self.assertTrue(all(len(line) < 1024 for line in lines))
        payload = "".join(line.decode()[len('S="$S'):-2] for line in lines[1:-1])
        self.assertEqual(base64.b64decode(payload).decode(), release.SMOKE_PROGRAM)

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
    assert len(payload) < 126
    return bytes([0x80 | opcode, len(payload)]) + payload


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
        sock = FakeSocket(server_frame(
            b"\x00\x01" + release.SMOKE_MARK.encode() + json.dumps(result).encode() + b"\n"))
        self.assertEqual(release.exec_smoke(sock, [], deadline=240, clock=lambda: 0), result)
        self.assertEqual(sock.timeouts, [240, 240])

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
