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

import base64
import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from fnmatch import fnmatchcase
from pathlib import Path
from unittest import mock

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
    "actions/attest": {"id-token": "write", "attestations": "write"},
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


class CodeQLRequiredContextTests(unittest.TestCase):
    """Matrix contexts need their own guard; CodeQL pushes have no path filter."""

    def setUp(self) -> None:
        self.document = yaml.safe_load((WORKFLOWS / "codeql.yml").read_text(encoding="utf-8"))

    def assert_reporting_boundary(self, document: dict) -> None:
        triggers = document.get("on", document.get(True, {}))
        self.assertIn("pull_request", triggers, "CodeQL pull_request trigger is required.")
        pull_request = triggers["pull_request"]
        self.assertIsInstance(pull_request, dict, "CodeQL pull_request must target main.")
        self.assertEqual(
            pull_request.get("branches"), ["main"], "CodeQL pull_request must target main."
        )
        for key in ("paths", "paths-ignore", "branches-ignore"):
            self.assertNotIn(key, pull_request, f"CodeQL pull_request cannot filter with {key}.")
        self.assertTrue(
            {"opened", "synchronize", "reopened"}.issubset(
                pull_request.get("types", ["opened", "synchronize", "reopened"])
            ),
            "CodeQL pull_request must cover the default PR events.",
        )

        jobs = document.get("jobs", {})
        self.assertIn("analyze", jobs, "CodeQL analyze job is required.")
        job = jobs["analyze"]
        for key in ("if", "needs"):
            self.assertNotIn(key, job, f"CodeQL analyze cannot be skipped through {key}.")
        self.assertIs(
            job.get("continue-on-error", False), False, "CodeQL analysis must remain blocking."
        )
        strategy = job.get("strategy", {})
        self.assertIs(
            strategy.get("fail-fast"), False, "CodeQL fail-fast must not cancel sibling contexts."
        )
        matrix = strategy.get("matrix", {})
        self.assertIsInstance(matrix, dict, "CodeQL matrix must use literal include rows.")
        self.assertEqual(
            set(matrix), {"include"}, "CodeQL matrix must not add axes or exclude language rows."
        )
        rows = matrix["include"]
        self.assertIsInstance(rows, list, "CodeQL matrix must use literal include rows.")
        name = job.get("name", "analyze")
        self.assertIsInstance(name, str, "CodeQL context name must be a string.")
        contexts = []
        for row in rows:
            self.assertIsInstance(row, dict, "CodeQL matrix rows must declare a language.")
            language = row.get("language")
            self.assertIsInstance(language, str, "CodeQL matrix rows must declare a language.")
            contexts.append(re.sub(r"\$\{\{\s*matrix\.language\s*\}\}", language, name))
        self.assertCountEqual(
            contexts,
            ["Analyze (python)", "Analyze (javascript-typescript)", "Analyze (csharp)"],
            "CodeQL contexts must preserve the exact required language check names.",
        )

    def assert_rejected(self, document: dict, message: str) -> None:
        self.assert_reporting_boundary(self.document)
        with self.assertRaisesRegex(AssertionError, message):
            self.assert_reporting_boundary(document)

    def test_current_workflow_reports_every_required_language_context(self) -> None:
        self.assert_reporting_boundary(self.document)

    def test_missing_pull_request_trigger_is_rejected(self) -> None:
        document = deepcopy(self.document)
        del document.get("on", document.get(True))["pull_request"]
        self.assert_rejected(document, "CodeQL pull_request trigger")

    def test_main_pr_filters_and_event_omissions_are_rejected(self) -> None:
        changes = [
            ("paths", ["proxy/**"]),
            ("paths-ignore", ["docs/**"]),
            ("branches-ignore", ["main"]),
            ("branches", ["release"]),
            ("branches", ["main", "!main"]),
            ("branches", []),
            ("types", ["opened"]),
            ("types", ["synchronize"]),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                document = deepcopy(self.document)
                document.get("on", document.get(True))["pull_request"][key] = value
                self.assert_rejected(document, "CodeQL pull_request")

    def test_explicit_default_events_and_expression_spacing_keep_contexts(self) -> None:
        document = deepcopy(self.document)
        document.get("on", document.get(True))["pull_request"]["types"] = [
            "opened", "synchronize", "reopened",
        ]
        document["jobs"]["analyze"]["name"] = "Analyze (${{matrix.language}})"
        self.assert_reporting_boundary(document)

    def test_missing_analyze_job_is_rejected(self) -> None:
        document = deepcopy(self.document)
        del document["jobs"]["analyze"]
        self.assert_rejected(document, "CodeQL analyze job")

    def test_renamed_or_missing_context_name_is_rejected(self) -> None:
        for name in ("Scan (${{ matrix.language }})", "Analyze", None):
            with self.subTest(name=name):
                document = deepcopy(self.document)
                job = document["jobs"]["analyze"]
                if name is None:
                    del job["name"]
                else:
                    job["name"] = name
                self.assert_rejected(document, "CodeQL contexts")

    def test_removing_any_language_or_duplicating_csharp_is_rejected(self) -> None:
        for language in ("python", "javascript-typescript", "csharp", None):
            with self.subTest(language=language):
                document = deepcopy(self.document)
                rows = document["jobs"]["analyze"]["strategy"]["matrix"]["include"]
                if language is None:
                    rows.append({"language": "csharp", "build-mode": "manual"})
                else:
                    rows[:] = [row for row in rows if row["language"] != language]
                self.assert_rejected(document, "CodeQL contexts")

    def test_missing_language_field_is_rejected(self) -> None:
        document = deepcopy(self.document)
        rows = document["jobs"]["analyze"]["strategy"]["matrix"]["include"]
        del next(row for row in rows if row["language"] == "csharp")["language"]
        self.assert_rejected(document, "CodeQL matrix rows must declare a language")

    def test_matrix_exclusions_or_dynamic_axes_are_rejected(self) -> None:
        for key, value in (
            ("exclude", [{"language": "csharp"}]),
            ("language", ["python", "javascript-typescript"]),
            ("include", "${{ fromJSON(needs.changes.outputs.languages) }}"),
        ):
            with self.subTest(key=key):
                document = deepcopy(self.document)
                document["jobs"]["analyze"]["strategy"]["matrix"][key] = value
                self.assert_rejected(document, "CodeQL matrix")

    def test_job_skips_or_nonblocking_analysis_are_rejected(self) -> None:
        for key, value in (
            ("if", "github.event_name == 'push'"),
            ("needs", ["changes"]),
            ("continue-on-error", True),
        ):
            with self.subTest(key=key):
                document = deepcopy(self.document)
                document["jobs"]["analyze"][key] = value
                self.assert_rejected(document, "CodeQL analy")

    def test_enabled_or_default_fail_fast_is_rejected(self) -> None:
        for fail_fast in (True, None):
            with self.subTest(fail_fast=fail_fast):
                document = deepcopy(self.document)
                strategy = document["jobs"]["analyze"]["strategy"]
                if fail_fast is None:
                    del strategy["fail-fast"]
                else:
                    strategy["fail-fast"] = fail_fast
                self.assert_rejected(document, "CodeQL fail-fast")


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
                "scripts/verify-image-provenance.py",
                "scripts/_image_refs.py",
                "scripts/check-resource-providers.py",
                "scripts/check-model-availability.py",
                "scripts/validate-feature-prereqs.py",
                "scripts/derive-json-transport.py",
                "scripts/_json_transport.py",
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

class ClaudeWorkflowBoundaryTests(unittest.TestCase):
    def test_disabled_jobs_keep_source_identity_and_enabled_reader_is_isolated(self):
        document = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
        job = document["jobs"]["deploy"]
        steps = job["steps"]
        source = next(step for step in steps if step.get("name") == "Log in to Azure CLI (OIDC)")
        self.assertNotIn("if", source)
        self.assertEqual(source["with"]["client-id"], "${{ env.AZURE_CLIENT_ID }}")
        reader = next(step for step in steps if step.get("name") == "Log in isolated Claude target reader (OIDC)")
        self.assertEqual(reader["if"], "${{ env.AI4IA_CLAUDE_EXTERNAL_ENABLED == 'true' }}")
        self.assertEqual(reader["env"]["AZURE_CONFIG_DIR"], "${{ runner.temp }}/claude-target-reader")
        for input_name, field in (
            ("client-id", "targetReaderClientId"), ("tenant-id", "targetTenantId"),
            ("subscription-id", "targetSubscriptionId"),
        ):
            self.assertIn(field, reader["with"][input_name])
            self.assertNotIn("AZURE_CLIENT_ID", reader["with"][input_name])
        for name in ("AI4IA_CLAUDE_ENABLED", "AI4IA_CLAUDE_EXTERNAL_ENABLED", "AI4IA_CLAUDE_BINDING_JSON"):
            self.assertEqual(job["env"][name], "${{ vars." + name + " }}")
        check = next(step for step in steps if step.get("name") == "Validate cross-tenant Claude binding configuration")
        self.assertNotIn("AI4IA_CLAUDE_TARGET_AZURE_CONFIG_DIR", job["env"])
        self.assertIn('${RUNNER_TEMP}/claude-target-reader', check["run"])
        self.assertIn('"$GITHUB_ENV"', check["run"])
        self.assertIn('claude_external="${AI4IA_CLAUDE_EXTERNAL_ENABLED:-}"', check["run"])
        self.assertIn('"${claude_external,,}" = "true"', check["run"])
        routed = next(step for step in steps if step.get("name") == "Verify current Claude routes before image rollout")
        deploy = next(step for step in steps if step.get("id") == "deploy")
        self.assertLess(steps.index(check), steps.index(reader))
        self.assertLess(steps.index(reader), steps.index(routed))
        self.assertLess(steps.index(routed), steps.index(deploy))
        self.assertEqual(
            routed["if"],
            "${{ steps.execution.outputs.allowed == 'true' && (env.AI4IA_CLAUDE_EXTERNAL_ENABLED == 'true') }}",
        )
        self.assertEqual(reader["if"], "${{ env.AI4IA_CLAUDE_EXTERNAL_ENABLED == 'true' }}")
        self.assertEqual(routed["run"], "python scripts/check-claude-binding.py --routed")
        self.assertNotIn("provision", routed["if"])


FIRST_CLI_LOGIN = "Log in to Azure CLI (OIDC)"
CLI_LOGIN_REFRESH = "Refresh the Azure CLI login after provisioning"
REVIEWED_AZURE_LOGIN = "azure/login@a641126d1b8aa4d1fa005f4f92df94a3a4c4c906"
SOURCE_IDENTITY_INPUTS = {
    "client-id": "${{ env.AZURE_CLIENT_ID }}",
    "tenant-id": "${{ env.AZURE_TENANT_ID }}",
    "subscription-id": "${{ env.AZURE_SUBSCRIPTION_ID }}",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class DeployCliLoginRefreshTests(unittest.TestCase):
    """The Azure CLI's login-time OIDC assertion does not outlive provisioning.

    `azure/login` gives the CLI one GitHub OIDC assertion, and Entra rejects it
    about ten minutes later (AADSTS700024). From then on every CLI token for a
    resource the CLI has not cached fails. A deploy run passed provisioning and
    the postprovision gates, then failed its canary preflight 11.1 minutes after
    login. The refresh restarts that window before any later step can need a
    new token.
    """

    def setUp(self) -> None:
        document = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
        self.steps = document["jobs"]["deploy"]["steps"]

    @staticmethod
    def assert_refresh_contract(steps: list[dict]) -> None:
        names = [step.get("name") for step in steps]
        positions = [index for index, name in enumerate(names) if name == CLI_LOGIN_REFRESH]
        _require(len(positions) == 1, f"expected one {CLI_LOGIN_REFRESH!r} step, found {len(positions)}")
        (position,) = positions
        refresh = steps[position]
        first = steps[names.index(FIRST_CLI_LOGIN)]
        # The identical reviewed action and identity: no new pin, input or grant.
        _require(first.get("uses") == REVIEWED_AZURE_LOGIN, f"first login uses {first.get('uses')!r}")
        _require(refresh.get("uses") == first["uses"], f"refresh uses {refresh.get('uses')!r}")
        _require(first.get("with") == SOURCE_IDENTITY_INPUTS, f"first login inputs {first.get('with')!r}")
        _require(refresh.get("with") == first["with"], f"refresh inputs {refresh.get('with')!r}")
        # Always within an admitted deployment, including no-provision runs;
        # the preview terminal boundary must not exchange a late deployment token.
        _require(set(refresh) == {"name", "uses", "with", "if"}, f"refresh keys {sorted(refresh)}")
        _require(refresh["if"] == "${{ steps.execution.outputs.allowed == 'true' }}",
                 "refresh must depend only on admitted execution, not provision outcome")
        _require(
            position > 0 and names[position - 1] == "Provision infrastructure",
            f"refresh follows {names[position - 1]!r}, not provisioning",
        )
        build = next(
            (index for index, name in enumerate(names)
             if str(name).startswith("Build and push service images")),
            None,
        )
        _require(build is not None and position < build, "refresh must precede the image build")

    def test_the_cli_login_is_refreshed_right_after_provisioning(self) -> None:
        self.assert_refresh_contract(self.steps)

    def test_the_contract_rejects_each_way_the_refresh_can_regress(self) -> None:
        def at(steps: list[dict], name: str) -> int:
            return next(index for index, step in enumerate(steps) if step.get("name") == name)

        def move_after(target: str):
            def change(steps: list[dict]) -> None:
                step = steps.pop(at(steps, CLI_LOGIN_REFRESH))
                steps.insert(at(steps, target) + 1, step)
            return change

        def edit(**fields: object):
            def change(steps: list[dict]) -> None:
                steps[at(steps, CLI_LOGIN_REFRESH)].update(deepcopy(fields))
            return change

        def edit_inputs(change_inputs):
            def change(steps: list[dict]) -> None:
                change_inputs(steps[at(steps, CLI_LOGIN_REFRESH)]["with"])
            return change

        def remove(steps: list[dict]) -> None:
            steps.pop(at(steps, CLI_LOGIN_REFRESH))

        def duplicate(steps: list[dict]) -> None:
            index = at(steps, CLI_LOGIN_REFRESH)
            steps.insert(index + 1, deepcopy(steps[index]))

        def edit_both_logins(steps: list[dict]) -> None:
            for name in (FIRST_CLI_LOGIN, CLI_LOGIN_REFRESH):
                steps[at(steps, name)]["with"]["client-id"] = "${{ vars.OTHER_CLIENT_ID }}"

        mutations = {
            "removed": remove,
            "duplicated": duplicate,
            "moved after the image build": move_after("Build and push service images, recording their digests"),
            "moved after the legacy-role check": move_after("Verify legacy API inference roles are revoked"),
            "moved before provisioning": move_after("Capture pre-provision revisions (rollback target)"),
            "floating tag": edit(uses="azure/login@v3"),
            "different pinned commit": edit(uses="azure/login@" + "0" * 40),
            "different client": edit_inputs(lambda inputs: inputs.update({"client-id": "${{ vars.OTHER_CLIENT_ID }}"})),
            "extra input": edit_inputs(lambda inputs: inputs.update({"allow-no-subscriptions": True})),
            "missing input": edit_inputs(lambda inputs: inputs.pop("subscription-id")),
            "both logins changed together": edit_both_logins,
            "runs after a failure": edit(**{"if": "${{ always() }}"}),
            "skipped without provisioning": edit(**{"if": "${{ steps.provision.outcome == 'success' }}"}),
            "separate CLI profile": edit(env={"AZURE_CONFIG_DIR": "${{ runner.temp }}/refresh"}),
            "suppressed failure": edit(**{"continue-on-error": True}),
        }
        # Control: the identical copy, before any mutation, satisfies the contract.
        self.assert_refresh_contract(deepcopy(self.steps))
        for name, change in mutations.items():
            with self.subTest(mutation=name):
                steps = deepcopy(self.steps)
                change(steps)
                with self.assertRaises(AssertionError):
                    self.assert_refresh_contract(steps)


DERIVE_TRANSPORTS = "Derive azd transports for JSON-valued variables"
DERIVE_COMMAND = "python scripts/derive-json-transport.py --github-env"
TRANSPORT_RAW_VALUES = {
    "AI4IA_GROUP_POLICY_JSON": '{"version": 1, "domains": {}}',
    "AI4IA_CLAUDE_BINDING_JSON": '{"networkMode": "public-keyless"}',
}


class DeployJsonTransportTests(unittest.TestCase):
    """JSON-valued variables reach azd only as transports derived before provisioning.

    azd substitutes values into main.parameters.json unescaped, so deploy run
    36259812510 failed before provisioning anything once AI4IA_GROUP_POLICY_JSON
    held valid JSON. The derivation must precede `azd provision`, mask the
    secret-derived projection, and need no token or permission.
    """

    def setUp(self) -> None:
        document = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
        self.job = document["jobs"]["deploy"]
        self.steps = self.job["steps"]

    @staticmethod
    def assert_derivation_contract(steps: list[dict]) -> None:
        names = [step.get("name") for step in steps]
        positions = [index for index, name in enumerate(names) if name == DERIVE_TRANSPORTS]
        _require(len(positions) == 1, f"expected one {DERIVE_TRANSPORTS!r} step, found {len(positions)}")
        (position,) = positions
        # Only the command: no action, token, env override, condition or suppression.
        step = steps[position]
        _require(step == {"name": DERIVE_TRANSPORTS, "run": DERIVE_COMMAND}, f"derivation step is {step!r}")
        provisions = [
            index for index, candidate in enumerate(steps)
            if re.search(r"\bazd\s+provision\b", str(candidate.get("run", "")))
        ]
        _require(
            len(provisions) == 1 and position < provisions[0],
            f"derivation at {position} must precede the one azd provision at {provisions}",
        )
        python = next(
            (index for index, candidate in enumerate(steps)
             if str(candidate.get("uses", "")).startswith("actions/setup-python@")),
            None,
        )
        _require(python is not None and python < position, "derivation runs before Python is set up")

    def test_transports_are_derived_before_azd_provision(self) -> None:
        self.assert_derivation_contract(self.steps)

    def test_the_contract_rejects_each_way_the_derivation_can_regress(self) -> None:
        def at(steps: list[dict], name: str) -> int:
            return next(index for index, step in enumerate(steps) if step.get("name") == name)

        def remove(steps: list[dict]) -> None:
            steps.pop(at(steps, DERIVE_TRANSPORTS))

        def duplicate(steps: list[dict]) -> None:
            index = at(steps, DERIVE_TRANSPORTS)
            steps.insert(index + 1, deepcopy(steps[index]))

        def move(target: str | None):
            def change(steps: list[dict]) -> None:
                step = steps.pop(at(steps, DERIVE_TRANSPORTS))
                steps.insert(0 if target is None else at(steps, target) + 1, step)
            return change

        def edit(**fields: object):
            def change(steps: list[dict]) -> None:
                steps[at(steps, DERIVE_TRANSPORTS)].update(deepcopy(fields))
            return change

        mutations = {
            "removed": remove,
            "duplicated": duplicate,
            "moved after provisioning": move("Provision infrastructure"),
            "moved before Python": move(None),
            "conditional": edit(**{"if": "${{ inputs.provision }}"}),
            "suppressed failure": edit(**{"continue-on-error": True}),
            "extra environment": edit(env={"AI4IA_GROUP_POLICY_JSON": "{}"}),
            "hook mode instead of GITHUB_ENV": edit(run="python scripts/derive-json-transport.py --azd-env"),
            "an action": edit(uses="actions/github-script@v7"),
        }
        # Control: the identical copy, before any mutation, satisfies the contract.
        self.assert_derivation_contract(deepcopy(self.steps))
        for name, change in mutations.items():
            with self.subTest(mutation=name):
                steps = deepcopy(self.steps)
                change(steps)
                with self.assertRaises(AssertionError):
                    self.assert_derivation_contract(steps)

    def test_the_step_derives_only_current_transports_without_printing_values(self) -> None:
        step = next(step for step in self.steps if step.get("name") == DERIVE_TRANSPORTS)
        program, script, mode = step["run"].split()
        self.assertEqual(program, "python")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "github-env"
            target.write_text("", encoding="utf-8")
            env = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("AI4IA_", "GITHUB_"))
            }
            env.update(TRANSPORT_RAW_VALUES, GITHUB_ENV=str(target))
            result = subprocess.run(
                [sys.executable, script, mode], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=60,
            )
            written = dict(line.split("=", 1) for line in target.read_text(encoding="utf-8").splitlines())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(written), {f"{name}_B64" for name in TRANSPORT_RAW_VALUES})
        for name, raw in TRANSPORT_RAW_VALUES.items():
            self.assertEqual(base64.b64decode(written[f"{name}_B64"], validate=True).decode("utf-8"), raw)
        self.assertNotIn("AI4IA_PROXY_PROFILE_PROJECTION_JSON_B64", written)
        for value in (*TRANSPORT_RAW_VALUES.values(), *written.values()):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_the_step_needs_no_token_or_permission(self) -> None:
        step = next(step for step in self.steps if step.get("name") == DERIVE_TRANSPORTS)
        boundary = WorkflowPermissionBoundaryTests("test_all_workflow_jobs_have_only_the_permissions_their_steps_need")
        self.assertEqual(boundary.required_permissions({"steps": [step]}), {})
        self.assertEqual(
            self.job["permissions"], {"id-token": "write", "contents": "read", "attestations": "write"}
        )

    def test_raw_variables_stay_the_exported_operator_contract(self) -> None:
        env = self.job["env"]
        self.assertEqual(env["AI4IA_GROUP_POLICY_JSON"], "${{ vars.AI4IA_GROUP_POLICY_JSON }}")
        self.assertEqual(env["AI4IA_CLAUDE_BINDING_JSON"], "${{ vars.AI4IA_CLAUDE_BINDING_JSON }}")
        self.assertNotIn("AI4IA_PROXY_PROFILE_PROJECTION_JSON", env)
        self.assertEqual([name for name in env if name.endswith("_B64")], [], "transports are derived, never configured")
        reader = next(step for step in self.steps if step.get("name") == "Log in isolated Claude target reader (OIDC)")
        for value in reader["with"].values():
            self.assertIn("fromJSON(env.AI4IA_CLAUDE_BINDING_JSON || '{}')", value)


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
            if action == "actions/attest":
                # ACR uses the existing Docker login, not packages:write. The
                # unreviewed org-only metadata consumer must stay disabled.
                self.assertIs(inputs.get("create-storage-record"), False)
                self.assertIs(inputs.get("push-to-registry"), True)
                self.assertNotIn("github-token", inputs, "Use the scoped job token.")
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
        for scope in ("contents", "pages", "id-token", "security-events", "actions", "attestations"):
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

    def test_attestation_permissions_follow_the_actual_consumer(self) -> None:
        step = {
            "uses": "actions/attest@reviewed-sha",
            "with": {"create-storage-record": False, "push-to-registry": True},
        }
        document = {
            "permissions": {},
            "jobs": {"sign": {
                "permissions": {"id-token": "write", "attestations": "write"},
                "steps": [step],
            }},
        }
        self.assert_permission_boundary(document)
        job = document["jobs"]["sign"]
        for scope in ("id-token", "attestations"):
            original = job["permissions"].pop(scope)
            with self.assertRaises(AssertionError):
                self.assert_permission_boundary(document)
            job["permissions"][scope] = original
        for scope in ("packages", "artifact-metadata", "contents"):
            job["permissions"][scope] = "write"
            with self.assertRaises(AssertionError):
                self.assert_permission_boundary(document)
            del job["permissions"][scope]
        job["steps"] = []
        with self.assertRaises(AssertionError):
            self.assert_permission_boundary(document)
        job["permissions"] = {}
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
        "AI4IA_CLAUDE_EXTERNAL_ENABLED": "true",
        "AI4IA_CLAUDE_BINDING_JSON": "{}",
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
        extra: dict[str, str] | None = None,
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
            env.update(extra or {})
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

    def test_gateway_authentication_is_explicit_and_not_a_federation_fallback(self) -> None:
        gateway = {
            "AI4IA_CLAUDE_EXTERNAL_ENABLED": "false", "AI4IA_CLAUDE_GATEWAY_ENABLED": "true",
            "AI4IA_CLAUDE_GATEWAY_URL": "https://claude-fixture.azure-api.net/claude",
        }
        attestation = (
            "AI4IA_CLAUDE_ORGANIZATION_NAME", "AI4IA_CLAUDE_COUNTRY_CODE", "AI4IA_CLAUDE_INDUSTRY",
        )
        result, output = self.run_validation(extra=gateway, missing=attestation)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output.strip(), "deployment_enabled=true")
        for changes in (
            {"AI4IA_CLAUDE_EXTERNAL_ENABLED": "true"},
            {"AI4IA_CLAUDE_GATEWAY_URL": ""},
            {"AI4IA_CLAUDE_GATEWAY_ENABLED": "maybe"},
        ):
            with self.subTest(changes=changes):
                result, output = self.run_validation(extra={**gateway, **changes}, missing=attestation)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "")
        self.assertEqual(self.run_validation(extra=gateway, missing=attestation)[0].returncode, 0)

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
            {"id-token": "write", "contents": "read", "attestations": "write"},
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


