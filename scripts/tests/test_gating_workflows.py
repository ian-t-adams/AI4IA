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
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

# A hard import, deliberately not guarded by `unittest.skipIf`. This file is a
# gate: if PyYAML were missing it would skip silently and report success while
# checking nothing, which is exactly the failure mode the rest of this suite
# exists to prevent. The workflow step installs it.
import yaml

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


def _find_bash() -> str | None:
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("bash")
    return found if found and os.name != "nt" else None


BASH = _find_bash()


@unittest.skipIf(BASH is None, "bash is unavailable on this machine")
class DeployWorkflowConfigurationValidationTests(unittest.TestCase):
    REQUIRED = {
        "AZURE_CLIENT_ID": "client-id",
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_SUBSCRIPTION_ID": "subscription-id",
        "AZURE_ENV_NAME": "prod",
        "AZURE_LOCATION": "westus3",
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
        self, *, posture: str = "", missing: tuple[str, ...] = ()
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

    def test_missing_variables_fail_with_each_actionable_name(self) -> None:
        missing = (
            "AZURE_CLIENT_ID",
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_LOCATION",
            "AI4IA_CLAUDE_ORGANIZATION_NAME",
        )
        result, output = self.run_validation(missing=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        for name in missing:
            self.assertIn(name, result.stdout + result.stderr)

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
