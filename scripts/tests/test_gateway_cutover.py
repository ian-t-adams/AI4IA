"""Offline request-content proof; no Azure service or model calls."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
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
            "image": "example.azurecr.io/ai4ia/api-test@sha256:" + "a" * 64,
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


def revision_inventory(names, *, observed_at=None, active_name="api--r0"):
    return {
        "observedAt": observed_at or datetime.now(UTC).isoformat(),
        "rows": [
            {
                "active": name == active_name,
                "health": "Healthy",
                "name": name,
                "state": "Succeeded",
            }
            for name in names
        ],
    }


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
            existing_revisions=revision_inventory(["api--r0", "api--legacy"]),
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

    def test_c3_baseline_must_be_the_fresh_r1_off_serving_revision(self):
        app, revision = snapshot("Multiple")
        revision["name"] = "api--r1"
        revision["id"] = RESOURCE + "/revisions/api--r1"
        app["properties"]["latestRevisionName"] = "api--r1"
        app["properties"]["latestReadyRevisionName"] = "api--r1"
        app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": "api--r1", "weight": 100},
        ]
        value = manifest()
        deployment = next(iter(value["models"].values()))["targets"][0]["deployment"]
        rows = revision_inventory(["api--r0", "api--r1"], active_name="api--r1")

        payload = self.build(
            app, revision, stage="C3", suffix="gw22s1",
            deployments=deployment, manifest=json.dumps(value), existing_revisions=rows,
        )

        self.assertEqual(
            payload["properties"]["configuration"]["ingress"]["traffic"],
            [{"revisionName": "api--r1", "weight": 100}],
        )
        candidate = payload["properties"]["template"]
        self.assertEqual(
            candidate["containers"][0]["image"],
            revision["properties"]["template"]["containers"][0]["image"],
        )
        self.assertEqual(candidate["revisionSuffix"], "gw22s1")
        old_revision_serving = copy.deepcopy(app)
        old_revision_serving["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": "api--r0", "weight": 100},
        ]
        with self.assertRaisesRegex(ValueError, "traffic must identify R0"):
            self.build(
                old_revision_serving, revision, stage="C3", suffix="gw22s2",
                deployments=deployment, manifest=json.dumps(value), existing_revisions=rows,
            )
        inactive_r1 = revision_inventory(["api--r0", "api--r1"])
        with self.assertRaisesRegex(ValueError, "revision rows"):
            self.build(
                app, revision, stage="C3", suffix="gw22s2",
                deployments=deployment, manifest=json.dumps(value), existing_revisions=inactive_r1,
            )

    def test_c2_candidate_image_keeps_r0_at_100_and_changes_only_image_and_suffix(self):
        app, revision = snapshot("Multiple")
        original = copy.deepcopy(app)
        old_template = copy.deepcopy(revision["properties"]["template"])
        candidate_image = old_template["containers"][0]["image"].rsplit("@", 1)[0] + "@sha256:" + "b" * 64
        payload = self.build(
            app, revision, stage="C2", suffix="image-1",
            api_image=candidate_image,
            existing_revisions=revision_inventory(["api--r0", "api--legacy"]),
        )
        merged = merge_patch(app, payload)
        self.assertEqual(
            merged["properties"]["configuration"]["activeRevisionsMode"], "Multiple",
        )
        self.assertEqual(
            merged["properties"]["configuration"]["ingress"]["traffic"],
            [{"revisionName": "api--r0", "weight": 100}],
        )
        candidate = merged["properties"]["template"]
        self.assertEqual(candidate["containers"][0]["image"], candidate_image)
        self.assertEqual(candidate["revisionSuffix"], "image-1")
        self.assertEqual(
            candidate["containers"][0]["env"], old_template["containers"][0]["env"],
        )
        candidate = copy.deepcopy(candidate)
        candidate["containers"][0]["image"] = old_template["containers"][0]["image"]
        candidate["revisionSuffix"] = old_template["revisionSuffix"]
        self.assertEqual(candidate, old_template)
        self.assertEqual(app, original)

    def test_c2_rejects_stale_or_ambiguous_revision_and_image_inputs(self):
        app, revision = snapshot("Multiple")
        current_image = revision["properties"]["template"]["containers"][0]["image"]
        candidate_image = current_image.rsplit("@", 1)[0] + "@sha256:" + "b" * 64
        controls = [
            ("mode", {"app_change": ("properties", "configuration", "activeRevisionsMode", "Single")}),
            ("latest", {"app_change": ("properties", "configuration", "ingress", "traffic", [{"latestRevision": True, "weight": 100}])}),
            ("stale", {"app_change": ("properties", "latestReadyRevisionName", "api--old")}),
            ("gateway-enabled", {"env_change": ("AI4IA_SHARED_GATEWAY_ENABLED", {"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "true"})}),
            ("gateway-secret-ref", {"env_change": ("AI4IA_SHARED_GATEWAY_ENABLED", {"name": "AI4IA_SHARED_GATEWAY_ENABLED", "secretRef": "gate"})}),
            ("gateway-value-and-secret-ref", {"env_change": ("AI4IA_SHARED_GATEWAY_ENABLED", {"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "false", "secretRef": "gate"})}),
            ("pilot-enabled", {"env_change": ("AI4IA_CHAT_PILOT_ISOLATION_ENABLED", {"name": "AI4IA_CHAT_PILOT_ISOLATION_ENABLED", "value": "true"})}),
            ("pilot-secret-ref", {"env_change": ("AI4IA_CHAT_PILOT_ISOLATION_ENABLED", {"name": "AI4IA_CHAT_PILOT_ISOLATION_ENABLED", "secretRef": "pilot"})}),
            ("duplicate-selector", {"duplicate_env": "AI4IA_SHARED_GATEWAY_ENABLED"}),
            ("same-image", {"image": current_image}),
            ("tag", {"image": current_image.split("@", 1)[0] + ":mutable"}),
            ("wrong-repository", {"image": "other.azurecr.io/ai4ia/api-test@sha256:" + "b" * 64}),
            ("existing-suffix", {"suffix": "old", "revisions": ["api--r0", "api--old"]}),
            ("missing-r0", {"revisions": ["api--old"]}),
            ("duplicate-revisions", {"revisions": ["api--r0", "api--r0"]}),
            ("candidate-exists", {"revisions": ["api--r0", "api--image-1"]}),
            ("malformed-inventory", {"revisions": ["api--r0", "not-a-revision"]}),
            ("too-many-revisions", {"revisions": ["api--r0", *[f"api--old{i}" for i in range(640)]]}),
        ]
        for label, change in controls:
            changed_app, changed_revision = copy.deepcopy((app, revision))
            revisions = revision_inventory(change.get("revisions", ["api--r0"]))
            image = change.get("image", candidate_image)
            suffix = change.get("suffix", "image-1")
            if "app_change" in change:
                path = change["app_change"]
                target = changed_app
                *keys, value = path
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
            if "env_change" in change:
                name, value = change["env_change"]
                env = changed_app["properties"]["template"]["containers"][0]["env"]
                env[:] = [item for item in env if item["name"] != name]
                env.append(value)
                changed_revision["properties"]["template"]["containers"][0]["env"] = copy.deepcopy(env)
            if "duplicate_env" in change:
                env = changed_app["properties"]["template"]["containers"][0]["env"]
                env.append(copy.deepcopy(next(item for item in env if item["name"] == change["duplicate_env"])))
                changed_revision["properties"]["template"]["containers"][0]["env"] = copy.deepcopy(env)
            with self.subTest(control=label), self.assertRaises(ValueError):
                self.build(
                    changed_app, changed_revision, stage="C2", suffix=suffix,
                    api_image=image, existing_revisions=revisions,
                )

    def test_c2_requires_all_stage_inputs_and_other_stages_reject_them(self):
        app, revision = snapshot("Multiple")
        image = revision["properties"]["template"]["containers"][0]["image"].rsplit("@", 1)[0] + "@sha256:" + "b" * 64
        for kwargs in (
            {"api_image": image},
            {"existing_revisions": revision_inventory(["api--r0"])},
            {"api_image": image, "existing_revisions": revision_inventory(["api--r0"]), "manifest": manifest().__str__()},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.build(app, revision, stage="C2", suffix="image-1", **kwargs)
        with self.assertRaises(ValueError):
            self.build(
                app, revision, stage="C3", suffix="gw22s1", deployments="x",
                manifest=json.dumps(manifest()), api_image=image,
                existing_revisions=revision_inventory(["api--r0"]),
            )
        deployment = next(iter(manifest()["models"].values()))["targets"][0]["deployment"]
        with self.assertRaisesRegex(ValueError, "revision rows"):
            self.build(
                app, revision, stage="C3", suffix="gw22s1",
                deployments=deployment, manifest=json.dumps(manifest()),
            )

    def test_c2_allows_absent_selector_flags_with_private_reader_rows(self):
        app, revision = snapshot("Multiple")
        for item in ("AI4IA_SHARED_GATEWAY_ENABLED",):
            app["properties"]["template"]["containers"][0]["env"] = [
                env for env in app["properties"]["template"]["containers"][0]["env"]
                if env["name"] != item
            ]
            revision["properties"]["template"]["containers"][0]["env"] = copy.deepcopy(
                app["properties"]["template"]["containers"][0]["env"]
            )
        image = revision["properties"]["template"]["containers"][0]["image"].rsplit("@", 1)[0] + "@sha256:" + "b" * 64
        payload = self.build(
            app, revision, stage="C2", suffix="image-2", api_image=image,
            existing_revisions=revision_inventory(["api--r0"]),
        )
        self.assertEqual(payload["properties"]["template"]["containers"][0]["image"], image)

    def test_c2_c3_reject_hand_authored_completeness_claims_and_malformed_rows(self):
        app, revision = snapshot("Multiple")
        value = manifest()
        deployment = next(iter(value["models"].values()))["targets"][0]["deployment"]
        forged = {
            "complete": True,
            "names": ["api--r0"],
            "observedAt": "2099-01-01T00:00:00+00:00",
            "pages": 1,
            "source": "Microsoft.App/containerApps/revisions",
        }
        malformed = (
            forged,
            [{"active": True, "health": "Healthy", "name": "api--r0", "state": "Succeeded", "nextLink": "https://evil.invalid"}],
            revision_inventory(["api--r0", "api--r0"]),
        )
        for rows in malformed:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.build(
                    app, revision, stage="C3", suffix="gw22s1",
                    deployments=deployment, manifest=json.dumps(value), existing_revisions=rows,
                )

    def test_revision_inventory_rejects_naive_times_and_enforces_aware_skew_bounds(self):
        now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
        for observed_at in (
            datetime(2000, 1, 1, tzinfo=UTC).replace(tzinfo=None).isoformat(),
            now.replace(tzinfo=None).isoformat(),
        ):
            with self.subTest(observed_at=observed_at), self.assertRaisesRegex(ValueError, "timezone offset"):
                CUTOVER.validate_revision_rows(
                    revision_inventory(["api--r0"], observed_at=observed_at),
                    "api", "api--r0", "gw22s1", now=now,
                )
        for observed in (now - timedelta(seconds=120), now + timedelta(seconds=30)):
            CUTOVER.validate_revision_rows(
                revision_inventory(["api--r0"], observed_at=observed.isoformat()),
                "api", "api--r0", "gw22s1", now=now,
            )
        for observed in (now - timedelta(seconds=121), now + timedelta(seconds=31)):
            with self.subTest(observed=observed), self.assertRaisesRegex(ValueError, "last 120 seconds"):
                CUTOVER.validate_revision_rows(
                    revision_inventory(["api--r0"], observed_at=observed.isoformat()),
                    "api", "api--r0", "gw22s1", now=now,
                )

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
                    existing_revisions=revision_inventory(["api--r0", "api--legacy"]),
                )

    def test_refuses_wrong_target_tagged_image_pilot_duplicate_env_and_suffix(self):
        for control in ("target", "image", "pilot", "duplicate-env", "mode", "suffix", "existing-suffix"):
            app, revision = snapshot("Multiple")
            value = manifest()
            deployment = next(iter(value["models"].values()))["targets"][0]["deployment"]
            suffix = "gw22s1"
            revisions = ["api--r0", "api--legacy"]
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
            elif control == "existing-suffix":
                revisions.append("api--gw22s1")
            else:
                suffix = "r0"
            revision["properties"]["template"] = copy.deepcopy(app["properties"]["template"])
            with self.subTest(control=control), self.assertRaises(ValueError):
                self.build(
                    app, revision, stage="C3", suffix=suffix,
                    deployments=deployment, manifest=json.dumps(value),
                    existing_revisions=revision_inventory(revisions),
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
