"""The workflows whose checks gate a merge must always report their contexts.

GitHub waits indefinitely for a required status check that is never reported, so
a **path-filtered** workflow cannot be a required check: a PR that misses the
filter would never see the context and would sit blocked forever. That was
verified empirically -- adding one unreachable context flipped an otherwise-green
PR from `CLEAN` to `BLOCKED`.

The consequence was that `app-ci`, `infra-validate` and `docker-build` were
deliberately excluded from branch protection, which meant a PR could break the
2,300-test API suite, the Bicep build, or the container images and still be
mergeable. Only the eleven always-emitted contexts actually blocked anything.

Those three workflows now run on **every** pull request. That is the cheapest fix
with no silent-skip failure mode. The tempting alternative -- keep the path
filter, add a `changes` job, gate the real jobs on its output -- introduces
custom change-detection whose bug would be *worse* than the status quo: it would
report success while skipping the tests entirely. Measured cost of always
running: app-ci ~122s, docker-build ~66s, infra-validate ~40s.

This file fails if a `paths:` filter comes back under `pull_request:`, because
the failure it would reintroduce is silent -- the context simply stops appearing,
and a required-check entry that no longer matches anything blocks every PR.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from copy import deepcopy
from fnmatch import fnmatchcase
from pathlib import Path

# A hard import, deliberately not guarded by `unittest.skipIf`. This file is a
# gate: if PyYAML were missing it would skip silently and report success while
# checking nothing, which is exactly the failure mode the rest of this suite
# exists to prevent. The workflow step installs it.
import yaml

from scripts.tests._platform import find_bash

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY_WORKFLOW = WORKFLOWS / "deploy.yml"

# Workflow file -> the status-check contexts it reports. A context is the job's
# `name:` when set, otherwise its job id.
GATING_WORKFLOWS: dict[str, set[str]] = {
    "app-ci.yml": {"web", "api"},
    "infra-validate.yml": {"bicep-lint-build"},
    "docker-build.yml": {"web image", "api image", "dockerignore context boundary"},
}

# Audited action consumers, not a workflow/job allowlist. Moving an action must
# move its grant; a new action needs its token use reviewed before joining here.
ACTION_PERMISSIONS: dict[str, dict[str, str]] = {
    "actions/checkout": {"contents": "read"},
    "actions/configure-pages": {"pages": "read"},
    "actions/deploy-pages": {"pages": "write", "id-token": "write"},
    "azure/login": {"id-token": "write"},
    # CodeQL reads its workflow/run metadata as well as writing code scanning.
    "github/codeql-action/init": {"actions": "read", "security-events": "write"},
    "github/codeql-action/analyze": {"actions": "read", "security-events": "write"},
    "github/codeql-action/upload-sarif": {"security-events": "write"},
}
RUNNER_OR_PUBLIC_ACTIONS = {
    "actions/download-artifact",  # Cross-run token input is handled separately.
    "actions/setup-dotnet",
    "actions/setup-node",
    "actions/setup-python",
    "actions/upload-artifact",
    "actions/upload-pages-artifact",
    "aquasecurity/trivy-action",
    "azure/setup-azd",
    "docker/build-push-action",
    "docker/setup-buildx-action",
    "gitleaks/gitleaks-action",
}


def workflow_paths() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


class GatingWorkflowsAlwaysReportTests(unittest.TestCase):
    def _load(self, filename: str) -> dict:
        raw = (WORKFLOWS / filename).read_text(encoding="utf-8")
        # `on:` is parsed by PyYAML as the boolean True (YAML 1.1 truthy key).
        return yaml.safe_load(raw)

    def _triggers(self, document: dict) -> dict:
        for key in ("on", True):
            if key in document:
                return document[key]
        self.fail("workflow declares no triggers")

    def test_every_gating_workflow_exists(self) -> None:
        for filename in GATING_WORKFLOWS:
            self.assertTrue((WORKFLOWS / filename).is_file(), filename)

    def test_pull_request_trigger_is_not_path_filtered(self) -> None:
        """The property that makes these safe to require."""
        for filename in GATING_WORKFLOWS:
            triggers = self._triggers(self._load(filename))
            self.assertIn("pull_request", triggers, filename)
            pull_request = triggers["pull_request"]
            if pull_request is None:
                continue
            for key in ("paths", "paths-ignore"):
                self.assertNotIn(
                    key,
                    pull_request,
                    f"{filename}: `{key}` under `pull_request:` stops the status "
                    "context being reported on PRs that miss the filter. A "
                    "required check that is never reported blocks the PR forever, "
                    "so either remove the filter or remove the context from the "
                    "branch protection ruleset -- never leave them inconsistent.",
                )

    def test_declared_contexts_match_the_workflow_jobs(self) -> None:
        """Keeps this inventory honest against the actual job definitions.

        A renamed job silently changes its context name, and a required check
        pinned to the old name then never reports.
        """
        for filename, expected in GATING_WORKFLOWS.items():
            jobs = self._load(filename).get("jobs", {})
            actual = {
                (definition or {}).get("name", job_id)
                for job_id, definition in jobs.items()
            }
            self.assertEqual(
                actual,
                expected,
                f"{filename}: job contexts changed. Update this inventory AND the "
                "branch protection ruleset together.",
            )

    def test_push_triggers_may_still_be_path_filtered(self) -> None:
        """Non-vacuity control.

        Pushes to main do not gate a merge, so their filters are free to stay --
        and if a blanket edit stripped those too, this test would notice rather
        than let CI cost quietly triple.
        """
        filtered = 0
        for filename in GATING_WORKFLOWS:
            triggers = self._triggers(self._load(filename))
            push = triggers.get("push")
            if isinstance(push, dict) and "paths" in push:
                filtered += 1
        self.assertEqual(
            filtered,
            len(GATING_WORKFLOWS),
            "push triggers lost their path filters; that is not required for "
            "branch protection and only costs runner time",
        )


class DeployWorkflowOperationalScriptTriggers(unittest.TestCase):
    """A release-path change must exercise itself on main.

    `postprovision.ps1` broke production deploys in #320, and its fix would not
    have triggered deploy if it touched only the script: the workflow watched
    app/infra/proxy and its own YAML, but not the code it executes.
    """

    def test_direct_release_scripts_trigger_deploy(self) -> None:
        raw = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        doc = yaml.safe_load(raw)
        triggers = doc.get("on", doc.get(True, {}))
        paths = set(triggers["push"]["paths"])
        self.assertTrue(
            {
                "scripts/postprovision.ps1",
                "scripts/post-deploy-verify.py",
                "scripts/check-resource-providers.py",
                "scripts/check-model-availability.py",
                "scripts/validate-feature-prereqs.py",
                "scripts/gen-model-catalog.py",
                "scripts/gen-mcp-catalog.py",
                "scripts/gen-voice-provider-catalog.py",
                "scripts/gen-gateway-policy.py",
            }.issubset(paths),
            "a script deploy.yml executes can change without ever exercising "
            "itself in production",
        )


class GeneratorDependencyTriggerTests(unittest.TestCase):
    def test_shared_generators_trigger_their_consuming_workflows(self) -> None:
        dependencies = {
            "app-ci.yml": {"scripts/_generator.py"},
            "deploy.yml": {"scripts/_generator.py"},
            "pages.yml": {"scripts/_generator.py", "scripts/gen-docs-catalog.py"},
        }
        for filename, sources in dependencies.items():
            document = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))
            paths = document.get("on", document.get(True, {}))["push"]["paths"]
            for source in sources:
                with self.subTest(workflow=filename, source=source):
                    self.assertTrue(
                        any(fnmatchcase(source, pattern) for pattern in paths),
                        f"{filename} does not run when its generator dependency {source} changes",
                    )

class WorkflowCheckoutCredentialTests(unittest.TestCase):
    def test_checkouts_do_not_retain_tokens_for_later_steps(self) -> None:
        checked = 0
        for path in workflow_paths():
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_name, job in document.get("jobs", {}).items():
                for step in job.get("steps", []):
                    if step.get("uses", "").split("@", 1)[0].lower() != "actions/checkout":
                        continue
                    checked += 1
                    with self.subTest(workflow=path.name, job=job_name):
                        self.assertIs(
                            step.get("with", {}).get("persist-credentials"),
                            False,
                            "No current workflow needs a checkout credential after fetching source.",
                        )
        self.assertGreaterEqual(checked, 15, "checkout discovery is no longer exercising the workflows")


class WorkflowPermissionBoundaryTests(unittest.TestCase):
    def required_permissions(self, job: dict) -> dict[str, str]:
        self.assertNotIn("uses", job, "Review token forwarding before adding a reusable workflow.")
        required: dict[str, str] = {}

        def require(permissions: dict[str, str]) -> None:
            for scope, level in permissions.items():
                if required.get(scope) != "write":
                    required[scope] = level

        for step in job.get("steps", []):
            action = step.get("uses", "").split("@", 1)[0].lower()
            inputs = step.get("with", {})
            if action:
                self.assertIn(
                    action,
                    ACTION_PERMISSIONS.keys() | RUNNER_OR_PUBLIC_ACTIONS,
                    "Review the new action's token/API consumers before declaring its permissions.",
                )
                require(ACTION_PERMISSIONS.get(action, {}))
            if action == "actions/configure-pages":
                self.assertIn(inputs.get("enablement", False), (False, "false"))
                self.assertNotIn("token", inputs, "Pages must use the scoped job token.")
            if action == "azure/login":
                self.assertNotIn("creds", inputs, "Keep the reviewed OIDC login path.")
                self.assertEqual(inputs.get("auth-type", "SERVICE_PRINCIPAL"), "SERVICE_PRINCIPAL")
            if action == "actions/download-artifact" and inputs.get("github-token"):
                # Unlike same-run uploads/listing, findBy uses the Actions REST API.
                require({"actions": "read"})
            if action == "gitleaks/gitleaks-action":
                self.assertEqual(step.get("env", {}).get("GITLEAKS_ENABLE_COMMENTS"), "false")

            script = re.sub(r"(?m)^\s*#.*$", "", step.get("run", ""))
            if re.search(r"\bazd\s+auth\s+login\b", script):
                self.assertRegex(script, r"--federated-credential-provider\s+['\"]?github\b")
                require({"id-token": "write"})
            if re.search(r"\bgh\s+api\b", script):
                self.assertIn("/actions/runs/", script, "Review the new GitHub REST consumer.")
                self.assertNotRegex(
                    script,
                    r"(?:--method|--field|--raw-field)(?:[=\s])|(?:^|\s)-[XfF]",
                    "Only the reviewed read-only Actions REST call is admitted.",
                )
                require({"actions": "read"})
        return required

    def assert_permission_boundary(self, document: dict) -> None:
        # contents:read is sufficient for checkout-only workflows. A job without
        # a checkout must explicitly opt out rather than inherit even that grant.
        defaults = document["permissions"]
        self.assertIn(defaults, ({}, {"contents": "read"}), "Privileged workflow default.")
        self.assertTrue(document.get("jobs"), "No jobs discovered.")
        for job_name, job in document["jobs"].items():
            # A job map REPLACES workflow defaults; it does not merge with them.
            effective = job.get("permissions", defaults)
            self.assertEqual(
                effective,
                self.required_permissions(job),
                f"{job_name}: permissions must match its actual action/REST/OIDC consumers.",
            )

    def test_all_workflow_jobs_have_only_the_permissions_their_steps_need(self) -> None:
        paths = workflow_paths()
        self.assertGreaterEqual(len(paths), 9, "Workflow discovery lost coverage.")
        for path in paths:
            with self.subTest(workflow=path.name):
                self.assert_permission_boundary(yaml.safe_load(path.read_text(encoding="utf-8")))

    def test_workflow_writes_are_rejected_even_when_jobs_override_them(self) -> None:
        document = {
            "permissions": {},
            "jobs": {"validation": {"permissions": {}, "steps": [{"run": "true"}]}},
        }
        self.assert_permission_boundary(document)
        for scope in ("contents", "pages", "id-token", "security-events", "actions"):
            with self.subTest(scope=scope):
                document["permissions"] = {scope: "write"}
                with self.assertRaisesRegex(AssertionError, "Privileged workflow default"):
                    self.assert_permission_boundary(document)

    def test_tokenless_artifact_job_must_opt_out_of_checkout_defaults(self) -> None:
        pages = yaml.safe_load((WORKFLOWS / "pages.yml").read_text(encoding="utf-8"))
        upload = next(
            step
            for job in pages["jobs"].values()
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/upload-pages-artifact@")
        )
        document = {
            "permissions": {"contents": "read"},
            "jobs": {"artifact-only": {"steps": [deepcopy(upload)]}},
        }
        with self.assertRaisesRegex(AssertionError, "artifact-only: permissions"):
            self.assert_permission_boundary(document)
        document["jobs"]["artifact-only"]["permissions"] = {}
        self.assert_permission_boundary(document)

    def test_oidc_permission_follows_the_consumer_when_jobs_change(self) -> None:
        pages = yaml.safe_load((WORKFLOWS / "pages.yml").read_text(encoding="utf-8"))
        login = next(
            step
            for job in pages["jobs"].values()
            for step in job["steps"]
            if step.get("uses", "").startswith("azure/login@")
        )
        document = {
            "permissions": {},
            "jobs": {
                "original": {"permissions": {"id-token": "write"}, "steps": [deepcopy(login)]},
                "new-name": {"permissions": {}, "steps": []},
            },
        }
        self.assert_permission_boundary(document)
        original, moved = document["jobs"].values()
        moved["steps"], original["steps"] = original["steps"], []
        with self.assertRaisesRegex(AssertionError, "original: permissions"):
            self.assert_permission_boundary(document)
        original["permissions"] = {}
        with self.assertRaisesRegex(AssertionError, "new-name: permissions"):
            self.assert_permission_boundary(document)
        moved["permissions"] = {"id-token": "write"}
        self.assert_permission_boundary(document)


BASH = find_bash()


@unittest.skipIf(BASH is None, "bash is unavailable on this machine")
class DeployWorkflowConfigurationValidationTests(unittest.TestCase):
    REQUIRED = {
        "AZURE_CLIENT_ID": "client-id",
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_SUBSCRIPTION_ID": "subscription-id",
        "AZURE_ENV_NAME": "prod",
        "AZURE_LOCATION": "westus3",
        "AI4IA_APP_ENVIRONMENT": "prod",
        "AI4IA_AUTH_PROVIDER": "entra",
        "AI4IA_ENTRA_TENANT_ID": "tenant-id",
        "AI4IA_ENTRA_AUDIENCE": "api://ai4ia-api",
        "AI4IA_ENTRA_WEB_CLIENT_ID": "web-client-id",
        "AI4IA_OWNER": "platform-team",
        "AI4IA_COST_CENTER": "platform-engineering",
        "AI4IA_APIM_PUBLISHER_EMAIL": "ai4ia-ops@example.org",
        "AI4IA_BUDGET_AMOUNT": "1500",
        "AI4IA_BUDGET_START_DATE": "2026-08-01",
        "AI4IA_ALERT_EMAIL": "ai4ia-alerts@example.org",
        "AI4IA_CLAUDE_ENABLED": "true",
        "AI4IA_CLAUDE_ORGANIZATION_NAME": "Example Legal Entity",
        "AI4IA_CLAUDE_COUNTRY_CODE": "US",
        "AI4IA_CLAUDE_INDUSTRY": "technology",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
        cls.jobs = cls.document["jobs"]
        cls.validation_job = cls.jobs["validate-deployment-configuration"]
        matches = [
            step
            for step in cls.validation_job["steps"]
            if step.get("name") == "Validate required deployment variables"
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"expected one deployment validation step, found {len(matches)}"
            )
        cls.validation_script = matches[0]["run"]

    def run_validation(
        self,
        *,
        posture: str = "",
        claude_enabled: str = "true",
        missing: tuple[str, ...] = (),
        ref: str = "refs/heads/main",
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "validate.sh"
            output = root / "output"
            script.write_text(self.validation_script, encoding="utf-8", newline="\n")
            output.write_text("", encoding="utf-8")
            env = dict(os.environ)
            env.update(self.REQUIRED)
            env["AI4IA_DEPLOYMENT_ENABLED"] = posture
            env["AI4IA_CLAUDE_ENABLED"] = claude_enabled
            env["GITHUB_REF"] = ref
            env["GITHUB_OUTPUT"] = str(output)
            for name in missing:
                env.pop(name, None)
            assert BASH is not None
            result = subprocess.run(
                [BASH, str(script)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            return result, output.read_text(encoding="utf-8")

    def test_configured_deployment_is_enabled(self) -> None:
        result, output = self.run_validation()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.strip(), "deployment_enabled=true")

    def test_only_the_exact_main_branch_can_reach_deployment_admission(self) -> None:
        for ref in ("", "main", "refs/heads/release", "refs/tags/main", "refs/heads/Main"):
            with self.subTest(ref=ref):
                result, output = self.run_validation(ref=ref)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "")
                self.assertIn("Deployments are restricted to refs/heads/main.", result.stdout)
        result, output = self.run_validation(ref="refs/heads/main")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.strip(), "deployment_enabled=true")

    def test_non_main_ref_is_rejected_even_when_deployment_is_disabled(self) -> None:
        result, output = self.run_validation(
            ref="refs/heads/release", posture="false", missing=tuple(self.REQUIRED)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.assertIn("Deployments are restricted to refs/heads/main.", result.stdout)

    def test_only_the_gated_deploy_job_can_request_oidc(self) -> None:
        self.assertEqual(self.document["permissions"], {})
        self.assertNotIn("permissions", self.validation_job)
        self.assertEqual(
            self.jobs["deploy"]["permissions"],
            {"id-token": "write", "contents": "read"},
        )
        self.assertEqual(
            [(step.get("name"), step.get("uses")) for step in self.validation_job["steps"]],
            [("Validate required deployment variables", None)],
        )
        checkout = next(
            step for step in self.jobs["deploy"]["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        )
        self.assertIs(checkout.get("with", {}).get("persist-credentials"), False)

    def test_missing_variables_fail_with_each_actionable_name(self) -> None:
        missing = (
            "AZURE_CLIENT_ID",
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_LOCATION",
            "AI4IA_OWNER",
            "AI4IA_BUDGET_START_DATE",
            "AI4IA_CLAUDE_ORGANIZATION_NAME",
        )
        result, output = self.run_validation(missing=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        for name in missing:
            self.assertIn(name, result.stdout + result.stderr)

    def test_claude_disabled_does_not_require_attestation(self) -> None:
        attestation = (
            "AI4IA_CLAUDE_ORGANIZATION_NAME",
            "AI4IA_CLAUDE_COUNTRY_CODE",
            "AI4IA_CLAUDE_INDUSTRY",
        )
        result, output = self.run_validation(
            claude_enabled="false", missing=attestation
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.strip(), "deployment_enabled=true")

    def test_invalid_claude_posture_fails(self) -> None:
        result, output = self.run_validation(claude_enabled="enabled")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.assertIn("AI4IA_CLAUDE_ENABLED must be true, false, or unset", result.stdout)

    def test_explicit_disabled_posture_is_the_only_clean_skip(self) -> None:
        result, output = self.run_validation(
            posture="false", missing=tuple(self.REQUIRED)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.strip(), "deployment_enabled=false")

    def test_invalid_posture_fails(self) -> None:
        result, output = self.run_validation(posture="disabled")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.assertIn("must be true, false, or unset", result.stdout + result.stderr)

    def test_deploy_job_depends_on_validation_output(self) -> None:
        deploy = self.jobs["deploy"]
        self.assertEqual(deploy["needs"], "validate-deployment-configuration")
        self.assertIn(
            "needs.validate-deployment-configuration.outputs.deployment_enabled",
            deploy["if"],
        )

    def test_azd_is_pinned_and_verified(self) -> None:
        steps = self.jobs["deploy"]["steps"]
        setup = next(step for step in steps if step.get("name") == "Install azd")
        self.assertEqual(setup["with"]["version"], "1.29.0")
        verify = next(step for step in steps if step.get("name") == "Verify azd version")
        self.assertIn("azd version 1\\.29\\.0", verify["run"])


if __name__ == "__main__":
    unittest.main()
