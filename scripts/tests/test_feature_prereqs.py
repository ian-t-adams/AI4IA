"""Unit tests for scripts/validate-feature-prereqs.py.

This validator is the plan-time gate that `azd provision` and the deploy
workflow run before any resource is touched, so a hole in it is a hole in every
deployment. The cases below pin the invariants that are easiest to regress
silently:

* The realtime Origin allowlist must stay **derived**, never a literal hostname
  in `main.parameters.json`. A hardcoded origin is non-empty, so it passes every
  other check while naming whatever tenant it was written for -- the stack comes
  up green and then rejects every browser on the Voice Live handshake. That was
  a real defect; this test is its regression guard.
* The committed `infra/main.parameters.json` must validate both as it ships
  (dev) and in the configuration a production/new-tenant standup uses
  (`appEnvironment=prod` + `apiAuthProvider=entra`). The prod path is not
  exercised by the default CI invocation, which is how a prod-only failure hid
  before.

stdlib only: the validator is loaded from its path (it is a script, not an
importable module) and driven through a temporary parameters file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-feature-prereqs.py"
DERIVE_SCRIPT = ROOT / "scripts" / "derive-json-transport.py"
REAL_PARAMETERS = ROOT / "infra" / "main.parameters.json"

sys.path.insert(0, str(ROOT / "scripts"))
from _json_transport import TRANSPORTS, TransportError, decode, encode  # noqa: E402

# Minimum environment for a production / new-tenant standup. The committed
# parameters file reads these through ${VAR=default} placeholders.
PROD_ENV = {
    "AZURE_ENV_NAME": "ai4ia-prod",
    "AI4IA_APP_ENVIRONMENT": "prod",
    "AI4IA_AUTH_PROVIDER": "entra",
    "AI4IA_ENTRA_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "AI4IA_ENTRA_AUDIENCE": "api://ai4ia-api",
    "AI4IA_ENTRA_WEB_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    "AI4IA_OWNER": "ai4ia-operations",
    "AI4IA_COST_CENTER": "platform-engineering",
    "AI4IA_APIM_PUBLISHER_EMAIL": "ai4ia-ops@contoso.com",
    "AI4IA_BUDGET_START_DATE": "2026-08-01",
    "AI4IA_ALERT_EMAIL": "ai4ia-alerts@contoso.com",
}
CLAUDE_ENV = {
    "AI4IA_CLAUDE_ORGANIZATION_NAME": "Nomad Analytics",
    "AI4IA_CLAUDE_COUNTRY_CODE": "US",
    "AI4IA_CLAUDE_INDUSTRY": "technology",
}


VALIDATOR = load_script("validate_feature_prereqs", SCRIPT)
DERIVE = load_script("derive_json_transport", DERIVE_SCRIPT)
TRANSPORT = {transport.variable: transport for transport in TRANSPORTS}


def _transported(values: dict[str, str]) -> dict[str, str]:
    """*values* plus the transports deploy.yml derives from its raw JSON variables."""
    derived = {
        TRANSPORT[name].transport_variable: encode(value)
        for name, value in values.items() if name in TRANSPORT
    }
    return {**values, **derived}


@contextmanager
def _environment(**values: str):
    """Run with *values* set and every other AI4IA_* placeholder var cleared.

    The validator resolves ${VAR=default} placeholders from os.environ, so a
    stray AI4IA_* variable inherited from the developer's shell would otherwise
    change what these tests actually assert.
    """
    removed = {k: v for k, v in os.environ.items() if k.startswith(("AI4IA_", "AZURE_"))}
    effective = {**CLAUDE_ENV, **values}
    with patch.dict(os.environ, effective, clear=False):
        for key in removed:
            if key not in effective:
                del os.environ[key]
        yield


@contextmanager
def _large_environment(**values: str):
    """``_environment`` for values past Windows' 32,767-character putenv limit.

    Only this process's own setter has that limit; a child process inherits a
    larger variable intact, so azd and its hooks are unaffected. The validator
    reads ``os.environ`` at call time, which a plain mapping satisfies.
    """
    kept = {k: v for k, v in os.environ.items() if not k.startswith(("AI4IA_", "AZURE_"))}
    with patch.object(os, "environ", {**kept, **CLAUDE_ENV, **values}):
        yield


def _run(
    parameters_file: Path, *, require_deployment_attestation: bool = False
) -> tuple[int, str, str]:
    """Run the validator against *parameters_file*; return (exit code, stdout, stderr)."""
    out, err = StringIO(), StringIO()
    with patch.object(VALIDATOR, "PARAMETERS_FILE", parameters_file):
        with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            code = VALIDATOR.main(
                require_deployment_attestation=require_deployment_attestation
            )
    return code, out.getvalue(), err.getvalue()


def _write_parameters(tmpdir: str, overrides: dict[str, Any]) -> Path:
    raw = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))
    for name, value in overrides.items():
        raw["parameters"][name] = {"value": value}
    path = Path(tmpdir) / "main.parameters.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class GroupPolicyPrerequisiteTests(unittest.TestCase):
    def test_group_policy_and_publication_are_unconfigured_by_default(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(parameters["groupPolicyEnabled"]["value"], "${AI4IA_GROUP_POLICY_ENABLED=false}")
        self.assertEqual(parameters["assetPublishingEnabled"]["value"], "${AI4IA_ASSET_PUBLISHING_ENABLED=false}")
        self.assertEqual(parameters["groupPolicyJsonBase64"]["value"], "${AI4IA_GROUP_POLICY_JSON_B64=}")
        self.assertNotIn("groupPolicyJson", parameters)

    def test_same_enabled_policy_requires_entra_and_bounded_configuration(self) -> None:
        good = {
            "groupPolicyEnabled": True, "assetPublishingEnabled": True,
            "apiAuthProvider": "entra", "entraTenantId": "tenant", "entraAudience": "api://app",
            "entraWebClientId": "web", "groupPolicyJsonBase64": encode('{"version":1,"domains":{}}'),
        }
        cases = [
            ({"apiAuthProvider": "dev"}, "require apiAuthProvider=entra"),
            ({"groupPolicyEnabled": False}, "requires groupPolicyEnabled=true"),
            ({"groupPolicyJsonBase64": encode("")}, "nonempty groupPolicyJson"),
            ({"groupPolicyJsonBase64": encode("not json")}, "valid JSON"),
            ({"groupPolicyJsonBase64": encode('{"version":true}')}, "version-1 object"),
            ({"groupPolicyJsonBase64": encode('{"version":1,"directoryLookup":true}')}, "unsupported top-level"),
        ]
        with tempfile.TemporaryDirectory() as tmp, _environment():
            code, _, err = _run(_write_parameters(tmp, good))
            self.assertEqual(code, 0, err)
            for change, expected in cases:
                with self.subTest(change=change):
                    code, _, err = _run(_write_parameters(tmp, {**good, **change}))
                    self.assertEqual(code, 1)
                    self.assertIn(expected, err)

    def test_distinct_restriction_only_actor_markers_reach_runtime_validation(self) -> None:
        config = {"version": 1}
        for name, subject, categories in (
            ("canaryActor", "synthetic-monitor", ["chat", "chat-fast"]),
            ("evaluationActor", "synthetic-evaluator", ["chat"]),
            ("realtimeCanaryActor", "synthetic-realtime", ["realtime"]),
        ):
            config[name] = {
                "tenantId": "synthetic-tenant", "subject": subject,
                "restrictions": {"models": categories, "spend": {"requestsPerMinute": 2}},
            }
        settings = {
            "groupPolicyEnabled": True, "apiAuthProvider": "entra",
            "entraTenantId": "synthetic-tenant", "entraAudience": "api://synthetic-app",
            "entraWebClientId": "synthetic-web",
        }
        with tempfile.TemporaryDirectory() as tmp, _environment():
            code, _, err = _run(_write_parameters(tmp, {
                **settings, "groupPolicyJsonBase64": encode(json.dumps(config)),
            }))
            self.assertEqual(code, 0, err)
            code, _, err = _run(_write_parameters(tmp, {
                **settings,
                "groupPolicyJsonBase64": encode(json.dumps({**config, "callerProfile": "monitor-canary"})),
            }))
            self.assertEqual(code, 1)
            self.assertIn("unsupported top-level", err)


class WorkflowAutomationPrerequisites(unittest.TestCase):
    def test_default_off_values_are_reachable(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(
            parameters["workflowApprovalsEnabled"]["value"],
            "${AI4IA_WORKFLOW_APPROVALS_ENABLED=false}",
        )
        self.assertEqual(
            parameters["workflowSchedulingEnabled"]["value"],
            "${AI4IA_WORKFLOW_SCHEDULING_ENABLED=false}",
        )

    def test_each_prerequisite_has_a_reachable_allowed_control(self) -> None:
        enabled = {
            "workflowApprovalsEnabled": True, "workflowSchedulingEnabled": True,
            "enableDurableWorkflows": True, "sessionDeletionEnabled": True,
            "sessionDeletionRolloutId": "reviewed-workflow-test",
            "durableWorkflowTimeoutSeconds": 1800,
        }
        cases = (
            ({"workflowApprovalsEnabled": False}, "requires workflowApprovalsEnabled"),
            ({"enableDurableWorkflows": False}, "requires the existing durable workflow host"),
            ({"sessionDeletionEnabled": False}, "requires protocol-v1 session deletion readiness"),
            ({"durableWorkflowTimeoutSeconds": 0}, "finite positive runtime"),
        )
        for denied, message in cases:
            with self.subTest(denied=denied), tempfile.TemporaryDirectory() as tmp, _environment(**PROD_ENV):
                code, _, err = _run(_write_parameters(tmp, {**enabled, **denied}))
                self.assertEqual(code, 1, err)
                self.assertIn(message, err)
                code, _, err = _run(_write_parameters(tmp, enabled))
                self.assertEqual(code, 0, err)


class PhotoAvatarPrerequisiteTests(unittest.TestCase):
    def test_committed_feature_is_default_off_with_conservative_limits(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(parameters["photoAvatarsEnabled"]["value"], "${AI4IA_PHOTO_AVATARS_ENABLED=false}")
        self.assertEqual(parameters["photoAvatarMaxPerUser"]["value"], "${AI4IA_PHOTO_AVATAR_MAX_PER_USER=5}")
        self.assertEqual(
            parameters["photoAvatarMaxCreationsPerDay"]["value"],
            "${AI4IA_PHOTO_AVATAR_MAX_CREATIONS_PER_DAY=5}",
        )
        self.assertEqual(
            parameters["photoAvatarLiveMaxMinutesPerSession"]["value"],
            "${AI4IA_PHOTO_AVATAR_LIVE_MAX_MINUTES_PER_SESSION=10}",
        )
        self.assertEqual(
            parameters["photoAvatarLiveIdleTimeoutSeconds"]["value"],
            "${AI4IA_PHOTO_AVATAR_LIVE_IDLE_TIMEOUT_SECONDS=120}",
        )

    def test_enabled_feature_requires_entra_and_warns_about_live_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _environment(**PROD_ENV):
            code, out, err = _run(_write_parameters(tmp, {"photoAvatarsEnabled": True}))
            self.assertEqual(code, 0, err)
            self.assertIn("CustomAvatar Limited Access capability", out + err)
            code, _, err = _run(_write_parameters(tmp, {"photoAvatarsEnabled": False}))
            self.assertEqual(code, 0, err)
        with tempfile.TemporaryDirectory() as tmp, _environment():
            code, _, err = _run(_write_parameters(tmp, {"photoAvatarsEnabled": True}))
            self.assertEqual(code, 1)
            self.assertIn("photoAvatarsEnabled=true requires apiAuthProvider=entra", err)
            # Control: the same dev configuration with the feature off passes.
            code, _, err = _run(_write_parameters(tmp, {"photoAvatarsEnabled": False}))
            self.assertEqual(code, 0, err)

    def test_limits_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _environment():
            for name in ("photoAvatarMaxPerUser", "photoAvatarMaxCreationsPerDay"):
                for bad in (0, 51, "five"):
                    with self.subTest(name=name, value=bad):
                        code, _, err = _run(_write_parameters(tmp, {name: bad}))
                        self.assertEqual(code, 1)
                        self.assertIn(f"{name} must be an integer from 1 to 50", err)
                code, _, err = _run(_write_parameters(tmp, {name: 50}))
                self.assertEqual(code, 0, err)

    def test_live_session_limits_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _environment():
            for name, low, high in (
                ("photoAvatarLiveMaxMinutesPerSession", 1, 60),
                ("photoAvatarLiveIdleTimeoutSeconds", 30, 900),
            ):
                for bad in (low - 1, high + 1, "ten"):
                    with self.subTest(name=name, value=bad):
                        code, _, err = _run(_write_parameters(tmp, {name: bad}))
                        self.assertEqual(code, 1)
                        self.assertIn(f"{name} must be an integer from {low} to {high}", err)
                for good in (low, high):
                    code, _, err = _run(_write_parameters(tmp, {name: good}))
                    self.assertEqual(code, 0, err)


class StagedRealtimeTests(unittest.TestCase):
    def test_ga_staging_and_selection_require_their_parent_gate(self) -> None:
        cases = (
            (
                {"voiceLiveEnabled": False, "voiceLiveToolsEnabled": False, "realtimeGaEnabled": True},
                {"voiceLiveEnabled": True}, "realtimeGaEnabled=true requires voiceLiveEnabled=true",
            ),
            (
                {"realtimeGaEnabled": False, "realtimeProtocol": "ga"},
                {"realtimeGaEnabled": True}, "realtimeProtocol=ga requires realtimeGaEnabled=true",
            ),
            (
                {"realtimeProtocol": "automatic"},
                {"realtimeProtocol": "preview"}, "realtimeProtocol must be preview or ga",
            ),
        )
        for denied, allowed, message in cases:
            with self.subTest(denied=denied), tempfile.TemporaryDirectory() as tmp, _environment():
                code, _, err = _run(_write_parameters(tmp, denied))
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                code, _, err = _run(_write_parameters(tmp, {**denied, **allowed}))
                self.assertEqual(code, 0, err)

    def test_committed_ga_surface_is_off_and_preview_remains_selected(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(parameters["realtimeGaEnabled"]["value"], "${AI4IA_REALTIME_GA_ENABLED=false}")
        self.assertEqual(parameters["realtimeProtocol"]["value"], "${AI4IA_REALTIME_PROTOCOL=preview}")
        with tempfile.TemporaryDirectory() as tmp, _environment():
            for protocol in ("preview", "ga"):
                code, _, err = _run(_write_parameters(tmp, {
                    "realtimeGaEnabled": True, "realtimeProtocol": protocol,
                }))
                self.assertEqual(code, 0, err)


COMPANION_IMAGE = "crfixture.azurecr.io/ai4ia/companion-ai4ia-fixture@sha256:" + "a" * 64
COMPANION_READY = {
    "companionAppEnabled": True,
    "proxyEventHubTelemetryEnabled": True,
    "companionAppImage": COMPANION_IMAGE,
    "companionAppEntraClientId": "22222222-2222-2222-2222-222222222222",
    "companionAppAdminGroupIds": "33333333-3333-3333-3333-333333333333",
}


class CompanionAppPrerequisiteTests(unittest.TestCase):
    def test_committed_console_is_default_off(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(
            parameters["companionAppEnabled"]["value"], "${AI4IA_COMPANION_APP_ENABLED=false}",
        )
        self.assertEqual(parameters["companionAppImage"]["value"], "${AI4IA_COMPANION_APP_IMAGE=}")
        # Control: a disabled console ignores even a nonsensical image.
        with tempfile.TemporaryDirectory() as tmp, _environment(AZURE_ENV_NAME="ai4ia-fixture"):
            code, _, err = _run(_write_parameters(tmp, {"companionAppImage": "nginx:latest"}))
            self.assertEqual(code, 0, err)

    def test_every_prerequisite_fails_closed_and_the_complete_set_passes(self) -> None:
        cases = (
            ({"proxyEventHubTelemetryEnabled": False}, "requires proxyEventHubTelemetryEnabled=true"),
            ({"companionAppImage": ""}, "requires companionAppImage as a digest reference"),
            ({"companionAppImage": "crfixture.azurecr.io/ai4ia/companion-ai4ia-fixture:latest"},
             "requires companionAppImage as a digest reference"),
            ({"companionAppImage": "docker.io/ai4ia/companion-ai4ia-fixture@sha256:" + "a" * 64},
             "requires companionAppImage as a digest reference"),
            ({"companionAppImage": "crfixture.azurecr.io/ai4ia/companion-other@sha256:" + "a" * 64},
             "must come from this environment's companion repository"),
            ({"companionAppEntraClientId": ""}, "requires companionAppEntraClientId"),
            ({"companionAppAdminGroupIds": ""}, "requires at least one admin group or principal id"),
            ({"companionAppAdminGroupIds": "admins"}, "must be Entra object id GUIDs"),
            ({"companionAppAllowedIpRanges": "0.0.0.0/0"}, "must not allow every address"),
            ({"companionAppAllowedIpRanges": "10.0.0.0/33"}, "must be an IPv4 CIDR"),
            ({"companionAppMinReplicas": "2"}, "companionAppMinReplicas must be 0 or 1"),
        )
        with tempfile.TemporaryDirectory() as tmp, _environment(AZURE_ENV_NAME="ai4ia-fixture"):
            # Control: the complete prerequisite set validates.
            code, _, err = _run(_write_parameters(tmp, COMPANION_READY))
            self.assertEqual(code, 0, err)
            code, _, err = _run(_write_parameters(tmp, {
                **COMPANION_READY,
                "companionAppAdminGroupIds": "",
                "companionAppAdminPrincipalIds": "44444444-4444-4444-4444-444444444444",
                "companionAppAllowedIpRanges": "203.0.113.0/24, 198.51.100.7/32",
                "companionAppMinReplicas": "1",
            }))
            self.assertEqual(code, 0, err)
            for override, message in cases:
                with self.subTest(override=override):
                    code, _, err = _run(_write_parameters(tmp, {**COMPANION_READY, **override}))
                    self.assertEqual(code, 1)
                    self.assertIn(message, err)


class CommittedParametersTests(unittest.TestCase):
    def test_versioned_gateway_staging_is_default_off_and_never_activates_hard_quota(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(parameters["gatewayAttemptsV1Staged"]["value"], "${AI4IA_GATEWAY_ATTEMPTS_V1_STAGED=false}")
        for staged in ("false", "true"):
            with _environment(AI4IA_GATEWAY_ATTEMPTS_V1_STAGED=staged):
                code, out, err = _run(REAL_PARAMETERS)
                self.assertEqual(code, 0, err)
                self.assertEqual("Runtime capability is still unavailable" in out + err, staged == "true")
            with _environment(AI4IA_GATEWAY_ATTEMPTS_V1_STAGED=staged, AI4IA_HARD_QUOTA_ENABLED="true"):
                code, _, err = _run(REAL_PARAMETERS)
                self.assertEqual(code, 1)
                self.assertIn("hardQuotaEnabled=true requires hardQuotaRolloutId", err)

    def test_hard_quota_default_off_and_activation_requires_a_selected_rollout(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(parameters["hardQuotaEnabled"]["value"], "${AI4IA_HARD_QUOTA_ENABLED=false}")
        self.assertEqual(parameters["hardQuotaRolloutId"]["value"], "${AI4IA_HARD_QUOTA_ROLLOUT_ID=}")
        for enabled in ("true", "false"):
            with _environment(AI4IA_HARD_QUOTA_ENABLED=enabled):
                code, _, err = _run(REAL_PARAMETERS)
            if enabled == "true":
                self.assertEqual(code, 1)
                self.assertIn("hardQuotaEnabled=true requires apiAuthProvider=entra", err)
                self.assertIn("hardQuotaEnabled=true requires hardQuotaRolloutId", err)
            else:
                self.assertEqual(code, 0, err)
        for rollout, valid in (("reviewed-request-count-1", True), ("-leading", False), ("a b", False)):
            with _environment(
                **PROD_ENV, AI4IA_HARD_QUOTA_ENABLED="true", AI4IA_HARD_QUOTA_ROLLOUT_ID=rollout,
            ):
                code, out, err = _run(REAL_PARAMETERS)
            self.assertEqual(code, 0 if valid else 1, err)
            self.assertEqual("requires hardQuotaRolloutId" in err, not valid)
            self.assertIn("does not prove writer drain or owner bootstrap", out + err)

    def test_hard_mode_warns_that_policy_spend_limits_stay_soft(self) -> None:
        soft_spend = {"version": 1, "domains": {}, "spend": {"mappings": []}}
        actor_spend = {"version": 1, "domains": {}, "canaryActor": {
            "tenantId": "synthetic-tenant", "subject": "synthetic-monitor",
            "restrictions": {"models": ["chat"], "spend": {"requestsPerMinute": 2}},
        }}
        no_spend = {"version": 1, "domains": {}}
        warning = "Group policy spend limits, including execution-actor restrictions, remain soft"
        for hard, config, expected in (
            ("true", soft_spend, True), ("true", actor_spend, True),
            # Controls: identical parameters without spend limits, or without hard mode.
            ("true", no_spend, False), ("false", soft_spend, False),
        ):
            with self.subTest(hard=hard, config=config), _environment(
                **_transported({
                    **PROD_ENV, "AI4IA_HARD_QUOTA_ENABLED": hard,
                    "AI4IA_HARD_QUOTA_ROLLOUT_ID": "reviewed-request-count-1",
                    "AI4IA_GROUP_POLICY_ENABLED": "true",
                    "AI4IA_GROUP_POLICY_JSON": json.dumps(config),
                })
            ):
                code, out, err = _run(REAL_PARAMETERS)
                self.assertEqual(code, 0, err)
                self.assertEqual(warning in out + err, expected)

    def test_committed_parameters_validate_as_shipped(self) -> None:
        with _environment():
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 0, f"committed main.parameters.json failed validation:\n{err}")

    def test_committed_parameters_validate_for_a_production_standup(self) -> None:
        """The prod/entra path a new-tenant standup uses must also validate.

        CI's default invocation resolves appEnvironment=dev, so a prod-only
        contradiction can sit in the committed parameters unnoticed until the
        deploy that matters.
        """
        with _environment(**PROD_ENV):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 0, f"production configuration failed validation:\n{err}")


class DocumentSearchPrerequisiteTests(unittest.TestCase):
    def test_document_and_search_flags_through_real_azd_placeholders(self) -> None:
        for environment in ("dev", "prod"):
            for enabled, search_enabled in (
                (True, False), (True, True), (False, False), (False, True),
            ):
                with self.subTest(
                    environment=environment, enabled=enabled, search=search_enabled,
                ), _environment(
                    **{
                        **PROD_ENV,
                        "AI4IA_APP_ENVIRONMENT": environment,
                        "AI4IA_DOCUMENT_UNDERSTANDING_ENABLED": str(enabled).lower(),
                        "AI4IA_SEARCH_ENABLED": str(search_enabled).lower(),
                        "AI4IA_DOCUMENT_COMPUTE_ENABLED": "false",
                        "AI4IA_CU_PREVIEW_ENABLED": "false",
                    }
                ):
                    code, _, err = _run(
                        REAL_PARAMETERS, require_deployment_attestation=True,
                    )
                    if enabled and not search_enabled:
                        self.assertEqual(code, 1)
                        self.assertIn(
                            "documentUnderstandingEnabled=true requires searchEnabled=true",
                            err,
                        )
                    else:
                        self.assertEqual(code, 0, err)

    def test_literal_parameters_cannot_bypass_the_search_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _environment():
            path = _write_parameters(
                tmp, {"documentUnderstandingEnabled": True, "searchEnabled": False},
            )
            code, _, err = _run(path)
        self.assertEqual(code, 1)
        self.assertIn("searchEnabled=true", err)

    def _assert_hook_stops(self, shell: str, hook_index: int, stub: str) -> None:
        executable = shutil.which(shell)
        if executable is None:
            self.skipTest(f"{shell} is not installed")
        source = (ROOT / "azure.yaml").read_text(encoding="utf-8")
        preprovision = source.split("  preprovision:\n", 1)[1].split("  postprovision:\n", 1)[0]
        # preprovision is a list; the checks are the windows/posix pair of its
        # second entry (the first derives the JSON transports, see below).
        hooks = re.findall(r"        run: \|\n((?:          .*\n|\n)+)", preprovision)
        self.assertEqual(len(hooks), 2)
        hook = textwrap.dedent(hooks[hook_index])
        for code in (0, 17):
            with self.subTest(shell=shell, exit_code=code):
                command = [executable]
                command.extend(
                    ["-NoProfile", "-NonInteractive", "-Command"]
                    if shell == "pwsh"
                    else ["-c"]
                )
                result = subprocess.run(
                    [*command, stub + "\n" + hook],
                    cwd=ROOT,
                    env={**os.environ, "TEST_PREFLIGHT_EXIT_CODE": str(code)},
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual("REACHED_MODEL_PREFLIGHT" in result.stdout, code == 0)

    def test_windows_hook_stops_before_model_preflight_on_parameter_failure(self) -> None:
        self._assert_hook_stops("pwsh", 0, r"""
