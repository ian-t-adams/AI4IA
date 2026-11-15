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

ROOT = Path(__file__).resolve().parents[2]
CUTOVER = load_script("gateway_cutover", ROOT / "scripts" / "prepare-gateway-cutover.py")
RESOURCE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001"
    "/resourceGroups/synthetic/providers/Microsoft.App/containerApps/api"
)
WEB_RESOURCE = RESOURCE.rsplit("/", 1)[0] + "/ca-web-test"


def snapshot(mode="Single", service="api"):
    resource = RESOURCE if service == "api" else WEB_RESOURCE
    app_name = "api" if service == "api" else "ca-web-test"
    revision_name = app_name + "--r0"
    env = (
        [
            {"name": "AZURE_CLIENT_ID", "value": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
            {"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "false"},
            {"name": "VOICE_UNCHANGED", "secretRef": "voice"},
        ]
        if service == "api" else [
            {"name": "API_BASE_URL", "value": "https://api.example.invalid"},
            {"name": "ENTRA_CLIENT_ID", "value": "ffffffff-aaaa-4bbb-8ccc-dddddddddddd"},
            {"name": "NEXTAUTH_SECRET", "secretRef": "web-auth"},
        ]
    )
    template = {
        "revisionSuffix": "r0",
        "containers": [{
            "name": service,
            "image": f"example.azurecr.io/ai4ia/{service}-test@sha256:" + "a" * 64,
            "env": env,
            "resources": {"cpu": 1, "memory": "2Gi"},
            "probes": [],
        }],
        "scale": {"minReplicas": 1, "maxReplicas": 3},
    }
    app = {
        "id": resource, "name": app_name, "location": "eastus2",
        "properties": {
            "provisioningState": "Succeeded",
            "latestRevisionName": revision_name, "latestReadyRevisionName": revision_name,
            "configuration": {
                "activeRevisionsMode": mode,
                "secrets": [{"name": "web-auth" if service == "web" else "shared-gateway-activation-manifest"}],
                "ingress": {
                    "external": False, "targetPort": 8000,
                    "traffic": [{"revisionName": revision_name, "weight": 100}],
                },
            },
            "template": template,
        },
    }
    revision = {
        "id": resource + "/revisions/" + revision_name, "name": revision_name,
        "properties": {
            "active": True, "healthState": "Healthy", "runningState": "Running",
            "template": copy.deepcopy(template),
        },
    }
    return app, revision


def provider_projection_snapshot(service):
    app, revision = snapshot("Multiple", service=service)
    app_name = f"ca-{service}-test"
    resource = RESOURCE.rsplit("/", 1)[0] + "/" + app_name
    revision_name = app_name + "--azd-1793658361"
    app["name"] = app_name
    app["id"] = resource
    app["properties"]["latestRevisionName"] = revision_name
    app["properties"]["latestReadyRevisionName"] = revision_name
    app["properties"]["configuration"]["ingress"]["traffic"] = [
        {"revisionName": revision_name, "weight": 100},
    ]
    app_template = app["properties"]["template"]
    app_template["revisionSuffix"] = "azd-1793658361"
    app_template["containers"][0]["resources"]["ephemeralStorage"] = "2Gi"
    revision_template = copy.deepcopy(app_template)
    revision_template.pop("revisionSuffix")
    revision_template["containers"][0]["resources"].pop("ephemeralStorage")
    revision.update({
        "id": resource + "/revisions/" + revision_name,
        "name": revision_name,
    })
    revision["properties"]["template"] = revision_template
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
        return CUTOVER.build_payload(app, revision, resource_id=app["id"], **kwargs)

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

    def test_c2_web_stages_only_accepted_image_and_preserves_writable_template(self):
        app, revision = snapshot("Multiple", service="web")
        for value in (app["properties"]["template"], revision["properties"]["template"]):
            value["containers"][0]["resources"]["ephemeralStorage"] = "2Gi"
            value["initContainers"] = [{
                "name": "init",
                "image": "example.azurecr.io/ai4ia/web-init@sha256:" + "c" * 64,
                "resources": {
                    "cpu": 0.1, "memory": "1Gi", "ephemeralStorage": "1Gi",
                },
            }]
        old_template = copy.deepcopy(revision["properties"]["template"])
        original_config = copy.deepcopy(app["properties"]["configuration"])
        candidate_image = old_template["containers"][0]["image"].rsplit("@", 1)[0] + "@sha256:" + "b" * 64
        rows = revision_inventory(
            ["ca-web-test--r0", "ca-web-test--old"], active_name="ca-web-test--r0",
        )
        payload = self.build(
            app, revision, stage="C2", service="web", suffix="web-image-1",
            web_image=candidate_image, existing_revisions=rows,
        )

        merged = merge_patch(app, payload)
        candidate = merged["properties"]["template"]
        expected = CUTOVER.writable_template_projection(old_template)
        expected["revisionSuffix"] = "web-image-1"
        expected["containers"][0]["image"] = candidate_image
        self.assertEqual(candidate, expected)
        self.assertNotIn("ephemeralStorage", candidate["containers"][0]["resources"])
        self.assertNotIn("ephemeralStorage", candidate["initContainers"][0]["resources"])
        self.assertEqual(merged["properties"]["configuration"], original_config)
        self.assertEqual(
            payload["properties"]["configuration"]["ingress"]["traffic"],
            [{"revisionName": "ca-web-test--r0", "weight": 100}],
        )

    def test_api_and_web_accept_only_observed_provider_template_projection(self):
        for service in ("api", "web"):
            app, revision = provider_projection_snapshot(service)
            self.assertNotEqual(
                app["properties"]["template"], revision["properties"]["template"],
            )
            old_template = revision["properties"]["template"]
            old_image = old_template["containers"][0]["image"]
            candidate_image = old_image.rsplit("@", 1)[0] + "@sha256:" + "b" * 64
            revision_name = revision["name"]
            with self.subTest(service=service):
                kwargs = {
                    "stage": "C2",
                    "service": service,
                    "suffix": "candidate-1",
                    "existing_revisions": revision_inventory(
                        [revision_name], active_name=revision_name,
                    ),
                }
                kwargs["api_image" if service == "api" else "web_image"] = candidate_image
                payload = self.build(app, revision, **kwargs)
                expected = CUTOVER.writable_template_projection(old_template)
                expected["revisionSuffix"] = "candidate-1"
                expected["containers"][0]["image"] = candidate_image
                self.assertEqual(payload["properties"]["template"], expected)

                changed = copy.deepcopy(app)
                changed["properties"]["template"]["containers"][0]["env"][0]["value"] = "changed"
                with self.assertRaisesRegex(ValueError, "identical writable template"):
                    self.build(changed, revision, **kwargs)

                malformed = copy.deepcopy(app)
                malformed["properties"]["template"]["containers"][0]["resources"][
                    "ephemeralStorage"
                ] = {"unknown": "provider-field"}
                with self.assertRaises(ValueError):
                    self.build(malformed, revision, **kwargs)

    def test_c2_web_rejects_bad_image_revision_and_service_inputs(self):
        for control in (
            "wrong-repository", "mutable", "same-image", "suffix-collision",
            "stale-inventory", "missing-r0", "inactive-r0", "wrong-container",
            "wrong-image-argument", "wrong-source-environment-repository",
            "wrong-mode", "latest-rule", "stale-latest", "stale-ready",
        ):
            app, revision = snapshot("Multiple", service="web")
            current = revision["properties"]["template"]["containers"][0]["image"]
            candidate = current.rsplit("@", 1)[0] + "@sha256:" + "b" * 64
            names = ["ca-web-test--r0", "ca-web-test--old"]
            active_name = "ca-web-test--r0"
            rows = revision_inventory(names, active_name=active_name)
            suffix = "web-image-1"
            image = candidate
            api_image = ""
            if control == "wrong-repository":
                image = "example.azurecr.io/ai4ia/api-test@sha256:" + "b" * 64
            elif control == "mutable":
                image = "example.azurecr.io/ai4ia/web-test:mutable"
            elif control == "same-image":
                image = current
            elif control == "suffix-collision":
                names.append("ca-web-test--web-image-1")
                rows = revision_inventory(names, active_name=active_name)
            elif control == "stale-inventory":
                rows = revision_inventory(
                    names, active_name=active_name,
                    observed_at=(datetime.now(UTC) - timedelta(seconds=121)).isoformat(),
                )
            elif control == "missing-r0":
                rows = revision_inventory(
                    ["ca-web-test--old"], active_name="ca-web-test--old",
                )
            elif control == "inactive-r0":
                rows = revision_inventory(names, active_name="ca-web-test--old")
            elif control == "wrong-container":
                app["properties"]["template"]["containers"][0]["name"] = "api"
                revision["properties"]["template"]["containers"][0]["name"] = "api"
            elif control == "wrong-image-argument":
                api_image, image = candidate, ""
            elif control == "wrong-source-environment-repository":
                source_image = "example.azurecr.io/ai4ia/web-prod@sha256:" + "a" * 64
                app["properties"]["template"]["containers"][0]["image"] = source_image
                revision["properties"]["template"]["containers"][0]["image"] = source_image
                image = source_image.rsplit("@", 1)[0] + "@sha256:" + "b" * 64
            elif control == "wrong-mode":
                app["properties"]["configuration"]["activeRevisionsMode"] = "Single"
            elif control == "latest-rule":
                app["properties"]["configuration"]["ingress"]["traffic"] = [
                    {"latestRevision": True, "weight": 100},
                ]
            elif control == "stale-latest":
                app["properties"]["latestRevisionName"] = "ca-web-test--old"
            elif control == "stale-ready":
                app["properties"]["latestReadyRevisionName"] = "ca-web-test--old"
            with self.subTest(control=control), self.assertRaises(ValueError):
                self.build(
                    app, revision, stage="C2", service="web", suffix=suffix,
                    api_image=api_image, web_image=image, existing_revisions=rows,
                )

        app, revision = snapshot("Multiple", service="web")
        with self.assertRaisesRegex(ValueError, "classified as nonconfidential"):
            self.build(app, revision, stage="C3", service="web")

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

    def test_c2_requires_all_stage_inputs(self):
        app, revision = snapshot("Multiple")
        image = revision["properties"]["template"]["containers"][0]["image"].rsplit("@", 1)[0] + "@sha256:" + "b" * 64
        for kwargs in (
            {"api_image": image},
            {"existing_revisions": revision_inventory(["api--r0"])},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.build(app, revision, stage="C2", suffix="image-1", **kwargs)

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

    def test_c2_rejects_hand_authored_completeness_claims_and_malformed_rows(self):
        app, revision = snapshot("Multiple")
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
                    app, revision, stage="C2", suffix="image-1",
                    api_image="example.azurecr.io/ai4ia/api-test@sha256:" + "b" * 64,
                    existing_revisions=rows,
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

    def test_c3_is_blocked_without_manifest_classification(self):
        app, revision = snapshot("Multiple")
        with self.assertRaisesRegex(ValueError, "classified as nonconfidential"):
            self.build(app, revision, stage="C3")

    def test_c3_cli_refuses_before_reading_snapshots_or_writing_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "blocked.json"
            command = [
                sys.executable, str(ROOT / "scripts" / "prepare-gateway-cutover.py"),
                "--stage", "C3",
                "--app", str(Path(directory) / "missing-app.json"),
                "--revision", str(Path(directory) / "missing-revision.json"),
                "--resource-id", RESOURCE, "--output", str(output),
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("classified as nonconfidential", result.stderr)
            self.assertNotIn("No such file", result.stderr)
            self.assertFalse(output.exists())

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

    def test_cli_refuses_snapshot_or_payload_paths_inside_repository(self):
        app, revision = snapshot()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            app_path = path / "app.json"
            revision_path = path / "revision.json"
            output_path = path / "patch.json"
            app_path.write_text(json.dumps(app), encoding="utf-8")
            revision_path.write_text(json.dumps(revision), encoding="utf-8")
            blocked_input = ROOT / "__private_snapshot_test__.json"
            blocked_output = ROOT / "__private_payload_test__.json"
            self.assertFalse(blocked_input.exists())
            self.assertFalse(blocked_output.exists())

            for app_arg, output_arg in (
                (blocked_input, output_path),
                (app_path, blocked_output),
            ):
                command = [
                    sys.executable, str(ROOT / "scripts" / "prepare-gateway-cutover.py"),
                    "--stage", "C1", "--app", str(app_arg),
                    "--revision", str(revision_path),
                    "--resource-id", RESOURCE, "--output", str(output_arg),
                ]
                result = subprocess.run(command, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn("must stay outside the repository", result.stderr)
                self.assertNotIn(str(blocked_input), result.stderr)
                self.assertNotIn(str(blocked_output), result.stderr)

            self.assertFalse(blocked_input.exists())
            self.assertFalse(blocked_output.exists())


if __name__ == "__main__":
    unittest.main()
