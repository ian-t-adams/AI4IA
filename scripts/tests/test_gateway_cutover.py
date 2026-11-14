"""Offline request-content proof; no Azure service or model calls."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.tests._loader import load_script
from scripts.tests.test_feature_prereqs import _lane_manifest

ROOT = Path(__file__).resolve().parents[2]
CUTOVER = load_script("gateway_cutover", ROOT / "scripts" / "prepare-gateway-cutover.py")
RESOURCE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001"
    "/resourceGroups/synthetic/providers/Microsoft.App/containerApps/api"
)


def snapshot(mode="Single"):
    template = {
        "revisionSuffix": "r0",
        "containers": [{
            "name": "api",
            "image": "example.invalid/api@sha256:" + "a" * 64,
            "env": [
                {"name": "AZURE_CLIENT_ID", "value": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
                {"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "false"},
                {"name": "VOICE_UNCHANGED", "secretRef": "voice"},
            ],
            "resources": {"cpu": 1, "memory": "2Gi"},
        }],
        "scale": {"minReplicas": 1, "maxReplicas": 3},
    }
    app = {
        "id": RESOURCE, "name": "api", "location": "eastus2",
        "properties": {
            "provisioningState": "Succeeded",
            "latestRevisionName": "api--r0", "latestReadyRevisionName": "api--r0",
            "configuration": {
                "activeRevisionsMode": mode,
                "secrets": [{"name": CUTOVER.SECRET}],
                "ingress": {
                    "external": False, "targetPort": 8000,
                    "traffic": [{"revisionName": "api--r0", "weight": 100}],
                },
            },
            "template": template,
        },
    }
    revision = {
        "id": RESOURCE + "/revisions/api--r0", "name": "api--r0",
        "properties": {
            "active": True, "healthState": "Healthy", "runningState": "Running",
            "template": copy.deepcopy(template),
        },
    }
    return app, revision


def merge_patch(target, patch):
    """RFC 7396 oracle: objects merge; arrays replace atomically in the model."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def manifest():
    return _lane_manifest("resolved")