function python {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'scripts/validate-feature-prereqs.py') {
        $global:LASTEXITCODE = [int]$env:TEST_PREFLIGHT_EXIT_CODE
    }
    if ($args[0] -eq 'scripts/check-model-availability.py') {
        Write-Output 'REACHED_MODEL_PREFLIGHT'
    }
}
""")

    def test_posix_hook_stops_before_model_preflight_on_parameter_failure(self) -> None:
        self._assert_hook_stops("sh", 1, r"""
python3() {
    if [ "$1" = "scripts/validate-feature-prereqs.py" ]; then
        return "$TEST_PREFLIGHT_EXIT_CODE"
    fi
    if [ "$1" = "scripts/check-model-availability.py" ]; then
        printf '%s\n' 'REACHED_MODEL_PREFLIGHT'
    fi
}
""")

    def test_document_search_flags_reach_ci_bicep_and_backend(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        api = (ROOT / "infra" / "modules" / "api.bicep").read_text(encoding="utf-8")
        for parameter, variable in (
            ("documentUnderstandingEnabled", "AI4IA_DOCUMENT_UNDERSTANDING_ENABLED"),
            ("searchEnabled", "AI4IA_SEARCH_ENABLED"),
        ):
            self.assertEqual(parameters[parameter]["value"], "${" + variable + "=true}")
            self.assertIn(variable + ": ${{ vars." + variable + " }}", workflow)
        self.assertIn("documentUnderstandingEnabled: documentUnderstandingEnabled", main)
        self.assertIn("deploySearch: searchEnabled", main)
        self.assertIn("searchEndpoint: search.outputs.searchEndpoint", main)
        self.assertIn("var documentEnv = documentUnderstandingEnabled ? concat([", api)
        self.assertIn("name: 'AI4IA_DOCUMENT_UNDERSTANDING_ENABLED'", api)
        self.assertIn("name: 'AI4IA_SEARCH_ENDPOINT'", api)
        self.assertIn("value: searchEndpoint", api)


class ContentUnderstandingPreviewTests(unittest.TestCase):
    def test_unsupported_gpt52_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _environment():
            models = json.loads(
                (ROOT / "infra" / "models.json").read_text(encoding="utf-8")
            )
            for model in models["catalog"]:
                if model["name"] == "gpt-5.2":
                    for deployment in model["deployments"]:
                        deployment["version"] = "unsupported"
            models_path = Path(tmp) / "models.json"
            models_path.write_text(json.dumps(models), encoding="utf-8")
            with patch.object(VALIDATOR, "MODELS_FILE", models_path):
                code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("gpt-5.2 2025-12-11", err)

    def test_agentic_id_is_blocked_at_current_50k_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _environment():
            path = _write_parameters(
                tmp,
                {
                    "cuPreviewEnabled": True,
                    "cuAgenticAnalyzerId": "agentic.contract",
                },
            )
            code, _, err = _run(path)
        self.assertEqual(code, 1)
        self.assertIn("400K TPM", err)

    def test_agentic_id_requires_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _environment():
            path = _write_parameters(
                tmp,
                {
                    "cuPreviewEnabled": False,
                    "cuAgenticAnalyzerId": "agentic.contract",
                },
            )
            code, _, err = _run(path)
        self.assertEqual(code, 1)
        self.assertIn("cuPreviewEnabled=true", err)

    def test_agentic_id_is_allowed_at_400k_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _environment():
            models = json.loads(
                (ROOT / "infra" / "models.json").read_text(encoding="utf-8")
            )
            for model in models["catalog"]:
                if model["name"] != "gpt-5.2":
                    continue
                for deployment in model["deployments"]:
                    if (
                        deployment["region"] == "eastus2"
                        and deployment["sku"] == "GlobalStandard"
                    ):
                        deployment["capacity"] = 400
            models_path = Path(tmp) / "models.json"
            models_path.write_text(json.dumps(models), encoding="utf-8")
            parameters_path = _write_parameters(
                tmp,
                {
                    "cuPreviewEnabled": True,
                    "cuAgenticAnalyzerId": "agentic.contract",
                },
            )
            with patch.object(VALIDATOR, "MODELS_FILE", models_path):
                code, _, err = _run(parameters_path)
        self.assertEqual(code, 0, err)


class ClaudeMarketplaceAttestationTests(unittest.TestCase):
    def test_disabled_claude_does_not_require_attestation(self) -> None:
        with _environment(
            **PROD_ENV,
            AI4IA_CLAUDE_ENABLED="false",
            AI4IA_CLAUDE_ORGANIZATION_NAME="",
            AI4IA_CLAUDE_COUNTRY_CODE="",
            AI4IA_CLAUDE_INDUSTRY="",
        ):
            code, _, err = _run(
                REAL_PARAMETERS, require_deployment_attestation=True
            )
        self.assertEqual(code, 0, err)

    def test_real_provision_requires_attestation_even_when_all_values_are_absent(
        self,
    ) -> None:
        with _environment(
            AI4IA_CLAUDE_ENABLED="true",
            AI4IA_CLAUDE_ORGANIZATION_NAME="",
            AI4IA_CLAUDE_COUNTRY_CODE="",
            AI4IA_CLAUDE_INDUSTRY="",
        ):
            code, _, err = _run(
                REAL_PARAMETERS, require_deployment_attestation=True
            )
        self.assertEqual(code, 1)
        self.assertIn("real legal entity", err)
        self.assertIn("uppercase ISO-2", err)
        self.assertIn("lowercase claudeIndustry", err)

    def test_azd_preprovision_always_enables_the_hard_gate(self) -> None:
        azure_yaml = (ROOT / "azure.yaml").read_text(encoding="utf-8")
        self.assertEqual(
            azure_yaml.count(
                "validate-feature-prereqs.py --require-deployment-attestation"
            ),
            2,
        )

    def test_missing_legal_entity_blocks_before_provision(self) -> None:
        with _environment(AI4IA_CLAUDE_ORGANIZATION_NAME=""):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("real legal entity", err)

    def test_placeholder_legal_entity_is_rejected(self) -> None:
        with _environment(AI4IA_CLAUDE_ORGANIZATION_NAME="Your Organization"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("real legal entity", err)

    def test_country_must_be_uppercase_iso2(self) -> None:
        with _environment(AI4IA_CLAUDE_COUNTRY_CODE="us"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("uppercase ISO-2", err)

    def test_industry_must_be_lowercase(self) -> None:
        with _environment(AI4IA_CLAUDE_INDUSTRY="Technology"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("lowercase claudeIndustry", err)

    def test_explicit_attestation_values_pass(self) -> None:
        from scripts.tests._claude_fixture import binding, environment

        with _environment(**_transported({**PROD_ENV, **environment(binding()), **CLAUDE_ENV})):
            code, _, err = _run(
                REAL_PARAMETERS, require_deployment_attestation=True
            )
        self.assertEqual(code, 0, err)


class DeploymentAttestationTests(unittest.TestCase):
    def test_real_provision_rejects_shipped_placeholders_and_silent_budget(self) -> None:
        with _environment(AZURE_ENV_NAME="ai4ia-prod"):
            code, _, err = _run(
                REAL_PARAMETERS, require_deployment_attestation=True
            )
        self.assertEqual(code, 1)
        self.assertIn("shipped placeholder 'ai4ia-operator'", err)
        self.assertIn("AI4IA_COST_CENTER", err)
        self.assertIn("example address", err)
        self.assertIn("AI4IA_BUDGET_START_DATE", err)
        self.assertIn("budget has no notification recipient", err)

    def test_real_provision_accepts_complete_owned_configuration(self) -> None:
        with _environment(**PROD_ENV):
            code, _, err = _run(
                REAL_PARAMETERS, require_deployment_attestation=True
            )
        self.assertEqual(code, 0, err)

    def test_invalid_environment_name_fails_before_arm(self) -> None:
        with _environment(AZURE_ENV_NAME="AI4IA_Production"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("environmentName must be 3-20 lowercase", err)

    def test_budget_start_date_must_be_first_of_a_real_month(self) -> None:
        with _environment(AI4IA_BUDGET_START_DATE="2026-02-15"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("first day of a month", err)

    def test_combined_names_cannot_truncate_the_cosmos_uniqueness_suffix(self) -> None:
        builders = []
        for filename, prefix in (
            ("data.bicep", "cosmos-"),
            ("apimcore.bicep", "apim-mcp-"),
            ("keyvault.bicep", "appcs-"),
            ("eventhubs.bicep", "evhns-"),
        ):
            source = (ROOT / "infra" / "modules" / filename).read_text(encoding="utf-8")
            shape = re.search(rf"take\('({prefix}[^']+)', (\d+)\)", source)
            self.assertIsNotNone(shape, filename)
            assert shape is not None
            builders.append((filename, shape.group(1), int(shape.group(2))))
        for workload, environment_name, valid in (
            ("w" * 10, "e" * 12, True),
            ("w" * 11, "e" * 12, False),
            ("w" * 20, "e" * 20, False),
            ("ai4ia", "slurmfactory", True),
        ):
            with self.subTest(workload=workload, environment=environment_name):
                with _environment(
                    AI4IA_WORKLOAD=workload, AZURE_ENV_NAME=environment_name
                ):
                    code, _, err = _run(REAL_PARAMETERS)
                self.assertEqual(code, 0 if valid else 1, err)
                if not valid:
                    self.assertIn("unique suffix", err)
                    continue
                for filename, template, limit in builders:
                    names = []
                    for suffix in ("a" * 13, "b" * 13):
                        rendered = (
                            template.replace("${workload}", workload)
                            .replace("${environmentName}", environment_name)
                            .replace("${uniqueSuffix}", suffix)
                        )[:limit]
                        self.assertNotIn("${", rendered)
                        self.assertTrue(rendered.endswith(suffix), filename)
                        names.append(rendered)
                    self.assertNotEqual(*names, msg=filename)

    def test_foundry_token_cannot_truncate_regional_account_suffix(self) -> None:
        original = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        source = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        shape = re.search(r"take\('(mf-[^']+)', (\d+)\)", source)
        self.assertIsNotNone(shape)
        assert shape is not None
        template, limit = shape.group(1), int(shape.group(2))
        region = max(original["regions"], key=len)
        environment_name = "e" * 12
        prefix = (
            template.replace("${foundryToken}", "")
            .replace("${environmentName}", environment_name)
            .replace("${r.name}", region)
            .replace("${uniqueSuffix}", "a" * 13)
        )
        self.assertNotIn("${", prefix)
        safe_length = limit - len(prefix)
        for token_length, valid in ((safe_length, True), (safe_length + 1, False)):
            with self.subTest(token_length=token_length), tempfile.TemporaryDirectory() as tmp:
                original["naming"]["foundryToken"] = "f" * token_length
                models_path = Path(tmp) / "models.json"
                models_path.write_text(json.dumps(original), encoding="utf-8")
                with (
                    _environment(AZURE_ENV_NAME=environment_name),
                    patch.object(VALIDATOR, "MODELS_FILE", models_path),
                ):
                    code, _, err = _run(REAL_PARAMETERS)
                self.assertEqual(code, 0 if valid else 1, err)
                if not valid:
                    self.assertIn("Foundry", err)
                    self.assertIn("unique suffix", err)


class PrimaryLocationCatalogTests(unittest.TestCase):
    def test_non_catalog_location_is_rejected_before_provision(self) -> None:
        with _environment(AZURE_LOCATION="moonbase"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("location='moonbase' is not defined in infra/models.json", err)

    def test_catalog_region_not_marked_primary_is_rejected(self) -> None:
        with _environment(AZURE_LOCATION="westus"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("location='westus'", err)
        self.assertIn("not marked primary", err)

    def test_swedencentral_is_a_supported_non_eastus2_primary(self) -> None:
        with _environment(AZURE_LOCATION="swedencentral"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 0, err)

    def test_primary_outputs_require_cu_deployments_even_when_feature_is_disabled(self) -> None:
        models = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        embedding = next(
            model for model in models["catalog"]
            if model["name"] == "text-embedding-3-large"
        )
        embedding["deployments"] = [
            deployment for deployment in embedding["deployments"]
            if deployment["region"] != "swedencentral"
        ]
        with tempfile.TemporaryDirectory() as tmp:
            models_path = Path(tmp) / "models.json"
            models_path.write_text(json.dumps(models), encoding="utf-8")
            parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))
            parameters["parameters"]["documentUnderstandingEnabled"]["value"] = False
            parameters_path = Path(tmp) / "parameters.json"
            parameters_path.write_text(json.dumps(parameters), encoding="utf-8")
            with patch.object(VALIDATOR, "MODELS_FILE", models_path):
                with _environment(AZURE_LOCATION="swedencentral"):
                    code, _, err = _run(parameters_path)
        self.assertEqual(code, 1)
        self.assertIn(
            "text-embedding-3-large/GlobalStandard",
            err,
        )


class BudgetNotificationTests(unittest.TestCase):
    """The budget shipped for months with an empty notifications map.

    budgetAlertEmails is not surfaced in main.parameters.json, so it stayed at
    its [] default and Azure accepted a $1500/month budget that emailed nobody.
    Nothing failed, and the portal renders a silent budget identically to a
    working one. main.bicep now falls back to alertEmail; these lock in that the
    remaining silent case is loud.
    """

    def test_budget_without_any_recipient_warns(self) -> None:
        with _environment():
            code, out, _ = _run(REAL_PARAMETERS)
        self.assertEqual(code, 0, "a silent budget is a warning, not a hard failure")
        self.assertIn("budget has no notification recipient", out)

    def test_alert_email_also_covers_the_budget(self) -> None:
        with _environment(AI4IA_ALERT_EMAIL="ops@example.org"):
            code, out, _ = _run(REAL_PARAMETERS)
        self.assertEqual(code, 0)
        self.assertNotIn(
            "budget has no notification recipient",
            out,
            "alertEmail feeds the budget, so supplying it must silence this warning",
        )


class OwnerPlaceholderTests(unittest.TestCase):
    """The owner guard had gone inert against the value it exists to catch.

    It rejected `ian-t-adams`, an older repo default, while main.bicep actually
    ships `ai4ia-operator`. So a deploy that never set AI4IA_OWNER tagged every
    resource with a placeholder owner and sailed straight past the check meant to
    stop exactly that. Same "configured but inert" shape as the empty budget above:
    the guard existed, ran, and could never fire. These pin both directions.
    """

    def test_shipped_placeholder_owner_warns(self) -> None:
        with _environment():
            code, out, _ = _run(REAL_PARAMETERS)
        self.assertEqual(
            code, 0, "infra-validate runs with no env, so this must not be fatal"
        )
        self.assertIn("owner is still the shipped placeholder", out)

    def test_real_owner_silences_the_warning(self) -> None:
        with _environment(AI4IA_OWNER="platform-team@example.org"):
            code, out, _ = _run(REAL_PARAMETERS)
        self.assertEqual(code, 0)
        self.assertNotIn("owner is still the shipped placeholder", out)


class RealtimeOriginTests(unittest.TestCase):
    def test_literal_realtime_origin_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_parameters(
                tmp, {"realtimeAllowedOrigins": "https://ai4ia.example-tenant.com"}
            )
            with _environment():
                code, _, err = _run(path)
        self.assertEqual(code, 1, "a hardcoded realtime Origin must fail validation")
        self.assertIn("realtimeAllowedOrigins", err)

    def test_placeholder_realtime_origin_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_parameters(
                tmp, {"realtimeAllowedOrigins": "${AI4IA_REALTIME_ALLOWED_ORIGINS=}"}
            )
            with _environment():
                code, _, err = _run(path)
        self.assertEqual(code, 0, f"the azd placeholder form must validate:\n{err}")

    def test_empty_realtime_origin_is_accepted_in_production(self) -> None:
        """Empty is correct now: main.bicep derives the deployed web origins."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_parameters(tmp, {"realtimeAllowedOrigins": ""})
            with _environment(**PROD_ENV):
                code, _, err = _run(path)
        self.assertEqual(code, 0, f"a derived (empty) allowlist must validate in prod:\n{err}")

    def test_extra_origins_supplied_by_variable_are_accepted(self) -> None:
        """The variable adds origins; Bicep unions them with the derived set."""
        with _environment(
            **PROD_ENV, AI4IA_REALTIME_ALLOWED_ORIGINS="https://extra.contoso.com"
        ):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 0, f"supplying extra origins must validate:\n{err}")


