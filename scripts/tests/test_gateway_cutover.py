"""Offline request-content proof; no Azure service or model calls."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
CUTOVER = load_script("gateway_cutover", ROOT / "scripts" / "prepare-gateway-cutover.py")
if str(ROOT / "app" / "api" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "app" / "api" / "src"))
from ai4ia_api import catalog as runtime_catalog  # noqa: E402

RESOURCE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001"
    "/resourceGroups/synthetic/providers/Microsoft.App/containerApps/api"
)
WEB_RESOURCE = RESOURCE.rsplit("/", 1)[0] + "/ca-web-test"
API_CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def synthetic_c3_inputs(
    *, credential="managed-identity", extra_auth_field=False, activation_ready=False,
):
    example_path = ROOT / "app" / "api" / "tests" / "shared_gateway_v21.example.json"
    manifest = json.loads(example_path.read_bytes())
    manifest["contractVersion"] = "2.2.0"
    manifest["status"] = "resolved"
    manifest["gateway"].pop("anthropicBaseUrl")
    manifest["gateway"].pop("videoBaseUrl")
    manifest["gateway"].update({
        "baseUrl": "https://gateway.test/lane/v1",
        "allowedHosts": ["gateway.test"],
        "pathPrefix": "/lane/v1",
    })
    manifest["auth"].update({
        "tenantId": "11111111-2222-4333-8444-555555555555",
        "issuer": "https://login.microsoftonline.com/11111111-2222-4333-8444-555555555555/v2.0",
        "audience": "api://gateway.test",
        "scope": "api://gateway.test/.default",
        "callerClientId": API_CLIENT_ID,
        "credential": credential,
    })
    if extra_auth_field:
        manifest["auth"]["clientSecret"] = "synthetic-secret"
    for capability in manifest["capabilities"].values():
        capability["models"] = {}
    manifest_bytes = (
        json.dumps(manifest, indent=2, ensure_ascii=True).replace("\n", "\r\n") + "\r\n"
    ).encode("utf-8")
    sidecar_bytes = json.dumps({
        "activationReady": activation_ready,
        "applicationChanged": False,
        "modelRequests": 0,
        "baseUrl": manifest["gateway"]["baseUrl"],
        "callerClientId": API_CLIENT_ID,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return manifest_bytes, sidecar_bytes


def pin_synthetic_c3_inputs(manifest_bytes, sidecar_bytes):
    return (
        patch.object(
            CUTOVER, "C3_CLASSIFIED_MANIFESTS",
            {hashlib.sha256(manifest_bytes).hexdigest(): "nonconfidential-operational"},
        ),
        patch.object(
            CUTOVER, "C3_SIDECAR_SHA256", hashlib.sha256(sidecar_bytes).hexdigest(),
        ),
    )


C3_DEPLOYMENTS = b"example-chat-deployment"


def synthetic_runtime_catalog(directory, *, category="chat", sku="GlobalStandard"):
    """Packaged-catalog stand-in so C3 exercises the API's real catalog validator."""
    path = Path(directory) / "model_catalog.json"
    path.write_text(json.dumps({"models": [{
        "id": "example-chat-model", "displayName": "Example chat", "category": category,
        "format": "OpenAI", "options": [{
            "region": "eastus2", "sku": sku, "deploymentName": "example-chat-deployment",
        }],
    }]}), encoding="utf-8")
    return patch.object(runtime_catalog, "_PACKAGED", path)


def c3_snapshot(*, selector=True, extra_env=()):
    """C2's unchanged R1-OFF output: no opt-in list and no manifest reference."""
    app, revision = snapshot("Multiple")
    env = app["properties"]["template"]["containers"][0]["env"]
    if not selector:
        env[:] = [item for item in env if item["name"] != "AI4IA_SHARED_GATEWAY_ENABLED"]
    env.extend([
        {"name": "AI4IA_ENV", "value": "prod"},
        {"name": "AI4IA_AUTH_PROVIDER", "value": "entra"},
        {"name": "AI4IA_CHAT_PILOT_ISOLATION_ENABLED", "value": "false"},
        *copy.deepcopy(list(extra_env)),
    ])
    revision["properties"]["template"]["containers"][0]["env"] = copy.deepcopy(env)
    return app, revision