class PushReconciliationTests(unittest.TestCase):
    """Execute the workflow's Python against local Git history, never Azure."""

    def setUp(self):
        self.steps = yaml.safe_load(DEPLOY_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        self.step = next(step for step in self.steps if step.get("id") == "reconciliation")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Offline test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.commit({"app/api/file.txt": "old", "infra/model.json": "{}"})
        self.before = self.git("rev-parse", "HEAD").strip()
        self.output = self.root / "output"
        self.output.touch()
        self.script = self.root / "classify.py"
        self.script.write_text(self.step["run"], encoding="utf-8")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout

    def commit(self, changes):
        for name, content in changes.items():
            target = self.repo / name
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "-m", "synthetic change")

    def environment(self, **extra):
        return {
            **os.environ, "RELEASE_EVENT": "push", "RELEASE_BEFORE": self.before,
            "RELEASE_AFTER": self.git("rev-parse", "HEAD").strip(),
            "RELEASE_MANUAL_PROVISION": "null", "GITHUB_OUTPUT": str(self.output),
            **extra,
        }

    def run_scope(self, **extra):
        self.output.write_text("")
        result = subprocess.run(
            [sys.executable, str(self.script)], cwd=self.repo,
            env=self.environment(**extra), capture_output=True, text=True,
        )
        return result, self.output.read_text()

    def assert_scope(self, expected, **extra):
        result, output = self.run_scope(**extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, f"provision={expected}\n")

    def test_app_only_and_manual_opt_out_share_the_same_result(self):
        self.commit({"app/api/file.txt": "new", "app/web/spaced file.txt": "new"})
        self.assert_scope("false")
        for value in ("false", "true"):
            with self.subTest(manual=value):
                self.assert_scope(value, RELEASE_EVENT="workflow_dispatch",
                                  RELEASE_MANUAL_PROVISION=value, RELEASE_BEFORE="")

    def test_every_non_app_path_preserves_reconciliation(self):
        for path in ("infra/model.json", "foundry/toolbox.json", "proxy/config.txt",
                     "azure.yaml", ".github/workflows/deploy.yml", "scripts/release.py",
                     "docs/note.md", "application/not-app.txt"):
            with self.subTest(path=path):
                self.commit({path: "changed", "app/api/file.txt": path})
                self.assert_scope("true")

    def test_entire_push_range_includes_earlier_infrastructure_commits(self):
        self.commit({"infra/model.json": "changed"})
        self.commit({"app/api/file.txt": "new"})
        self.assert_scope("true")

    def test_deletions_and_renames_include_the_old_non_app_path(self):
        self.commit({"infra/model.json": None, "app/api/moved.json": "{}"})
        self.assert_scope("true")
        self.before = self.git("rev-parse", "HEAD").strip()
        self.commit({"app/api/moved.json": None})
        self.assert_scope("false")

    def test_non_app_deletion_is_not_hidden_by_app_changes(self):
        self.commit({"infra/model.json": None, "app/api/file.txt": "new"})
        self.assert_scope("true")

    def test_invalid_or_unavailable_history_refuses_without_output(self):
        self.commit({"app/api/file.txt": "new"})
        for changes in (
            {"RELEASE_BEFORE": ""}, {"RELEASE_BEFORE": "0" * 40},
            {"RELEASE_BEFORE": "f" * 40}, {"RELEASE_BEFORE": "--all"},
            {"RELEASE_AFTER": "0" * 40}, {"RELEASE_AFTER": self.before},
            {"RELEASE_EVENT": "pull_request"},
            {"RELEASE_EVENT": "workflow_dispatch", "RELEASE_MANUAL_PROVISION": "null"},
        ):
            with self.subTest(changes=changes):
                result, output = self.run_scope(**changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "")
                self.assertIn("refusing release", result.stderr)

    def test_empty_and_non_ancestor_ranges_refuse(self):
        self.commit({})
        result, output = self.run_scope()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.commit({"app/api/file.txt": "branch"})
        branch = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "--quiet", "--detach", self.before)
        self.commit({"app/api/file.txt": "different"})
        result, output = self.run_scope(RELEASE_BEFORE=branch)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.before = self.git("rev-parse", "HEAD").strip()
        self.commit({"app/api/file.txt": "valid"})
        self.assert_scope("false")

    def test_shallow_history_refuses_instead_of_using_the_last_commit(self):
        self.commit({"infra/model.json": "changed"})
        self.commit({"app/api/file.txt": "new"})
        clone = self.root / "shallow"
        subprocess.run(
            ["git", "clone", "--quiet", "--depth=1", self.repo.as_uri(), str(clone)],
            check=True, capture_output=True,
        )
        complete = self.repo
        self.repo = clone
        result, output = self.run_scope()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.repo = complete
        self.assert_scope("true")

    def test_failed_or_malformed_git_inventory_cannot_publish_a_decision(self):
        for failure in (subprocess.CalledProcessError(1, "git"),
                        subprocess.TimeoutExpired("git", 60),
                        FileNotFoundError("git"), b"", b"app/api/file.txt",
                        b"app/api/file.txt\0\0"):
            def fake_git(args, **kwargs):
                if args[1] == "diff":
                    if isinstance(failure, Exception):
                        raise failure
                    return subprocess.CompletedProcess(args, 0, stdout=failure)
                return subprocess.CompletedProcess(args, 0, stdout=b"a" * 40 + b"\n")

            with self.subTest(failure=failure), mock.patch.dict(
                os.environ, self.environment(RELEASE_BEFORE="b" * 40, RELEASE_AFTER="a" * 40)
            ), mock.patch("subprocess.run", side_effect=fake_git):
                with self.assertRaisesRegex(SystemExit, "refusing release"):
                    exec(compile(self.step["run"], "<workflow>", "exec"), {})
                self.assertEqual(self.output.read_text(), "")
        self.commit({"app/api/file.txt": "valid"})
        self.assert_scope("false")

    def test_classifier_precedes_cloud_login_and_requires_complete_checkout(self):
        checkout = next(step for step in self.steps if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertEqual(self.step["shell"], "python")
        self.assertEqual(self.step["env"], {
            "RELEASE_EVENT": "${{ github.event_name }}",
            "RELEASE_BEFORE": "${{ github.event.before }}",
            "RELEASE_AFTER": "${{ github.sha }}",
            "RELEASE_MANUAL_PROVISION": "${{ toJSON(inputs.provision) }}",
        })
        index = self.steps.index(self.step)
        self.assertNotIn("if", self.step)
        for position, step in enumerate(self.steps):
            if "azd auth login" in step.get("run", "") or step.get("uses", "").startswith("azure/login@"):
                self.assertLess(index, position)

    def test_only_provision_steps_gain_the_reduction_gate(self):
        gated = {
            "Resolve deploy principal ID for Foundry and API Center data-plane setup",
            "Preflight resource provider registration",
            "Preflight custom-domain bindings (guard wipes, register first binds)",
            "Provision infrastructure",
        }
        selected = [step for step in self.steps if "steps.reconciliation.outputs" in step.get("if", "")
                    and step.get("id") != "outputs"]
        self.assertEqual({step["name"] for step in selected}, gated)
        for step in selected:
            authority = ("steps.release_mode.outputs.operation == 'ordinary'"
                         if step["name"].startswith("Resolve deploy principal")
                         else "steps.execution.outputs.allowed == 'true'")
            self.assertEqual(step["if"], "${{ " + authority
                             + " && (steps.reconciliation.outputs.provision == 'true') }}")
            for admitted in ("false", "true"):
                for provision in ("", "false", "true"):
                    self.assertEqual(PrivatePreviewWorkflowTests.expression(step["if"], {
                        "steps.release_mode.outputs.operation": "ordinary" if admitted == "true" else "preview",
                        "steps.execution.outputs.allowed": admitted,
                        "steps.reconciliation.outputs.provision": provision,
                    }), admitted == "true" and provision == "true")


AZD_OUTPUT_STUB = """#!/usr/bin/env bash
echo "$*" >> "$AZD_CALLS_FILE"
case "$1 $2" in
  "env refresh")
    if [ -e "$AZD_STATE_DIR/refresh_fail" ]; then echo "ERROR: refresh failed" >&2; exit 1; fi
    exit 0 ;;
  "env get-value")
    if [ -f "$AZD_STATE_DIR/outputs/$3" ]; then cat "$AZD_STATE_DIR/outputs/$3"; exit 0; fi
    echo "ERROR: key '$3' not found in the environment values" >&2
    exit 1 ;;
  "env set")
    if [ "$#" -ne 4 ]; then echo "unexpected env set arity" >&2; exit 98; fi
    if [ ! -e "$AZD_STATE_DIR/set_noop" ]; then printf '%s' "$4" > "$AZD_STATE_DIR/outputs/$3"; fi
    exit 0 ;;
esac
echo "unexpected azd invocation: $*" >&2
exit 97
"""

PYTHON_RECORDER_STUB = """#!/usr/bin/env bash
echo "python $*" >> "$AZD_CALLS_FILE"
exit 0
"""


@unittest.skipIf(find_bash() is None, "bash is unavailable on this machine")
class ExistingOutputsWorkflowTests(unittest.TestCase):
    """An app-only release loads, and proves, the outputs it skipped provisioning for."""

    SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"
    VALID = {
        "AZURE_RESOURCE_GROUP": "rg-ai4ia-slurmfactory",
        "AZURE_LOCATION": "eastus2",
        "AZURE_API_NETWORK_PHASE": "private",
        "AZURE_API_APP_NAME": "ca-api-private-slurmfactory",
        "AZURE_WEB_APP_NAME": "ca-web-slurmfactory",
        "AZURE_PROXY_APP_NAME": "ca-proxy-slurmfactory",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": "crai4iaslurmfactory.azurecr.io",
        "AZURE_APIM_RESOURCE_ID": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-ai4ia-slurmfactory"
            "/providers/Microsoft.ApiManagement/service/apim-ai4ia-slurmfactory"
        ),
    }

    def setUp(self):
        self.steps = yaml.safe_load(DEPLOY_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        self.names = [step.get("name") for step in self.steps]
        self.step = next(step for step in self.steps if step.get("id") == "outputs")

    def position(self, name):
        return self.names.index(name)

    def test_refresh_runs_only_for_admitted_releases_that_skip_provisioning(self):
        self.assertEqual(self.step["name"], "Load existing infrastructure outputs without provisioning")
        self.assertEqual(self.step["shell"], "bash")
        self.assertEqual(
            self.step["if"],
            "${{ steps.execution.outputs.allowed == 'true'"
            " && (steps.reconciliation.outputs.provision != 'true') }}",
        )
        provision = next(step for step in self.steps if step.get("id") == "provision")
        for admitted in ("false", "true"):
            for value in ("", "false", "true"):
                values = {"steps.execution.outputs.allowed": admitted,
                          "steps.reconciliation.outputs.provision": value}
                loads = PrivatePreviewWorkflowTests.expression(self.step["if"], values)
                provisions = PrivatePreviewWorkflowTests.expression(provision["if"], values)
                self.assertEqual(loads, admitted == "true" and value != "true")
                # Exactly one of the two output sources runs for admitted execution.
                self.assertEqual(loads or provisions, admitted == "true")
                self.assertFalse(loads and provisions)

    def test_refresh_is_read_only_and_never_falls_back_to_provisioning(self):
        run = self.step["run"]
        self.assertTrue(run.startswith("set -euo pipefail\n"))
        commands = re.findall(r"\bazd\s+[a-z-]+(?:\s+[a-z-]+)?", run)
        self.assertEqual(sorted(set(commands)), ["azd env get-value", "azd env refresh", "azd env set"])
        self.assertIn("azd env refresh --no-prompt\n", run)
        # The only local write: the bounded pre-recovery api retarget.
        self.assertEqual(re.findall(r"azd env set [^\n]*", run),
                         ['azd env set AZURE_API_APP_NAME "$RELEASE_API_APP_NAME"'])
        for forbidden in ("azd provision", "azd up", "az deployment", "continue-on-error"):
            self.assertNotIn(forbidden, run)
        self.assertEqual(self.step["env"], {
            "RELEASE_API_APP_NAME": "${{ steps.release_mode.outputs.api_app_name }}",
            "RELEASE_MIGRATION_PENDING": "${{ steps.release_mode.outputs.migration_pending }}",
            "RELEASE_EXECUTION_BLOCKERS": "${{ steps.release_mode.outputs.execution_blockers }}",
        })
        self.assertNotIn("continue-on-error", self.step)
        provision_run = next(step for step in self.steps if step.get("id") == "provision")["run"]
        self.assertEqual(provision_run, "azd provision --no-prompt --no-state")

    def test_refresh_precedes_every_output_consumer_and_follows_provisioning(self):
        index = self.steps.index(self.step)
        self.assertLess(self.position("Provision infrastructure"), index)
        self.assertLess(self.position("Refresh the Azure CLI login after provisioning"), index)
        for consumer in ("Verify upstream Claude gateway routes before image rollout",
                         "Deploy application"):
            self.assertLess(index, self.position(consumer))
        for position, step in enumerate(self.steps):
            run = step.get("run", "")
            if position != index and ("azd env get-value" in run or "azd deploy" in run):
                self.assertLess(index, position, step.get("name"))

    def test_gateway_check_refuses_an_empty_apim_id(self):
        gateway = next(step for step in self.steps
                       if step.get("name") == "Verify upstream Claude gateway routes before image rollout")
        self.assertIn("set -euo pipefail", gateway["run"])
        outputs = dict(self.VALID)
        code, out, calls = self.execute(gateway["run"], outputs={**outputs, "AZURE_APIM_RESOURCE_ID": ""})
        self.assertNotEqual(code, 0, out)
        self.assertIn("refusing to skip the Claude gateway route check", out)
        self.assertFalse([call for call in calls if call.startswith("python")])
        code, out, calls = self.execute(gateway["run"], outputs=outputs)
        self.assertEqual(code, 0, out)
        self.assertIn("python scripts/check-claude-binding.py --routed --source-apim-resource-id "
                      + outputs["AZURE_APIM_RESOURCE_ID"], calls)

    def execute(self, script, *, outputs, refresh_fails=False, set_noop=False, **env_overrides):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            (state / "outputs").mkdir(parents=True)
            for name, value in outputs.items():
                if value is not None:
                    (state / "outputs" / name).write_text(value, newline="\n")
            if refresh_fails:
                (state / "refresh_fail").write_text("")
            if set_noop:
                (state / "set_noop").write_text("")
            calls = root / "calls.txt"
            calls.write_text("")
            bindir = root / "bin"
            bindir.mkdir()
            for name, body in (("azd", AZD_OUTPUT_STUB), ("python", PYTHON_RECORDER_STUB)):
                stub = bindir / name
                stub.write_text(body, newline="\n")
                stub.chmod(0o755)
            step = root / "step.sh"
            step.write_text(script.replace("\r\n", "\n"), newline="\n")

            def posix(path: Path) -> str:
                text = str(path).replace("\\", "/")
                if len(text) > 1 and text[1] == ":":
                    text = "/" + text[0].lower() + text[2:]
                return text

            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AZURE_", "AI4IA_"))}
            env.update({
                "AZURE_ENV_NAME": "slurmfactory", "AZURE_LOCATION": "EastUS2",
                "AZURE_SUBSCRIPTION_ID": self.SUBSCRIPTION,
                "RELEASE_API_APP_NAME": "ca-api-private-slurmfactory",
                "RELEASE_MIGRATION_PENDING": "false", "RELEASE_EXECUTION_BLOCKERS": "[]",
                "AI4IA_API_NETWORK_PHASE": "private", "AI4IA_CLAUDE_GATEWAY_ENABLED": "true",
                "AI4IA_WORKLOAD": "",
                "AZD_STATE_DIR": posix(state), "AZD_CALLS_FILE": posix(calls),
                "PATH": posix(bindir) + ":" + os.environ.get("PATH", ""),
            })
            env.update(env_overrides)
            env = {key: value for key, value in env.items() if value is not None}
            result = subprocess.run([find_bash(), str(step)], env=env,
                                    capture_output=True, text=True, check=False)
            self.final_outputs = {path.name: path.read_text() for path in (state / "outputs").iterdir()}
            return result.returncode, result.stdout + result.stderr, calls.read_text().splitlines()

    def assert_loaded(self, outputs=None, *, retargets=False, **env):
        code, out, calls = self.execute(self.step["run"], outputs=self.VALID if outputs is None else outputs, **env)
        self.assertEqual(code, 0, out)
        self.assertEqual(calls[0], "env refresh --no-prompt")
        sets = [call for call in calls if call.startswith("env set ")]
        self.assertEqual(sets, ["env set AZURE_API_APP_NAME ca-api-private-slurmfactory"] if retargets else [])
        self.assertTrue(all(call.startswith("env get-value ") for call in calls[1:] if call not in sets), calls)
        return out

    def assert_refused(self, message, outputs=None, **env):
        code, out, calls = self.execute(self.step["run"], outputs=self.VALID if outputs is None else outputs, **env)
        self.assertNotEqual(code, 0, out)
        self.assertIn(message, out)
        self.assertIn("::error::", out)
        self.assertFalse([call for call in calls if "provision" in call], calls)
        self.assertFalse([call for call in calls if call.startswith("env set ")], calls)

    def test_valid_existing_outputs_load_without_provisioning(self):
        self.assertIn("no provisioning ran", self.assert_loaded())

    def test_failed_refresh_stops_the_release(self):
        code, out, calls = self.execute(self.step["run"], outputs=self.VALID, refresh_fails=True)
        self.assertNotEqual(code, 0, out)
        self.assertEqual(calls, ["env refresh --no-prompt"])

    def test_absent_or_empty_required_outputs_fail_closed(self):
        for name in ("AZURE_RESOURCE_GROUP", "AZURE_LOCATION", "AZURE_CONTAINER_REGISTRY_ENDPOINT",
                     "AZURE_API_APP_NAME", "AZURE_WEB_APP_NAME", "AZURE_PROXY_APP_NAME"):
            for value in (None, ""):
                with self.subTest(name=name, value=value):
                    self.assert_refused(f"has no {name} output", {**self.VALID, name: value})
        # No deployment found: `env refresh` exits 0 with nothing written.
        self.assert_refused("has no AZURE_RESOURCE_GROUP output", {})

    def test_gateway_requires_a_valid_apim_id_in_the_release_group(self):
        other_group = self.VALID["AZURE_APIM_RESOURCE_ID"].replace("rg-ai4ia-slurmfactory", "rg-other")
        other_subscription = self.VALID["AZURE_APIM_RESOURCE_ID"].replace(self.SUBSCRIPTION, "f" * 8 + self.SUBSCRIPTION[8:])
        for value in (None, "", other_group, other_subscription,
                      self.VALID["AZURE_APIM_RESOURCE_ID"] + "/apis/x",
                      self.VALID["AZURE_APIM_RESOURCE_ID"].rsplit("/", 1)[0] + "/"):
            with self.subTest(value=value):
                self.assert_refused("no valid AZURE_APIM_RESOURCE_ID",
                                    {**self.VALID, "AZURE_APIM_RESOURCE_ID": value})
        self.assert_loaded({**self.VALID, "AZURE_APIM_RESOURCE_ID": self.VALID["AZURE_APIM_RESOURCE_ID"].upper()})
        for disabled in ("false", "", None):
            with self.subTest(gateway=disabled):
                self.assert_loaded({**self.VALID, "AZURE_APIM_RESOURCE_ID": None},
                                   AI4IA_CLAUDE_GATEWAY_ENABLED=disabled)

    def test_outputs_must_name_the_validated_release_target(self):
        self.assert_refused("resource group does not match rg-ai4ia-slurmfactory",
                            {**self.VALID, "AZURE_RESOURCE_GROUP": "rg-ai4ia-other"})
        self.assert_loaded({**self.VALID, "AZURE_RESOURCE_GROUP": "rg-custom-slurmfactory",
                            "AZURE_APIM_RESOURCE_ID": self.VALID["AZURE_APIM_RESOURCE_ID"].replace(
                                "rg-ai4ia-", "rg-custom-")}, AI4IA_WORKLOAD="custom")
        self.assert_refused("location does not match", {**self.VALID, "AZURE_LOCATION": "swedencentral"})
        self.assert_refused("API network phase does not match",
                            {**self.VALID, "AZURE_API_NETWORK_PHASE": "activate"})
        self.assert_refused("API app does not match",
                            {**self.VALID, "AZURE_API_APP_NAME": "ca-api-slurmfactory"})
        self.assert_refused("did not report its validated API app target", RELEASE_API_APP_NAME="")
        self.assert_loaded({**self.VALID, "AZURE_API_NETWORK_PHASE": "legacy",
                            "AZURE_API_APP_NAME": "ca-api-slurmfactory"},
                           AI4IA_API_NETWORK_PHASE=None, RELEASE_API_APP_NAME="ca-api-slurmfactory")

    PRE_RECOVERY = {"AZURE_API_NETWORK_PHASE": None, "AZURE_API_APP_NAME": "ca-api-slurmfactory"}

    def test_a_completed_private_release_retargets_only_the_pre_recovery_api_output(self):
        for phase in (None, ""):
            with self.subTest(phase=phase):
                before = {**self.VALID, **self.PRE_RECOVERY, "AZURE_API_NETWORK_PHASE": phase}
                out = self.assert_loaded(before, retargets=True)
                self.assertIn("predates the private recovery", out)
                expected = {name: value for name, value in before.items() if value is not None}
                expected["AZURE_API_APP_NAME"] = "ca-api-private-slurmfactory"
                self.assertEqual(self.final_outputs, expected)

    def test_any_other_phase_less_shape_refuses_without_writing(self):
        message = "has no AZURE_API_NETWORK_PHASE output and is not the completed pre-recovery shape"
        base = {**self.VALID, **self.PRE_RECOVERY}
        for api in ("ca-api-private-slurmfactory", "ca-api-other", "ca-api-slurmfactory-old"):
            with self.subTest(api=api):
                self.assert_refused(message, {**base, "AZURE_API_APP_NAME": api})
        cases = [
            {"AI4IA_API_NETWORK_PHASE": None}, {"AI4IA_API_NETWORK_PHASE": "candidate"},
            {"AI4IA_API_NETWORK_PHASE": "activate"}, {"AI4IA_API_NETWORK_PHASE": "legacy"},
            {"RELEASE_MIGRATION_PENDING": "true"}, {"RELEASE_MIGRATION_PENDING": ""},
            {"RELEASE_MIGRATION_PENDING": None},
            {"RELEASE_EXECUTION_BLOCKERS": '["candidate_not_activated"]'},
            {"RELEASE_EXECUTION_BLOCKERS": ""}, {"RELEASE_EXECUTION_BLOCKERS": None},
            {"RELEASE_API_APP_NAME": "ca-api-slurmfactory"},
            {"RELEASE_API_APP_NAME": "ca-api-private-other"},
        ]
        for env in cases:
            with self.subTest(**{key: str(value) for key, value in env.items()}):
                self.assert_refused(message, base, **env)

    def test_a_present_phase_never_uses_the_allowance(self):
        # Stale legacy outputs carrying a phase must still refuse, not retarget.
        self.assert_refused("API network phase does not match", {
            **self.VALID, "AZURE_API_NETWORK_PHASE": "legacy", "AZURE_API_APP_NAME": "ca-api-slurmfactory"})
        self.assert_refused("API network phase does not match", {
            **self.VALID, "AZURE_API_NETWORK_PHASE": "activate"})
        self.assert_refused("API app does not match",
                            {**self.VALID, "AZURE_API_APP_NAME": "ca-api-slurmfactory"})

    def test_an_unpersisted_retarget_refuses(self):
        code, out, calls = self.execute(self.step["run"], outputs={**self.VALID, **self.PRE_RECOVERY},
                                        set_noop=True)
        self.assertNotEqual(code, 0, out)
        self.assertIn("Could not retarget the api service", out)
        self.assertIn("env set AZURE_API_APP_NAME ca-api-private-slurmfactory", calls)


class PrivatePreviewWorkflowTests(unittest.TestCase):
    """Drive the actual operation and workflow gate expressions, without Azure."""

    IMAGE_ONLY_STEPS = {
        "Build and push service images, recording their digests", "Bind production image subjects",
        "Generate production SPDX SBOMs", "Attest api build provenance", "Attest api SPDX SBOM",
        "Verify production image attestations", "Retain production image evidence",
    }

    @staticmethod
    def expression(text: str, values: dict[str, str]) -> bool:
        source = text.removeprefix("${{").removesuffix("}}").strip()
        for name, value in values.items():
            source = source.replace(name, repr(value))
        source = source.replace("&&", "and").replace("||", "or")
        node = ast.parse(source, mode="eval").body

        def evaluate(item):
            if isinstance(item, ast.Constant):
                return item.value
            if isinstance(item, ast.BoolOp):
                children = [evaluate(value) for value in item.values]
                return all(children) if isinstance(item.op, ast.And) else any(children)
            if isinstance(item, ast.Compare) and len(item.ops) == 1 and isinstance(item.ops[0], ast.Eq):
                return evaluate(item.left) == evaluate(item.comparators[0])
            if isinstance(item, ast.Compare) and len(item.ops) == 1 and isinstance(item.ops[0], ast.NotEq):
                return evaluate(item.left) != evaluate(item.comparators[0])
            raise AssertionError("Unexpected execution-gate syntax")
        return bool(evaluate(node))

    @staticmethod
    def guarded(step: dict) -> None:
        expression = step.get("if", "").removeprefix("${{").removesuffix("}}").strip()
        gate = "steps.execution.outputs.allowed == 'true'"
        if step.get("name") in PrivatePreviewWorkflowTests.IMAGE_ONLY_STEPS:
            alternative = gate + " || steps.image_execution.outputs.allowed == 'true'"
            expected = (f"({alternative}) && (always() && steps.subjects.outcome == 'success')"
                        if step["name"] == "Retain production image evidence" else alternative)
            assert expression == expected, step.get("name")
            return
        assert expression.startswith(gate), step.get("name")
        tail = expression[len(gate):].strip()
        if not tail:
            return
        assert tail.startswith("&& (") and tail.endswith(")"), step.get("name")
        depth = 0
        for index, char in enumerate(tail[3:]):
            depth += (char == "(") - (char == ")")
            assert depth >= 0
            if depth == 0:
                assert index == len(tail[3:]) - 1, "an OR can escape the execution gate"

    def test_pending_push_and_preview_success_cannot_dispatch_any_later_step(self):
        from scripts._api_private import deployment_operation

        document = yaml.safe_load(DEPLOY_WORKFLOW.read_text())
        steps = document["jobs"]["deploy"]["steps"]
        position = next(index for index, step in enumerate(steps) if step.get("id") == "execution")
        authority = steps[position]["if"]
        protected = steps[position + 1:]
        for step in protected:
            self.guarded(step)
        required_effects = ("Preflight resource provider registration",
                            "Preflight custom-domain bindings (guard wipes, register first binds)",
                            "Provision infrastructure", "Deploy application",
                            "Acquire the canary token for verification", "Roll back to the captured revisions")
        names = {step["name"] for step in protected}
        self.assertTrue(set(required_effects) <= names)
        self.assertTrue(any("Attest " in name for name in names))
        env = {"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push",
               "AI4IA_API_NETWORK_PHASE": "candidate"}
        operation = deployment_operation(env, pending=True, blocked=False)
        for provider_claim in ("false", "true"):
            allowed = self.expression(authority, {
                "steps.release_mode.outputs.operation": operation,
                "steps.private_preview.outputs.authorized": provider_claim,
            })
            # Even a successful or incorrectly authorizing preview cannot run
            # provider/domain mutations, images, attestations, canaries or rollback.
            dispatched = [step["name"] for step in protected if allowed]
            self.assertEqual(dispatched, [])
        ordinary = deployment_operation({**env, "AI4IA_API_NETWORK_PHASE": "private"},
                                        pending=False, blocked=False)
        self.assertTrue(self.expression(authority, {
            "steps.release_mode.outputs.operation": ordinary,
            "steps.private_preview.outputs.authorized": "false",
        }))
        self.assertFalse(self.expression(authority, {
            "steps.release_mode.outputs.operation": "deploy",
            "steps.private_preview.outputs.authorized": "false",
        }))
        self.assertFalse(self.expression(authority, {
            "steps.release_mode.outputs.operation": "deploy",
            "steps.private_preview.outputs.authorized": "true",
        }))
        apply = next(step for step in steps if step.get("id") == "private_apply")
        for operation, claimed, expected in (("preview", "true", False), ("deploy", "false", False),
                                             ("deploy", "true", True), ("image", "true", False)):
            self.assertEqual(self.expression(apply["if"], {
                "steps.release_mode.outputs.operation": operation,
                "steps.private_preview.outputs.authorized": claimed,
            }), expected)

    def test_image_operation_has_only_the_narrow_registry_lane(self):
        from scripts._api_private import deployment_operation
        env = {"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
               "AI4IA_API_NETWORK_PHASE": "candidate", "AI4IA_RELEASE_OPERATION": "image",
               "AI4IA_RELEASE_PROVISION": "false"}
        self.assertEqual(deployment_operation(env, pending=True, blocked=False), "image")
        for update in ({"GITHUB_EVENT_NAME": "push"}, {"AI4IA_RELEASE_PROVISION": "true"},
                       {"AI4IA_PRIVATE_PREVIEW_BINDING": "held"}, {"AI4IA_API_NETWORK_PHASE": "private"},
                       {"AI4IA_PRIVATE_IMAGE_RECEIPT": "{}"}):
            with self.assertRaises(ValueError):
                deployment_operation({**env, **update}, pending=True, blocked=False)
        steps = yaml.safe_load(DEPLOY_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        authority = next(step for step in steps if step.get("id") == "image_execution")["if"]
        for mode in ("ordinary", "preview", "deploy", "image"):
            self.assertEqual(self.expression(authority, {"steps.release_mode.outputs.operation": mode}), mode == "image")
        for step in steps:
            if "steps.image_execution.outputs.allowed" in step.get("if", ""):
                self.assertIn(step["name"], self.IMAGE_ONLY_STEPS)
                self.guarded(step)
        self.assertEqual(sum("steps.image_execution.outputs.allowed" in step.get("if", "") for step in steps),
                         len(self.IMAGE_ONLY_STEPS))

    def test_post_create_failure_receipt_is_retained_without_authorizing_work(self):
        steps = yaml.safe_load(DEPLOY_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        receipt = next(step for step in steps if step["name"] == "Retain scoped execution receipt")
        self.assertEqual(receipt["if"], "${{ always() && steps.private_apply.outputs.receipt == 'true' }}")
        self.assertTrue(receipt["uses"].startswith("actions/upload-artifact@"))
        self.assertTrue(receipt["with"]["path"].endswith("/execution.json"))
        for step in steps:
            if "steps.private_apply.outputs.receipt" in step.get("if", ""):
                self.assertIs(step, receipt)

    def test_each_removed_or_escaped_write_guard_is_detected(self):
        steps = yaml.safe_load(DEPLOY_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        position = next(index for index, step in enumerate(steps) if step.get("id") == "execution")
        for step in steps[position + 1:]:
            with self.subTest(step=step["name"]):
                self.guarded(step)
                changed = deepcopy(step)
                changed.pop("if")
                with self.assertRaises(AssertionError):
                    self.guarded(changed)
                changed["if"] = "${{ steps.execution.outputs.allowed == 'true' || true }}"
                with self.assertRaises(AssertionError):
                    self.guarded(changed)

    def test_preview_artifact_has_no_parameter_payload_and_pin_matches_existing(self):
        steps = yaml.safe_load(DEPLOY_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        retain = next(step for step in steps if step["name"] == "Retain sanitized private preview")
        existing = next(step for step in steps if step["name"] == "Retain production image evidence")
        self.assertEqual(retain["uses"], existing["uses"])
        self.assertEqual(retain["with"]["path"], "${{ runner.temp }}/ai4ia-private-preview/report.json")
        install = next(step for step in steps if step["name"] == "Install the same pinned Bicep as infra validation")
        infra = yaml.safe_load((WORKFLOWS / "infra-validate.yml").read_text())
        versions = [step["env"]["BICEP_VERSION"] for job in infra["jobs"].values()
                    for step in job["steps"] if "BICEP_VERSION" in step.get("env", {})]
        self.assertEqual(versions, [install["env"]["BICEP_VERSION"]])


if __name__ == "__main__":
    unittest.main()