class PrivateToolCatalogPrerequisiteTests(unittest.TestCase):
    def test_catalog_without_official_mcp_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_parameters(
                tmp,
                {"enablePrivateToolCatalog": True, "enableOfficialMcp": False},
            )
            with _environment():
                code, _, err = _run(path)
        self.assertEqual(code, 1)
        self.assertIn("requires enableOfficialMcp=true", err)

    def test_catalog_with_official_mcp_passes_that_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_parameters(
                tmp,
                {"enablePrivateToolCatalog": True, "enableOfficialMcp": True},
            )
            with _environment():
                code, _, err = _run(path)
        self.assertEqual(code, 0, err)

class ContradictionTests(unittest.TestCase):
    """Spot-check that the validator still catches contradictions at all.

    scripts/tests/test_gateway_policy.py::FeaturePrerequisiteTests covers the
    individual contradiction rules against synthetic parameter dicts; this only
    proves main() can still return non-zero, so a regression that made it
    unconditionally succeed would not leave every test above green.
    """

    def test_prod_requires_entra(self) -> None:
        with _environment(AI4IA_APP_ENVIRONMENT="prod", AI4IA_AUTH_PROVIDER="dev"):
            code, _, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("apiAuthProvider=entra", err)


class FeaturePrerequisiteTests(unittest.TestCase):
    """Individual feature-gate contradiction rules, driven against minimal parameter dicts.

    These tests create a stripped-down parameters file containing only the params
    relevant to the rule under test (plus Claude attestation defaults), so a gate
    cannot accidentally pass because an unrelated real-parameters value hides it.
    """

    def run_validator(self, parameters: dict[str, object]) -> tuple[int, str]:
        parameters = {
            "claudeOrganizationName": "Example Legal Entity",
            "claudeCountryCode": "US",
            "claudeIndustry": "technology",
            **parameters,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "parameters.json"
            path.write_text(
                json.dumps(
                    {
                        "parameters": {
                            name: {"value": value}
                            for name, value in parameters.items()
                        }
                    }
                ),
                encoding="utf-8",
            )
            original = VALIDATOR.PARAMETERS_FILE
            VALIDATOR.PARAMETERS_FILE = path
            output = StringIO()
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    result = VALIDATOR.main()
            finally:
                VALIDATOR.PARAMETERS_FILE = original
            return result, output.getvalue()

    def test_profiles_reject_shared_key_prerequisite(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "proxyProfilesEnabled": True,
                "proxyProfileProjectionJsonBase64": encode('[{"appId":"app-a"}]'),
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("verified identity-aware application header", output)
        # The projection itself decoded and validated: only the edge blocks.
        self.assertNotIn("proxyProfileProjectionJson", output)

    def test_tool_auto_approval_requires_entra_only_when_enabled(self) -> None:
        parameters: dict[str, object] = {
            "owner": "operator",
            "apimPublisherEmail": "ops@contoso.test",
            "appEnvironment": "dev",
            "apiAuthProvider": "dev",
            "toolAutoApproveEnabled": False,
        }
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 0, output)
        parameters["toolAutoApproveEnabled"] = True
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 1, output)
        self.assertIn("toolAutoApproveEnabled=true requires apiAuthProvider=entra", output)
        parameters.update(
            apiAuthProvider="entra",
            entraTenantId="tenant",
            entraAudience="api-client",
            entraWebClientId="web-client",
        )
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 0, output)

    def test_resumable_deletion_requires_explicit_cutover_and_entra(self) -> None:
        parameters: dict[str, object] = {
            "owner": "operator",
            "apimPublisherEmail": "ops@contoso.test",
            "appEnvironment": "dev",
            "apiAuthProvider": "dev",
            "sessionDeletionEnabled": False,
        }
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 0, output)
        parameters["sessionDeletionEnabled"] = True
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 1, output)
        self.assertIn("requires apiAuthProvider=entra", output)
        self.assertIn("requires sessionDeletionRolloutId", output)
        parameters.update(
            apiAuthProvider="entra", entraTenantId="tenant", entraAudience="api-client",
            entraWebClientId="web-client", sessionDeletionRolloutId="reviewed-cutover",
        )
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 0, output)
        self.assertIn("does not prove worker drain", output)

    def test_priorities_require_worker_reservations(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "proxyPrioritiesEnabled": True,
                "proxyPriorityWorkers": "invalid",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("priority:count format", output)

    def test_image_editing_requires_image_generation(self) -> None:
        parameters: dict[str, object] = {
            "owner": "operator",
            "apimPublisherEmail": "ops@contoso.test",
            "imageGenerationEnabled": False,
            "imageEditingEnabled": False,
        }
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 0, output)
        parameters["imageEditingEnabled"] = True
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 1, output)
        self.assertIn("imageEditingEnabled=true requires imageGenerationEnabled=true", output)
        parameters["imageGenerationEnabled"] = True
        code, output = self.run_validator(parameters)
        self.assertEqual(code, 0, output)

    def test_committed_image_editing_default_is_off_and_reachable(self) -> None:
        parameters = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(
            parameters["imageEditingEnabled"]["value"], "${AI4IA_IMAGE_EDITING_ENABLED=false}",
        )
        with patch.dict(
            "os.environ",
            {"AI4IA_IMAGE_EDITING_ENABLED": "true", "AI4IA_IMAGE_GENERATION_ENABLED": "false"},
            clear=False,
        ):
            result, output = self.run_validator({
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "imageGenerationEnabled": "${AI4IA_IMAGE_GENERATION_ENABLED=false}",
                "imageEditingEnabled": "${AI4IA_IMAGE_EDITING_ENABLED=false}",
            })
        self.assertEqual(result, 1, output)
        self.assertIn("imageEditingEnabled=true requires imageGenerationEnabled=true", output)

    def test_environment_overrides_parameter_placeholder_defaults(self) -> None:
        with patch.dict(
            "os.environ",
            _transported({
                "AI4IA_PROXY_PROFILES_ENABLED": "true",
                "AI4IA_PROXY_PROFILE_PROJECTION_JSON": '[{"appId":"app-a"}]',
            }),
            clear=False,
        ):
            result, output = self.run_validator(
                {
                    "owner": "operator",
                    "apimPublisherEmail": "ops@contoso.test",
                    "proxyProfilesEnabled": "${AI4IA_PROXY_PROFILES_ENABLED=false}",
                    "proxyProfileProjectionJsonBase64": "${AI4IA_PROXY_PROFILE_PROJECTION_JSON_B64=}",
                }
            )
        self.assertEqual(result, 1)
        self.assertIn("verified identity-aware application header", output)
        self.assertNotIn("proxyProfileProjectionJson", output)

    def test_private_data_tier_requires_vnet_isolation(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "dataTierPrivate": True,
                "vnetIsolationEnabled": False,
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("requires vnetIsolationEnabled=true", output)

    def test_speech_voice_live_requires_master_voice_live_gate(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": False,
                "speechVoiceLiveEnabled": True,
                "voiceProviderAllowlist": "azure_openai,speech_voice_live",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("speechVoiceLiveEnabled=true is inert unless voiceLiveEnabled=true", output)

    def test_speech_voice_live_requires_allowlist_membership(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": True,
                "speechVoiceLiveEnabled": True,
                "voiceProviderAllowlist": "azure_openai",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn(
            "requires voiceProviderAllowlist to include speech_voice_live", output
        )

    def test_allowlist_without_enablement_is_rejected(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": True,
                "speechVoiceLiveEnabled": False,
                "voiceProviderAllowlist": "azure_openai,speech_voice_live",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("but speechVoiceLiveEnabled is not true", output)

    def test_allowlist_always_requires_azure_openai(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceProviderAllowlist": "speech_voice_live",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("must always include azure_openai", output)

    def test_default_provider_must_be_allowlisted(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceProviderAllowlist": "azure_openai",
                "voiceDefaultProvider": "speech_voice_live",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("voiceDefaultProvider must be a member of voiceProviderAllowlist", output)

    def test_speech_voice_live_complete_configuration_passes(self) -> None:
        # No realtimeAllowedOrigins here on purpose: main.bicep now derives the
        # allowlist from the web app this deployment creates, and the validator
        # rejects a literal hostname pinned in parameters as tenant-coupled.
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "voiceLiveEnabled": True,
                "speechVoiceLiveEnabled": True,
                "voiceProviderAllowlist": "azure_openai,speech_voice_live",
                "voiceDefaultProvider": "azure_openai",
            }
        )
        self.assertEqual(result, 0)
        self.assertIn("look sane", output)

    def test_speech_voice_live_audience_must_not_be_blanked(self) -> None:
        result, output = self.run_validator(
            {
                "owner": "operator",
                "apimPublisherEmail": "ops@contoso.test",
                "speechVoiceLiveManagedIdentityAudience": "",
            }
        )
        self.assertEqual(result, 1)
        self.assertIn("speechVoiceLiveManagedIdentityAudience must not be blanked out", output)


# The incident value's shape (deploy run 36259812510): strict version-1 JSON with
# quotes, spaces and nesting. Synthetic identifiers only.
INCIDENT_POLICY = json.dumps({
    "version": 1,
    "canaryActor": {
        "tenantId": "00000000-0000-4000-8000-000000000001",
        "subject": "00000000-0000-4000-8000-000000000002",
        "restrictions": {
            "models": ["chat", "chat-fast"],
            "spend": {"requestsPerMinute": 2, "costPerDayMicroUsd": 100000},
        },
    },
})
# Everything the substitution could misread: quotes, backslashes, newlines, a
# JSON-hostile separator, multi-byte text and an azd-looking token.
AWKWARD_JSON = json.dumps(
    {
        "quote": '"', "backslash": "\\", "newline": "a\nb", "separator": "\u2028",
        "unicode": "Zürich 東京 🚀", "token": "${AI4IA_OWNER}", "html": "<&>",
    },
    ensure_ascii=False, indent=2,
)
SECRET_PROJECTION = '[{"appId": "app-a", "label": "synthetic \\"secret\\" projection"}]'
RAW_JSON = {
    "AI4IA_GROUP_POLICY_JSON": INCIDENT_POLICY,
    "AI4IA_CLAUDE_BINDING_JSON": AWKWARD_JSON,
    "AI4IA_PROXY_PROFILE_PROJECTION_JSON": SECRET_PROJECTION,
}
AZD_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(=([^}]*))?\}")


def _azd_resolve(parameters_text: str, env: dict[str, str]) -> dict[str, Any]:
    """Resolve string parameters the way azd 1.29.0 does.

    ``loadParameters`` (cli/azd/pkg/infra/provisioning/bicep/bicep_provider.go)
    marshals each parameter entry to compact JSON, runs drone/envsubst over that
    text, where an empty or unset variable takes the ``=default``, and unmarshals
    the result. The value is inserted verbatim: that is the incident.
    """
    resolved: dict[str, Any] = {}
    for name, entry in json.loads(parameters_text)["parameters"].items():
        marshaled = json.dumps(entry, separators=(",", ":"))

        def substitute(match: re.Match[str]) -> str:
            value = env.get(match.group(1), "")
            return value if value or match.group(2) is None else match.group(3)

        resolved[name] = json.loads(AZD_TOKEN.sub(substitute, marshaled))
    return resolved


def _pre_fix_parameters_text() -> str:
    """The committed parameters with each transport put back in its old raw form."""
    document = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))
    parameters = document["parameters"]
    for transport in TRANSPORTS:
        del parameters[transport.transport_parameter]
        parameters[transport.parameter] = {"value": "${" + transport.variable + "=}"}
    return json.dumps(document, indent=2)