def snapshot(mode="Single", service="api"):
    resource = RESOURCE if service == "api" else WEB_RESOURCE
    app_name = "api" if service == "api" else "ca-web-test"
    revision_name = app_name + "--r0"
    env = (
        [
            {"name": "AZURE_CLIENT_ID", "value": API_CLIENT_ID},
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

    def build_c3(
        self, app, revision, *, inputs=None, deployments=C3_DEPLOYMENTS, digest=None,
        category="chat", sku="GlobalStandard",
    ):
        manifest_bytes, sidecar_bytes = inputs or synthetic_c3_inputs()
        patches = pin_synthetic_c3_inputs(manifest_bytes, sidecar_bytes)
        with tempfile.TemporaryDirectory() as directory, patches[0], patches[1], \
                synthetic_runtime_catalog(directory, category=category, sku=sku):
            return self.build(
                app, revision, stage="C3", suffix="gateway-on-1",
                existing_revisions=revision_inventory(["api--r0", "api--old"]),
                manifest_bytes=manifest_bytes, classification_sidecar_bytes=sidecar_bytes,
                deployments_bytes=deployments,
                deployments_sha256=digest or hashlib.sha256(deployments).hexdigest(),
            )

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

    def test_c1_web_transitions_single_to_multiple_and_pins_latest_r0_without_template(self):
        app, revision = snapshot("Single", service="web")
        app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"latestRevision": True, "weight": 100},
        ]
        original = copy.deepcopy(app)
        payload = self.build(app, revision, stage="C1", service="web")
        self.assertNotIn("template", payload["properties"])
        expected = copy.deepcopy(original)
        expected["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
        expected["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": "ca-web-test--r0", "weight": 100},
        ]
        self.assertEqual(merge_patch(app, payload), expected)
        self.assertEqual(app, original)

    def test_c1_web_rejects_wrong_mode_name_and_revision_traffic(self):
        app, revision = snapshot("Multiple", service="web")
        with self.assertRaisesRegex(ValueError, "C1 requires Single mode"):
            self.build(app, revision, stage="C1", service="web")

        app, revision = snapshot("Single", service="web")
        app["name"] = "web-test"
        app["id"] = WEB_RESOURCE.rsplit("/", 1)[0] + "/web-test"
        revision["id"] = app["id"] + "/revisions/" + revision["name"]
        with self.assertRaisesRegex(ValueError, "web resource name"):
            self.build(app, revision, stage="C1", service="web")

        app, revision = snapshot("Single", service="web")
        app["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": "ca-web-test--old", "weight": 100},
        ]
        with self.assertRaisesRegex(ValueError, "traffic must identify R0"):
            self.build(app, revision, stage="C1", service="web")

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
        with self.assertRaisesRegex(ValueError, "web service supports only C1 and C2"):
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

    def test_c3_adds_exactly_three_literals_to_r1_off_and_preserves_every_other_field(self):
        manifest_bytes, sidecar_bytes = synthetic_c3_inputs()
        for selector in (True, False):
            app, revision = c3_snapshot(selector=selector)
            original_app = copy.deepcopy(app)
            original_template = copy.deepcopy(revision["properties"]["template"])
            payload = self.build_c3(app, revision, inputs=(manifest_bytes, sidecar_bytes))

            self.assertEqual(app, original_app)
            self.assertEqual(set(payload["properties"]), {"configuration", "template"})
            self.assertEqual(payload["properties"]["configuration"], {
                "activeRevisionsMode": "Multiple",
                "ingress": {"traffic": [{"revisionName": "api--r0", "weight": 100}]},
            })
            expected = CUTOVER.writable_template_projection(original_template)
            expected["revisionSuffix"] = "gateway-on-1"
            env = expected["containers"][0]["env"]
            enabled = {"name": "AI4IA_SHARED_GATEWAY_ENABLED", "value": "true"}
            if selector:
                env[next(
                    i for i, item in enumerate(env)
                    if item["name"] == "AI4IA_SHARED_GATEWAY_ENABLED"
                )] = enabled
            else:
                env.append(enabled)
            env.append({
                "name": "AI4IA_SHARED_GATEWAY_DEPLOYMENTS",
                "value": C3_DEPLOYMENTS.decode("ascii"),
            })
            env.append({
                "name": "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST",
                "value": manifest_bytes.decode("utf-8"),
            })
            candidate = payload["properties"]["template"]
            with self.subTest(selector=selector):
                self.assertEqual(candidate, expected)
                self.assertEqual(
                    candidate["containers"][0]["env"][-1]["value"].encode("utf-8"),
                    manifest_bytes,
                )
                self.assertIn(
                    {"name": "VOICE_UNCHANGED", "secretRef": "voice"},
                    candidate["containers"][0]["env"],
                )
                self.assertFalse(any(
                    item.get("secretRef") == "shared-gateway-activation-manifest"
                    for item in candidate["containers"][0]["env"]
                ))

    def test_c3_refuses_anything_but_the_r1_off_baseline(self):
        controls = {
            "selector-on": c3_snapshot(),
            "selector-secret": c3_snapshot(selector=False, extra_env=[
                {"name": "AI4IA_SHARED_GATEWAY_ENABLED", "secretRef": "selector"},
            ]),
            "selector-case-variant": c3_snapshot(selector=False, extra_env=[
                {"name": "ai4ia_shared_gateway_enabled", "value": "false"},
            ]),
            "deployments-present": c3_snapshot(extra_env=[
                {"name": "AI4IA_SHARED_GATEWAY_DEPLOYMENTS", "value": "example-chat-deployment"},
            ]),
            "deployments-case-variant": c3_snapshot(extra_env=[
                {"name": "ai4ia_shared_gateway_deployments", "value": "example-chat-deployment"},
            ]),
            "manifest-secret-ref": c3_snapshot(extra_env=[{
                "name": "AI4IA_SHARED_GATEWAY_ACTIVATION_MANIFEST",
                "secretRef": "shared-gateway-activation-manifest",
            }]),
            "manifest-secret-other-name": c3_snapshot(extra_env=[
                {"name": "UNRELATED", "secretRef": "shared-gateway-activation-manifest"},
            ]),
            "duplicate-case-insensitive": c3_snapshot(extra_env=[
                {"name": "azure_client_id", "value": API_CLIENT_ID},
            ]),
            "catalog-path": c3_snapshot(extra_env=[
                {"name": "AI4IA_MODEL_CATALOG_PATH", "value": "/tmp/catalog.json"},
            ]),
            "runtime-field-secret": c3_snapshot(extra_env=[
                {"name": "AI4IA_DATA_RESIDENCY", "secretRef": "residency"},
            ]),
        }
        for template in (
            controls["selector-on"][0]["properties"]["template"],
            controls["selector-on"][1]["properties"]["template"],
        ):
            template["containers"][0]["env"][1]["value"] = "true"
        pilot = c3_snapshot()
        for template in (pilot[0]["properties"]["template"], pilot[1]["properties"]["template"]):
            template["containers"][0]["env"][-1]["value"] = "true"
        controls["pilot-on"] = pilot
        local = c3_snapshot()
        for template in (local[0]["properties"]["template"], local[1]["properties"]["template"]):
            template["containers"][0]["env"][3]["value"] = "local"
        controls["local-environment"] = local
        for control, (app, revision) in controls.items():
            with self.subTest(control=control), self.assertRaises(ValueError):
                self.build_c3(app, revision)

    def test_c3_reuses_runtime_catalog_and_manifest_validation(self):
        for control, kwargs in {
            "not-in-manifest": {"deployments": b"example-chat-deployment,unlisted"},
            "not-conversational": {"category": "image"},
            "sku-not-in-manifest": {"sku": "DataZoneStandard"},
        }.items():
            app, revision = c3_snapshot()
            with self.subTest(control=control), self.assertRaisesRegex(
                ValueError, "startup validators",
            ):
                self.build_c3(app, revision, **kwargs)

    def test_c3_requires_the_exact_reviewed_deployment_bytes(self):
        controls = {
            "digest-mismatch": {"digest": "0" * 64},
            "digest-uppercase": {
                "digest": hashlib.sha256(C3_DEPLOYMENTS).hexdigest().upper(),
            },
            "trailing-newline": {"deployments": C3_DEPLOYMENTS + b"\n"},
            "whitespace": {"deployments": b"example-chat-deployment, other"},
            "empty-item": {"deployments": b"example-chat-deployment,"},
            "duplicate": {"deployments": b"example-chat-deployment,Example-Chat-Deployment"},
            "non-ascii": {"deployments": "example-chat-deplöyment".encode("utf-8")},
            "too-long": {"deployments": b",".join([b"a" * 60] * 70)},
        }
        for control, kwargs in controls.items():
            app, revision = c3_snapshot()
            with self.subTest(control=control), self.assertRaisesRegex(
                ValueError, "deployment opt-in",
            ):
                self.build_c3(app, revision, **kwargs)
        app, revision = c3_snapshot()
        with self.assertRaisesRegex(ValueError, "manifest inputs are accepted only for C3"):
            self.build(
                app, revision, stage="C1", deployments_bytes=C3_DEPLOYMENTS,
                deployments_sha256=hashlib.sha256(C3_DEPLOYMENTS).hexdigest(),
            )

    def test_c3_runtime_validation_ignores_process_environment(self):
        app, revision = c3_snapshot()
        with patch.dict(os.environ, {
            "AI4IA_SHARED_GATEWAY_DEPLOYMENTS": "unlisted",
            "AI4IA_MODEL_CATALOG_PATH": str(ROOT / "missing-catalog.json"),
            "AI4IA_CHAT_PILOT_ISOLATION_ENABLED": "true",
        }):
            payload = self.build_c3(app, revision)
        self.assertEqual(
            payload["properties"]["template"]["containers"][0]["env"][-2]["value"],
            C3_DEPLOYMENTS.decode("ascii"),
        )

    def test_c3_runtime_validation_uses_checked_loader_and_restores_cached_module(self):
        fake = types.ModuleType("ai4ia_api.shared_gateway_contract")
        fake.load_shared_gateway_manifest = lambda *args, **kwargs: self.fail(
            "cached, unqualified loader was invoked by runtime validation",
        )
        app, revision = c3_snapshot()
        with patch.dict(sys.modules, {"ai4ia_api.shared_gateway_contract": fake}):
            self.build_c3(app, revision)
            self.assertIs(sys.modules["ai4ia_api.shared_gateway_contract"], fake)
        with patch.dict(sys.modules):
            sys.modules.pop("ai4ia_api.shared_gateway_contract", None)
            app, revision = c3_snapshot()
            self.build_c3(app, revision)
            self.assertNotIn("ai4ia_api.shared_gateway_contract", sys.modules)

    def test_c3_requires_the_exact_qualified_loader_blob(self):
        loader_path = ROOT / "app" / "api" / "src" / "ai4ia_api" / "shared_gateway_contract.py"
        self.assertEqual(
            CUTOVER.git_blob_sha1(loader_path.read_bytes()),
            "cd89beaffe81acb0dd344ddd8a81d1a0c78e890e",
        )

    def test_c3_ignores_preloaded_unqualified_loader(self):
        manifest_bytes, sidecar_bytes = synthetic_c3_inputs()
        patches = pin_synthetic_c3_inputs(manifest_bytes, sidecar_bytes)
        fake = types.ModuleType("ai4ia_api.shared_gateway_contract")
        fake.load_shared_gateway_manifest = lambda *args, **kwargs: self.fail(
            "cached, unqualified loader was invoked",
        )
        with patches[0], patches[1], patch.dict(
            sys.modules, {"ai4ia_api.shared_gateway_contract": fake},
        ):
            _, manifest = CUTOVER.load_c3_contract(manifest_bytes, sidecar_bytes, API_CLIENT_ID)
            self.assertEqual(manifest.contract_version, "2.2.0")
            self.assertIs(sys.modules["ai4ia_api.shared_gateway_contract"], fake)

    def test_c3_executes_checked_loader_bytes_after_disk_swap(self):
        manifest_bytes, sidecar_bytes = synthetic_c3_inputs()
        patches = pin_synthetic_c3_inputs(manifest_bytes, sidecar_bytes)
        qualified = (ROOT / "app" / "api" / "src" / "ai4ia_api" / "shared_gateway_contract.py").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader_path = root / "app" / "api" / "src" / "ai4ia_api" / "shared_gateway_contract.py"
            loader_path.parent.mkdir(parents=True)
            loader_path.write_bytes(qualified)
            original_read = Path.read_bytes
            reads = []

            def swap_after_read(path):
                result = original_read(path)
                if path == loader_path:
                    reads.append(path)
                    loader_path.write_text(
                        "raise AssertionError('swapped loader executed')\n", encoding="utf-8",
                    )
                return result

            package = types.ModuleType("ai4ia_api")
            package.__path__ = [str(loader_path.parent)]
            with patches[0], patches[1], patch.object(CUTOVER, "ROOT", root), patch.object(
                Path, "read_bytes", swap_after_read,
            ), patch.dict(sys.modules, {"ai4ia_api": package}):
                sys.modules.pop("ai4ia_api.shared_gateway_contract", None)
                _, manifest = CUTOVER.load_c3_contract(manifest_bytes, sidecar_bytes, API_CLIENT_ID)
                self.assertEqual(manifest.contract_version, "2.2.0")
                self.assertEqual(reads, [loader_path])

    def test_c3_rejects_hash_sidecar_activation_and_classification_mismatches(self):
        manifest_bytes, sidecar_bytes = synthetic_c3_inputs()
        with self.assertRaisesRegex(ValueError, "exact offline classification"):
            CUTOVER.load_c3_contract(b"{", b"{", API_CLIENT_ID)

        patches = pin_synthetic_c3_inputs(manifest_bytes, sidecar_bytes)
        with patches[0], patches[1], self.assertRaisesRegex(ValueError, "sidecar digest"):
            CUTOVER.load_c3_contract(manifest_bytes, b"{}", API_CLIENT_ID)

        ready_manifest, ready_sidecar = synthetic_c3_inputs(activation_ready=True)
        ready_patches = pin_synthetic_c3_inputs(ready_manifest, ready_sidecar)
        with ready_patches[0], ready_patches[1], self.assertRaisesRegex(
            ValueError, "non-activation state",
        ):
            CUTOVER.load_c3_contract(ready_manifest, ready_sidecar, API_CLIENT_ID)

        classification_patches = pin_synthetic_c3_inputs(manifest_bytes, sidecar_bytes)
        with classification_patches[0], classification_patches[1], patch.object(
            CUTOVER, "C3_CLASSIFIED_MANIFESTS", {},
        ), self.assertRaisesRegex(ValueError, "exact offline classification"):
            CUTOVER.load_c3_contract(manifest_bytes, sidecar_bytes, API_CLIENT_ID)

    def test_c3_loader_rejects_identity_mismatch_extra_credentials_and_wrong_contract(self):
        app, revision = c3_snapshot()
        for template in (app["properties"]["template"], revision["properties"]["template"]):
            template["containers"][0]["env"][0]["value"] = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        with self.assertRaisesRegex(ValueError, "qualified shared gateway contract loader"):
            self.build_c3(app, revision)

        for inputs in (
            synthetic_c3_inputs(credential="client-secret"),
            synthetic_c3_inputs(extra_auth_field=True),
        ):
            app, revision = c3_snapshot()
            with self.subTest(inputs=inputs), self.assertRaisesRegex(
                ValueError, "qualified shared gateway contract loader",
            ):
                self.build_c3(app, revision, inputs=inputs)
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
            self.assertIn("exact offline-classified manifest", result.stderr)
            self.assertNotIn("No such file", result.stderr)
            self.assertFalse(output.exists())

            missing_list = subprocess.run(command + [
                "--manifest", str(Path(directory) / "manifest.json"),
                "--classification-sidecar", str(Path(directory) / "sidecar.json"),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(missing_list.returncode, 1)
            self.assertIn("reviewed deployment list and its digest", missing_list.stderr)
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
