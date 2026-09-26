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
import os
import re
import subprocess
import sys
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
                "scripts/verify-companion-image.py",
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
        self.assertEqual(routed["if"], reader["if"])
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
        # Nothing else: no `if:` (neither an always() that outlives a failure nor
        # a provision-only gate), no separate CLI profile, no error suppression.
        _require(set(refresh) == {"name", "uses", "with"}, f"refresh keys {sorted(refresh)}")
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
    "AI4IA_PROXY_PROFILE_PROJECTION_JSON": '[{"appId": "synthetic-app"}]',
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
            "extra environment": edit(env={"AI4IA_PROXY_PROFILE_PROJECTION_JSON": "${{ secrets.OTHER }}"}),
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

    def test_the_step_masks_the_secret_before_anything_else_it_prints(self) -> None:
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
        secret = written["AI4IA_PROXY_PROFILE_PROJECTION_JSON_B64"]
        self.assertEqual(result.stdout.splitlines()[0], f"::add-mask::{secret}")
        self.assertEqual(result.stdout.count(secret), 1)
        self.assertNotIn(secret, result.stderr)

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
        self.assertEqual(
            env["AI4IA_PROXY_PROFILE_PROJECTION_JSON"], "${{ secrets.AI4IA_PROXY_PROFILE_PROJECTION_JSON }}"
        )
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
            # The canary CLI's only GitHub REST consumer reads an exact prior
            # workflow/run/artifact. Its observe command exchanges runner OIDC
            # directly for the dedicated API audience, with no ARM login.
            if re.search(r"\bpython(?:3)?\s+-m\s+scripts\.canaries\s+locate\b", script):
                require({"actions": "read"})
            if re.search(r"\bpython(?:3)?\s+-m\s+scripts\.canaries\s+observe\b", script):
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


if __name__ == "__main__":
    unittest.main()