def _policy_of_size(size: int, filler: str = " ") -> str:
    """A version-1 policy of exactly *size* UTF-8 bytes.

    A space pads JSON whitespace; a multi-byte *filler* pads a string value, so
    the byte bound differs from the character count.
    """
    if filler == " ":
        head, tail = '{"version": 1, "domains": {}', "}"
    else:
        head, tail = '{"version": 1, "canaryActor": {"subject": "', '"}}'
    room = size - len((head + tail).encode("utf-8"))
    width = len(filler.encode("utf-8"))
    policy = head + filler * (room // width) + "x" * (room % width) + tail
    assert len(policy.encode("utf-8")) == size, (size, len(policy.encode("utf-8")))
    json.loads(policy)
    return policy


class AzdParameterSubstitutionRegressionTests(unittest.TestCase):
    """JSON-valued variables must survive azd's unescaped parameter substitution."""

    def test_incident_values_cross_azd_through_their_transports(self) -> None:
        resolved = _azd_resolve(
            REAL_PARAMETERS.read_text(encoding="utf-8"), _transported(RAW_JSON)
        )
        for transport in TRANSPORTS:
            with self.subTest(variable=transport.variable):
                carried = resolved[transport.transport_parameter]["value"]
                self.assertEqual(decode(carried), RAW_JSON[transport.variable])
                self.assertNotIn(transport.parameter, resolved)

    def test_the_pre_fix_raw_form_fails_the_same_substitution(self) -> None:
        old = _pre_fix_parameters_text()
        # Control: the emulation parses the old form while no value has a quote.
        self.assertEqual(_azd_resolve(old, {})["groupPolicyJson"]["value"], "")
        self.assertEqual(
            _azd_resolve(old, {"AI4IA_GROUP_POLICY_JSON": "unquoted"})["groupPolicyJson"]["value"],
            "unquoted",
        )
        # Each raw JSON value then breaks it, as azd's json.Unmarshal did.
        for name, value in RAW_JSON.items():
            with self.subTest(variable=name), self.assertRaises(json.JSONDecodeError):
                _azd_resolve(old, {name: value})

    def test_unset_and_empty_variables_keep_the_empty_default(self) -> None:
        text = REAL_PARAMETERS.read_text(encoding="utf-8")
        for env in ({}, {transport.transport_variable: "" for transport in TRANSPORTS}):
            resolved = _azd_resolve(text, env)
            for transport in TRANSPORTS:
                with self.subTest(env=bool(env), variable=transport.variable):
                    self.assertEqual(resolved[transport.transport_parameter]["value"], "")
        self.assertEqual(decode(""), "")

    def test_a_value_at_the_policy_bound_crosses_azd(self) -> None:
        for filler in (" ", "東"):
            policy = _policy_of_size(65536, filler)
            resolved = _azd_resolve(
                REAL_PARAMETERS.read_text(encoding="utf-8"),
                _transported({"AI4IA_GROUP_POLICY_JSON": policy}),
            )
            self.assertEqual(decode(resolved["groupPolicyJsonBase64"]["value"]), policy)

    def test_transports_are_string_parameters_so_azd_takes_this_path(self) -> None:
        # azd parses object/array parameters differently; the emulation above
        # models the string path only.
        bicep = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        for transport in TRANSPORTS:
            with self.subTest(parameter=transport.transport_parameter):
                self.assertRegex(bicep, rf"(?m)^param {transport.transport_parameter} string = ''$")
                self.assertNotRegex(bicep, rf"(?m)^param {transport.parameter}\b")


class JsonTransportCodecTests(unittest.TestCase):
    def test_values_round_trip_exactly_in_a_substitution_safe_alphabet(self) -> None:
        for raw in ("", "{}", " \t", INCIDENT_POLICY, AWKWARD_JSON, SECRET_PROJECTION, "\u2028", "🚀"):
            with self.subTest(raw=raw[:24]):
                carried = encode(raw)
                self.assertEqual(decode(carried), raw)
                self.assertRegex(carried, r"^[A-Za-z0-9+/]*={0,2}$")
                self.assertEqual(json.loads(f'"{carried}"'), carried)
        self.assertEqual(encode(""), "")

    def test_values_near_the_64_kib_policy_bound(self) -> None:
        policy = TRANSPORT["AI4IA_GROUP_POLICY_JSON"]
        self.assertEqual((policy.max_bytes, policy.transport_max_length), (65536, 87384))
        for filler in (" ", "東"):
            for size in (65535, 65536, 65537, 65539):
                with self.subTest(filler=filler, size=size):
                    raw = _policy_of_size(size, filler)
                    carried = encode(raw)
                    self.assertEqual(decode(carried), raw)
                    # The encoding of 65536 bytes fits the ARM @maxLength; 65539 does not.
                    self.assertEqual(len(carried) <= policy.transport_max_length, size < 65539)

    def test_decode_accepts_only_the_canonical_spelling(self) -> None:
        # Controls: the canonical form of each payload decodes.
        self.assertEqual(decode("fn5+"), "~~~")
        self.assertEqual(decode("QQ=="), "A")
        self.assertEqual(decode(encode(INCIDENT_POLICY)), INCIDENT_POLICY)
        canonical = encode(INCIDENT_POLICY)
        for label, value in (
            ("trailing newline", canonical + "\n"),
            ("leading space", " " + canonical),
            ("missing padding", "QQ"),
            ("extra padding", "QQ==="),
            ("url-safe alphabet", "fn5-"),
            ("nonzero padding bits", "QR=="),
            ("raw JSON", INCIDENT_POLICY),
            ("non-ASCII", "ü"),
            ("invalid UTF-8", "//4="),
        ):
            with self.subTest(label), self.assertRaises(TransportError):
                decode(value)

    def test_undecodable_environment_text_is_refused_not_rewritten(self) -> None:
        with self.assertRaises(TransportError):
            encode("\ud800")


class DeriveJsonTransportTests(unittest.TestCase):
    """scripts/derive-json-transport.py, the only writer of transports."""

    def run_github_env(self, values: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "github-env"
            target.write_text("", encoding="utf-8")
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("AI4IA_", "GITHUB_"))
            }
            env.update(values, GITHUB_ENV=str(target))
            result = subprocess.run(
                [sys.executable, str(DERIVE_SCRIPT), "--github-env"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
            )
            return result, target.read_text(encoding="utf-8")

    def test_github_env_masks_the_secret_and_writes_every_transport(self) -> None:
        result, written = self.run_github_env(RAW_JSON)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = dict(line.split("=", 1) for line in written.splitlines())
        self.assertEqual(set(lines), {transport.transport_variable for transport in TRANSPORTS})
        for transport in TRANSPORTS:
            self.assertEqual(decode(lines[transport.transport_variable]), RAW_JSON[transport.variable])
        masked = lines["AI4IA_PROXY_PROFILE_PROJECTION_JSON_B64"]
        self.assertTrue(masked)  # control: there is a secret-derived value to mask
        self.assertEqual(result.stdout.splitlines()[0], f"::add-mask::{masked}")
        self.assertEqual(result.stdout.count(masked), 1)
        self.assertNotIn(masked, result.stderr)
        for transport in TRANSPORTS:
            if not transport.secret:
                self.assertNotIn(lines[transport.transport_variable], result.stdout)
            self.assertNotIn(RAW_JSON[transport.variable], result.stdout + result.stderr)

    def test_the_mask_is_emitted_before_the_secret_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "github-env"
            target.write_text("", encoding="utf-8")
            seen: list[str] = []

            class Recorder(StringIO):
                def write(self, text: str) -> int:
                    if text.startswith("::add-mask::"):
                        seen.append(target.read_text(encoding="utf-8"))
                    return super().write(text)

            code = DERIVE.github_env({**RAW_JSON, "GITHUB_ENV": str(target)}, Recorder())
            self.assertEqual(code, 0)
            self.assertEqual(seen, [""])
            self.assertIn(encode(SECRET_PROJECTION), target.read_text(encoding="utf-8"))

    def test_unset_variables_derive_empty_transports_and_no_mask(self) -> None:
        result, written = self.run_github_env({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            written, "".join(f"{transport.transport_variable}=\n" for transport in TRANSPORTS)
        )
        self.assertNotIn("::add-mask::", result.stdout)

    def test_github_env_refuses_without_a_target_or_with_undecodable_text(self) -> None:
        with redirect_stderr(StringIO()):
            self.assertEqual(DERIVE.github_env(RAW_JSON, StringIO()), 2)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "github-env"
            target.write_text("", encoding="utf-8")
            with self.assertRaises(SystemExit):
                DERIVE.github_env(
                    {"AI4IA_GROUP_POLICY_JSON": "\ud800", "GITHUB_ENV": str(target)}, StringIO()
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "")

    def run_azd_env(
        self, environ: dict[str, str], *, returncode: int = 0, azd: str | None = "/opt/azd/bin/azd",
    ) -> tuple[int, list[tuple[list[str], str, str]], str, str]:
        calls: list[tuple[list[str], str, str]] = []

        def run(command: list[str], check: bool) -> subprocess.CompletedProcess[str]:
            self.assertFalse(check)
            path = command[command.index("--file") + 1]
            calls.append((command, Path(path).read_text(encoding="utf-8"), path))
            return subprocess.CompletedProcess(command, returncode)

        out, err = StringIO(), StringIO()
        with redirect_stderr(err):
            code = DERIVE.azd_env(
                environ, out, run=run, which=lambda name: azd if name == "azd" else None
            )
        return code, calls, out.getvalue(), err.getvalue()

    def test_azd_env_stores_stale_transports_through_a_private_file(self) -> None:
        code, calls, out, err = self.run_azd_env({**RAW_JSON, "AZURE_ENV_NAME": "fixture-env"})
        self.assertEqual(code, 0, err)
        ((command, content, path),) = calls
        self.assertEqual(
            command,
            ["/opt/azd/bin/azd", "env", "set", "--file", path, "--environment", "fixture-env"],
        )
        expected = {t.transport_variable: encode(RAW_JSON[t.variable]) for t in TRANSPORTS}
        self.assertEqual(content, "".join(f"{name}='{value}'\n" for name, value in expected.items()))
        self.assertFalse(Path(path).exists())
        for value in expected.values():
            self.assertNotIn(value, " ".join(command) + out + err)

    def test_azd_env_writes_nothing_when_every_transport_is_current(self) -> None:
        code, calls, out, _ = self.run_azd_env(_transported(RAW_JSON))
        self.assertEqual((code, calls), (0, []))
        self.assertIn("current", out)
        # Control: the identical environment with one stale transport stores it alone.
        stale = {**_transported(RAW_JSON), "AI4IA_GROUP_POLICY_JSON": INCIDENT_POLICY + " "}
        code, calls, _, _ = self.run_azd_env(stale)
        self.assertEqual(code, 0)
        ((_, content, _),) = calls
        self.assertEqual(content, f"AI4IA_GROUP_POLICY_JSON_B64='{encode(INCIDENT_POLICY + ' ')}'\n")

    def test_azd_env_clears_the_transport_of_a_removed_raw_variable(self) -> None:
        code, calls, _, _ = self.run_azd_env({"AI4IA_GROUP_POLICY_JSON_B64": encode(INCIDENT_POLICY)})
        self.assertEqual(code, 0)
        ((command, content, _),) = calls
        self.assertEqual(content, "AI4IA_GROUP_POLICY_JSON_B64=''\n")
        self.assertNotIn("--environment", command)

    def test_azd_env_failures_are_fatal_and_leave_no_file(self) -> None:
        code, calls, _, err = self.run_azd_env(RAW_JSON, returncode=3)
        self.assertEqual(code, 1)
        self.assertIn("exited 3", err)
        self.assertFalse(Path(calls[0][2]).exists())
        code, calls, _, err = self.run_azd_env(RAW_JSON, azd=None)
        self.assertEqual((code, calls), (1, []))
        self.assertIn("not on PATH", err)

    def preprovision_entries(self) -> list[str]:
        source = (ROOT / "azure.yaml").read_text(encoding="utf-8")
        preprovision = source.split("  preprovision:\n", 1)[1].split("  postprovision:\n", 1)[0]
        return re.split(r"(?m)^    - ", preprovision)[1:]

    def test_the_first_preprovision_entry_derives_before_any_check(self) -> None:
        # azd reloads its environment after each entry, so only a SEPARATE earlier
        # entry lets the validator and the parameter file see derived values.
        entries = self.preprovision_entries()
        self.assertEqual(len(entries), 2)
        derive, checks = entries
        self.assertTrue(derive.startswith("windows:\n"), derive)
        self.assertIn("\n      posix:\n", derive)
        self.assertEqual(re.findall(r"(?m)^\s+run: (.*)$", derive), [
            "python scripts/derive-json-transport.py --azd-env; "
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            "python3 scripts/derive-json-transport.py --azd-env",
        ])
        self.assertEqual(derive.count("continueOnError: false"), 2)
        self.assertNotIn("derive-json-transport", checks)
        self.assertEqual(checks.count("validate-feature-prereqs.py --require-deployment-attestation"), 2)

    def _assert_derive_hook_exit(self, shell: str, index: int, stub: str) -> None:
        executable = shutil.which(shell)
        if executable is None:
            self.skipTest(f"{shell} is not installed")
        line = re.findall(r"(?m)^\s+run: (.*)$", self.preprovision_entries()[0])[index]
        flags = ["-NoProfile", "-NonInteractive", "-Command"] if shell == "pwsh" else ["-c"]
        for code in (0, 17):
            with self.subTest(shell=shell, exit_code=code):
                result = subprocess.run(
                    [executable, *flags, stub + "\n" + line], cwd=ROOT, capture_output=True, text=True,
                    env={**os.environ, "TEST_DERIVE_EXIT_CODE": str(code)}, timeout=30,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn("REACHED_DERIVE", result.stdout)

    def test_windows_derive_hook_fails_the_provision_when_derivation_fails(self) -> None:
        self._assert_derive_hook_exit("pwsh", 0, r"""
function python {
    if ($args[0] -eq 'scripts/derive-json-transport.py' -and $args[1] -eq '--azd-env') {
        Write-Output 'REACHED_DERIVE'
    }
    $global:LASTEXITCODE = [int]$env:TEST_DERIVE_EXIT_CODE
}
""")

    def test_posix_derive_hook_fails_the_provision_when_derivation_fails(self) -> None:
        self._assert_derive_hook_exit("sh", 1, r"""
python3() {
    if [ "$1" = "scripts/derive-json-transport.py" ] && [ "$2" = "--azd-env" ]; then
        printf '%s\n' 'REACHED_DERIVE'
    fi
    return "$TEST_DERIVE_EXIT_CODE"
}
""")


class JsonTransportValidationTests(unittest.TestCase):
    """validate-feature-prereqs.py checks the transport azd will actually pass."""

    def transport_errors(self, parameters: dict[str, Any], env: dict[str, str]) -> list[str]:
        errors: list[str] = []
        with _environment(**env):
            decoded = VALIDATOR.transported_json(parameters, errors)
        self.assertEqual(set(decoded), {transport.parameter for transport in TRANSPORTS})
        return errors

    def committed(self) -> dict[str, Any]:
        return json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))["parameters"]

    def test_committed_parameters_read_only_transports(self) -> None:
        parameters = self.committed()
        for transport in TRANSPORTS:
            with self.subTest(variable=transport.variable):
                self.assertEqual(
                    parameters[transport.transport_parameter]["value"],
                    "${" + transport.transport_variable + "=}",
                )
                self.assertNotIn(transport.parameter, parameters)
        self.assertNotRegex(REAL_PARAMETERS.read_text(encoding="utf-8"), r"\$\{[A-Z0-9_]+_JSON[=}]")
        self.assertEqual(self.transport_errors(parameters, {}), [])

    def test_each_transport_must_carry_its_raw_variable_exactly(self) -> None:
        parameters = self.committed()
        for transport in TRANSPORTS:
            raw, name, carried = RAW_JSON[transport.variable], transport.variable, transport.transport_variable
            for env, expected in (
                ({name: raw, carried: encode(raw)}, None),  # control: derived by the workflow/hook
                ({}, None),
                ({name: raw}, "is empty while"),
                ({carried: encode(raw)}, "is set while"),
                ({name: raw, carried: encode(raw + " ")}, "different value"),
                ({name: raw, carried: " " + encode(raw)}, "not the canonical"),
                ({name: raw, carried: raw}, "not the canonical"),
            ):
                with self.subTest(variable=name, env=sorted(env), expected=expected):
                    errors = self.transport_errors(parameters, env)
                    if expected is None:
                        self.assertEqual(errors, [])
                    else:
                        self.assertEqual(len(errors), 1, errors)
                        self.assertIn(expected, errors[0])
                        self.assertIn(carried, errors[0])
                        self.assertNotIn(raw, errors[0])

    def test_raw_json_substitution_and_misnamed_transports_are_refused(self) -> None:
        old = json.loads(_pre_fix_parameters_text())["parameters"]
        errors = self.transport_errors(old, {})
        self.assertEqual(len(errors), len(TRANSPORTS), errors)
        for transport in TRANSPORTS:
            self.assertTrue(any(f"substitutes {transport.variable} directly" in e for e in errors))
        misnamed = {**self.committed(), "groupPolicyJsonBase64": {"value": "${AI4IA_OTHER_B64=}"}}
        self.assertEqual(
            self.transport_errors(misnamed, {}),
            ["groupPolicyJsonBase64 must read AI4IA_GROUP_POLICY_JSON_B64."],
        )

    def test_group_policy_is_validated_after_decoding_at_the_64_kib_bound(self) -> None:
        enabled = {**PROD_ENV, "AI4IA_GROUP_POLICY_ENABLED": "true"}
        for size, messages in (
            (65536, []),
            (65537, ["bounded, nonempty groupPolicyJson"]),
            (65539, ["bounded, nonempty groupPolicyJson", "exceeds 87384 characters"]),
        ):
            for filler in (" ", "東"):
                with self.subTest(size=size, filler=filler), _large_environment(
                    **_transported({**enabled, "AI4IA_GROUP_POLICY_JSON": _policy_of_size(size, filler)})
                ):
                    code, _, err = _run(REAL_PARAMETERS)
                    self.assertEqual(code, 1 if messages else 0, err)
                    for message in messages:
                        self.assertIn(message, err)

    def test_decoded_group_policy_is_validated_like_the_raw_value(self) -> None:
        enabled = {**PROD_ENV, "AI4IA_GROUP_POLICY_ENABLED": "true"}
        for policy, expected in (
            (INCIDENT_POLICY, None),
            ("not json", "valid JSON"),
            ('{"version":true}', "version-1 object"),
            ('{"version":1,"directoryLookup":true}', "unsupported top-level"),
        ):
            with self.subTest(policy=policy), _environment(
                **_transported({**enabled, "AI4IA_GROUP_POLICY_JSON": policy})
            ):
                code, _, err = _run(REAL_PARAMETERS)
                self.assertEqual(code, 0 if expected is None else 1, err)
                if expected:
                    self.assertIn(expected, err)

    def test_the_secret_projection_never_reaches_validator_output(self) -> None:
        stale = {
            "AI4IA_PROXY_PROFILE_PROJECTION_JSON": SECRET_PROJECTION,
            "AI4IA_PROXY_PROFILE_PROJECTION_JSON_B64": encode(SECRET_PROJECTION + " "),
        }
        with _environment(**stale):
            code, out, err = _run(REAL_PARAMETERS)
        self.assertEqual(code, 1)
        self.assertIn("AI4IA_PROXY_PROFILE_PROJECTION_JSON_B64 does not carry", err)
        for value in (*stale.values(), encode(SECRET_PROJECTION)):
            self.assertNotIn(value, out + err)


if __name__ == "__main__":
    unittest.main()