class CutoverTests(unittest.TestCase):
    def build(self, app, revision, **kwargs):
        return CUTOVER.build_payload(app, revision, resource_id=RESOURCE, **kwargs)

    def test_c1_single_request_preserves_everything_except_mode_and_traffic(self):
        app, revision = snapshot()
        app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"latestRevision": True, "weight": 100},
        ]
        original = copy.deepcopy(app)
        payload = self.build(app, revision, stage="C1")
        self.assertNotIn("template", payload["properties"])
        expected = copy.deepcopy(original)
        expected["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
        expected["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": "api--r0", "weight": 100},
        ]
        self.assertEqual(merge_patch(app, payload), expected)
        self.assertEqual(app, original)

    def test_c3_single_request_pins_r0_and_changes_only_suffix_and_gateway_env(self):
        app, revision = snapshot("Multiple")
        value = manifest()
        deployment = next(iter(value["models"].values()))["targets"][0]["deployment"]
        original = copy.deepcopy(app)
        payload = self.build(
            app, revision, stage="C3", suffix="gw22s1",
            deployments=deployment, manifest=json.dumps(value),
        )
        merged = merge_patch(app, payload)
        self.assertEqual(merged["properties"]["configuration"], original["properties"]["configuration"])
        candidate = merged["properties"]["template"]
        old = original["properties"]["template"]
        env = {item["name"]: item for item in candidate["containers"][0]["env"]}
        self.assertEqual(env["AI4IA_SHARED_GATEWAY_ENABLED"]["value"], "true")
        self.assertEqual(env["AI4IA_SHARED_GATEWAY_DEPLOYMENTS"]["value"], deployment)
        self.assertEqual(
            env["AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST"],
            {"name": "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST", "secretRef": CUTOVER.SECRET},
        )
        self.assertEqual(candidate["containers"][0]["image"], old["containers"][0]["image"])
        self.assertEqual(candidate["scale"], old["scale"])
        candidate = copy.deepcopy(candidate)
        candidate["revisionSuffix"] = old["revisionSuffix"]
        candidate["containers"][0]["env"] = old["containers"][0]["env"]
        self.assertEqual(candidate, old)
        self.assertEqual(app, original)

    def test_rejects_stale_unhealthy_drifted_or_ambiguous_snapshot(self):
        for field in ("latestRevisionName", "latestReadyRevisionName", "provisioningState"):
            app, revision = snapshot()
            app["properties"][field] = "unexpected"
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build(app, revision, stage="C1")
        for field, value in (("active", False), ("healthState", "Unhealthy"), ("runningState", "Stopped")):
            app, revision = snapshot()
            revision["properties"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build(app, revision, stage="C1")
        for traffic in (
            [], [{"revisionName": "other", "weight": 100}],
            [{"revisionName": "api--r0", "weight": 100, "latestRevision": True}],
            [{"revisionName": "api--r0", "weight": 100, "label": "old"}],
        ):
            app, revision = snapshot()
            app["properties"]["configuration"]["ingress"]["traffic"] = traffic
            with self.subTest(traffic=traffic), self.assertRaises(ValueError):
                self.build(app, revision, stage="C1")
        app, revision = snapshot()
        app["properties"]["template"]["scale"]["minReplicas"] = 2
        with self.assertRaises(ValueError):
            self.build(app, revision, stage="C1")

    def test_c3_rejects_latest_rule_missing_secret_and_schema_only_manifest(self):
        for control in ("latest", "secret", "schema-only", "unknown", "empty", "wrong-case"):
            app, revision = snapshot("Multiple")
            value = manifest()
            deployment = next(iter(value["models"].values()))["targets"][0]["deployment"]
            if control == "latest":
                app["properties"]["configuration"]["ingress"]["traffic"] = [
                    {"latestRevision": True, "weight": 100},
                ]
            elif control == "secret":
                app["properties"]["configuration"]["secrets"] = []
            elif control == "schema-only":
                value = _lane_manifest("schema-only")
                app["properties"]["template"]["containers"][0]["env"][0]["value"] = (
                    value["auth"]["callerClientId"]
                )
                revision["properties"]["template"] = copy.deepcopy(app["properties"]["template"])
            elif control == "unknown":
                deployment = "unknown"
            elif control == "empty":
                deployment = ""
            else:
                deployment = deployment.upper()
            with self.subTest(control=control), self.assertRaises(ValueError):
                self.build(
                    app, revision, stage="C3", suffix="gw22s1",
                    deployments=deployment, manifest=json.dumps(value),
                )

    def test_refuses_wrong_target_tagged_image_pilot_and_duplicate_env(self):
        for control in ("target", "image", "pilot", "duplicate-env", "mode", "suffix"):
            app, revision = snapshot("Multiple")
            value = manifest()
            deployment = next(iter(value["models"].values()))["targets"][0]["deployment"]
            suffix = "gw22s1"
            if control == "target":
                app["id"] += "-other"
            elif control == "image":
                app["properties"]["template"]["containers"][0]["image"] = "example.invalid/api:tag"
            elif control == "pilot":
                app["properties"]["template"]["containers"][0]["env"].append(
                    {"name": "AI4IA_CHAT_PILOT_ISOLATION_ENABLED", "value": "true"},
                )
            elif control == "duplicate-env":
                app["properties"]["template"]["containers"][0]["env"].append(
                    {"name": "AZURE_CLIENT_ID", "value": "ambiguous"},
                )
            elif control == "mode":
                app["properties"]["configuration"]["activeRevisionsMode"] = "Single"
            else:
                suffix = "r0"
            revision["properties"]["template"] = copy.deepcopy(app["properties"]["template"])
            with self.subTest(control=control), self.assertRaises(ValueError):
                self.build(
                    app, revision, stage="C3", suffix=suffix,
                    deployments=deployment, manifest=json.dumps(value),
                )

    def test_cli_writes_reviewable_patch_without_overwriting_existing_output(self):
        app, revision = snapshot()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "app.json").write_text(json.dumps(app), encoding="utf-8")
            (path / "revision.json").write_text(json.dumps(revision), encoding="utf-8")
            output = path / "patch.json"
            command = [
                sys.executable, str(ROOT / "scripts" / "prepare-gateway-cutover.py"),
                "--stage", "C1", "--app", str(path / "app.json"),
                "--revision", str(path / "revision.json"),
                "--resource-id", RESOURCE, "--output", str(output),
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), self.build(app, revision, stage="C1"))
            before = output.read_bytes()
            refused = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("Cutover payload refused:", refused.stderr)
            self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
