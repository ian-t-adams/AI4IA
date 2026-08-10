"""Unit tests for scripts/post-deploy-verify.py, the post-deploy gate.

This gate is the only thing that will ever tell the repo that a deploy did not
work. It runs exclusively in `deploy.yml`, against live Azure, at the one moment
nobody is watching -- so every branch in it has to be exercised here or it is
untested forever. `az` is stubbed at the process boundary (the module's
``run_az``), and HTTP is stubbed at ``http_request``, so these tests make no
network calls and mutate nothing.

Three things are load-bearing and get disproportionate attention:

* **The unchanged-revision assertion.** It is the single check that catches the
  failure the audit named: `azd deploy` exiting 0 without Container Apps ever
  promoting a new template.
* **Rollback target selection.** Container Apps' two revision modes need two
  different primitives, and picking the wrong one is a silent no-op -- the app
  keeps serving the broken release while the log says "restored".
* **Redaction.** The canary holds a bearer token and receives model output.
  Neither may ever reach a retained CI log.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "post-deploy-verify.py"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


def load_script():
    spec = importlib.util.spec_from_file_location("post_deploy_verify", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging failure
        raise RuntimeError("Unable to load the post-deploy verification script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pdv = load_script()

STATE_FILE = "state.json"
# The image a captured revision was running, and therefore the image the app must
# be back on before a rollback may claim it restored anything.
RESTORED_IMAGE = "acr.azurecr.io/x:1"


def container_app(
    *,
    name: str = "ca-api-slurmfactory",
    revision: str = "ca-api-slurmfactory--r2",
    mode: str = "Single",
    min_replicas: int | None = 1,
    image: str = "acr.azurecr.io/api:azd-deploy-2",
    fqdn: str | None = "ca-api-slurmfactory.eastus2.azurecontainerapps.io",
    custom_domains: list[dict] | None = None,
    traffic: list[dict] | None = None,
) -> dict:
    """An `az containerapp show` payload, shaped the way ARM actually returns it."""

    return {
        "name": name,
        "properties": {
            "latestReadyRevisionName": revision,
            "latestRevisionName": revision,
            "configuration": {
                "activeRevisionsMode": mode,
                "ingress": {
                    "fqdn": fqdn,
                    "traffic": (
                        traffic
                        if traffic is not None
                        else [{"latestRevision": True, "weight": 100}]
                    ),
                    "customDomains": custom_domains or [],
                },
            },
            "template": {
                "scale": {"minReplicas": min_replicas},
                "containers": [{"name": "api", "image": image}],
            },
        },
    }


def revision_payload(
    *,
    active: bool = True,
    health: str = "Healthy",
    running: str = "Running",
    replicas: int = 1,
) -> dict:
    return {
        "properties": {
            "active": active,
            "healthState": health,
            "runningState": running,
            "replicas": replicas,
        }
    }


class FakeAz:
    """Stand-in for the `az` CLI. Records argv, answers from a fixture dict."""

    def __init__(
        self,
        *,
        apps: dict[str, dict] | None = None,
        revisions: dict[tuple[str, str], dict] | None = None,
        failing_writes: set[str] | None = None,
    ) -> None:
        self.apps = apps or {}
        self.revisions = revisions or {}
        self.failing_writes = failing_writes or set()
        self.calls: list[list[str]] = []

    def __call__(self, args, *, timeout: float = 180.0) -> tuple[int, str, str]:
        argv = list(args)
        self.calls.append(argv)
        joined = " ".join(argv)
        name = self._flag(argv, "-n")

        if "revision show" in joined:
            revision = self._flag(argv, "--revision") or ""
            payload = self.revisions.get((name or "", revision))
            if payload is None:
                return 1, "", "ERROR: revision not found"
            return 0, json.dumps(payload), ""
        if "containerapp show" in joined:
            payload = self.apps.get(name or "")
            if payload is None:
                return 1, "", "ERROR: (ResourceNotFound) app not found"
            return 0, json.dumps(payload), ""
        if "revision copy" in joined or "ingress traffic set" in joined:
            if name in self.failing_writes:
                return 1, "", "ERROR: (RevisionOperationFailed) could not restore"
            # Model the real effect: `revision copy` clones the SOURCE revision's
            # template, so the app ends up on a new revision running the captured
            # image. Without this, confirm_restored could never confirm and the
            # tests would prove nothing about the confirmation path.
            source = self._flag(argv, "--from-revision")
            app = self.apps.get(name or "")
            if app is not None and source:
                app["properties"]["latestReadyRevisionName"] = f"{source}-restored"
                app["properties"]["template"]["containers"][0]["image"] = RESTORED_IMAGE
            return 0, "", ""
        return 0, "", ""

    @staticmethod
    def _flag(argv: list[str], flag: str) -> str | None:
        for index, value in enumerate(argv):
            if value == flag and index + 1 < len(argv):
                return argv[index + 1]
        return None


def inert_az(base: FakeAz, handler) -> Any:
    """Wrap a FakeAz with a custom handler while keeping its recorded calls."""

    class WrappedAz:
        apps = base.apps
        failing_writes = base.failing_writes

        @property
        def calls(self) -> list[list[str]]:
            return base.calls

        def __call__(self, args, **kwargs):
            return handler(args, **kwargs)

    return WrappedAz()


class FakeHttp:
    """Scripted responses keyed by ``METHOD path-suffix``, consumed in order."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, str]] = []
        self.headers: list[dict[str, str]] = []
        self.bodies: list[bytes | None] = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append((method, url))
        self.headers.append(dict(headers or {}))
        self.bodies.append(body)
        for key, responses in self.script.items():
            want_method, _, suffix = key.partition(" ")
            if method == want_method and url.endswith(suffix) and responses:
                return responses.pop(0) if len(responses) > 1 else responses[0]
        return pdv.HttpOutcome(status=404)


def ok(payload: Any, status: int = 200) -> Any:
    return pdv.HttpOutcome(status=status, body=json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# reading the Container App shape
# ---------------------------------------------------------------------------


class TrafficRevisionTests(unittest.TestCase):
    def test_single_revision_mode_resolves_the_latest_ready_name(self) -> None:
        """`Single` mode reports no revision NAME, only ``latestRevision: true``."""
        app = container_app(revision="ca-api-x--abc")
        self.assertEqual(pdv.traffic_revision(app), "ca-api-x--abc")

    def test_multiple_revision_mode_picks_the_heaviest_named_target(self) -> None:
        """Reading latestReadyRevisionName here would name a revision taking 0%."""
        app = container_app(
            mode="Multiple",
            revision="ca-api-x--new",
            traffic=[
                {"revisionName": "ca-api-x--new", "weight": 20},
                {"revisionName": "ca-api-x--old", "weight": 80},
            ],
        )
        self.assertEqual(pdv.traffic_revision(app), "ca-api-x--old")

    def test_zero_weight_targets_are_not_serving(self) -> None:
        """A fully drained app answers nothing; saying otherwise passes a dead deploy."""
        app = container_app(
            mode="Multiple",
            traffic=[{"revisionName": "ca-api-x--drained", "weight": 0}],
            revision="ca-api-x--live",
        )
        self.assertIsNone(pdv.traffic_revision(app))

    def test_missing_traffic_block_falls_back_to_latest_ready(self) -> None:
        app = container_app(traffic=[], revision="ca-api-x--only")
        self.assertEqual(pdv.traffic_revision(app), "ca-api-x--only")

    def test_unrecognisable_payload_reports_nothing_rather_than_guessing(self) -> None:
        self.assertIsNone(pdv.traffic_revision({"properties": {}}))
        self.assertIsNone(pdv.traffic_revision(None))

    def test_the_other_readers_tolerate_missing_sections(self) -> None:
        app = container_app(
            min_replicas=0,
            custom_domains=[{"name": "ai4ia.example.test", "bindingType": "SniEnabled"}],
        )
        self.assertEqual(pdv.revisions_mode(app), "Single")
        self.assertEqual(pdv.min_replicas(app), 0)
        self.assertEqual(pdv.container_image(app), "acr.azurecr.io/api:azd-deploy-2")
        self.assertTrue(pdv.ingress_fqdn(app).endswith("azurecontainerapps.io"))
        self.assertEqual(
            pdv.bound_custom_domains(app), {"ai4ia.example.test": "SniEnabled"}
        )
        self.assertEqual(pdv.revisions_mode({}), "Single")
        self.assertIsNone(pdv.min_replicas({}))
        self.assertIsNone(pdv.container_image({}))
        self.assertEqual(pdv.bound_custom_domains({}), {})


class NamingTests(unittest.TestCase):
    def test_resource_group_matches_the_bicep(self) -> None:
        """Hardcoding the workload token would send every lookup to a missing group."""
        self.assertEqual(
            pdv.resource_group_name("nomad", "slurmfactory"), "rg-nomad-slurmfactory"
        )
        self.assertEqual(
            pdv.resource_group_name("", "slurmfactory"), "rg-ai4ia-slurmfactory"
        )

    def test_app_names_match_the_bicep(self) -> None:
        self.assertEqual(
            pdv.app_names("slurmfactory"),
            {
                "api": "ca-api-slurmfactory",
                "web": "ca-web-slurmfactory",
                "proxy": "ca-proxy-slurmfactory",
            },
        )

    def test_the_derived_names_agree_with_infra(self) -> None:
        """A rename in bicep must not leave this gate silently checking nothing."""
        bicep = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                "infra/main.bicep",
                "infra/modules/api.bicep",
                "infra/modules/gateway.bicep",
            )
        )
        for prefix in pdv.APP_NAME_PREFIX.values():
            self.assertIn(f"'{prefix}${{environmentName}}'", bicep)
        self.assertIn("'rg-${workload}-${environmentName}'", bicep)


# ---------------------------------------------------------------------------
# rollout assertions
# ---------------------------------------------------------------------------


class RolloutProblemTests(unittest.TestCase):
    def test_a_clean_rollout_reports_nothing(self) -> None:
        self.assertEqual(
            pdv.rollout_problems(
                service="api",
                previous_revision="ca-api-x--r1",
                current_revision="ca-api-x--r2",
                revision_detail=revision_payload(),
                require_replicas=True,
            ),
            [],
        )

    def test_an_unchanged_revision_is_the_silent_no_op_deploy(self) -> None:
        """The whole reason this gate exists: green azd, nothing promoted."""
        problems = pdv.rollout_problems(
            service="api",
            previous_revision="ca-api-x--r1",
            current_revision="ca-api-x--r1",
            revision_detail=revision_payload(),
            require_replicas=True,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("still serving the pre-deploy revision", problems[0])

    def test_a_greenfield_first_deploy_has_no_previous_to_compare(self) -> None:
        self.assertEqual(
            pdv.rollout_problems(
                service="web",
                previous_revision=None,
                current_revision="ca-web-x--r1",
                revision_detail=revision_payload(),
                require_replicas=True,
            ),
            [],
        )

    def test_no_revision_taking_traffic_short_circuits(self) -> None:
        problems = pdv.rollout_problems(
            service="web",
            previous_revision="ca-web-x--r1",
            current_revision=None,
            revision_detail=revision_payload(),
            require_replicas=True,
        )
        self.assertEqual(problems, ["web: no revision is receiving traffic"])

    def test_an_unhealthy_or_inactive_revision_fails(self) -> None:
        problems = pdv.rollout_problems(
            service="api",
            previous_revision="ca-api-x--r1",
            current_revision="ca-api-x--r2",
            revision_detail=revision_payload(
                active=False, health="Unhealthy", running="Failed", replicas=0
            ),
            require_replicas=True,
        )
        joined = " | ".join(problems)
        self.assertIn("is not active", joined)
        self.assertIn("healthState is Unhealthy", joined)
        self.assertIn("runningState is Failed", joined)
        self.assertIn("running replicas", joined)

    def test_a_crash_looping_image_shows_up_as_zero_replicas(self) -> None:
        problems = pdv.rollout_problems(
            service="api",
            previous_revision="ca-api-x--r1",
            current_revision="ca-api-x--r2",
            revision_detail=revision_payload(
                health="Healthy", running="Running", replicas=0
            ),
            require_replicas=True,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("0 running replicas", problems[0])

    def test_scale_to_zero_is_only_acceptable_when_it_is_configured(self) -> None:
        """A minReplicas=0 proxy that has scaled down is correct, not broken.

        The same state on a minReplicas>=1 app means the replicas died.
        """
        idle = revision_payload(running="ScaledToZero", replicas=0)
        self.assertEqual(
            pdv.rollout_problems(
                service="proxy",
                previous_revision="ca-proxy-x--r1",
                current_revision="ca-proxy-x--r2",
                revision_detail=idle,
                require_replicas=False,
            ),
            [],
        )
        self.assertTrue(
            pdv.rollout_problems(
                service="proxy",
                previous_revision="ca-proxy-x--r1",
                current_revision="ca-proxy-x--r2",
                revision_detail=idle,
                require_replicas=True,
            )
        )

    def test_a_new_revision_running_the_old_image_fails(self) -> None:
        """A revision this deploy did not produce.

        This is the FALLBACK assertion, used when the caller cannot name the
        image it deployed. It was sound while azd tagged every build
        `azd-deploy-<unix-ts>`; deploy.yml now passes --expect-image instead,
        because a content-addressed digest repeats for identical content.
        """
        problems = pdv.rollout_problems(
            service="api",
            previous_revision="ca-api-x--r1",
            current_revision="ca-api-x--r2",
            revision_detail=revision_payload(),
            require_replicas=True,
            previous_image="acr.azurecr.io/api:azd-deploy-1",
            current_image="acr.azurecr.io/api:azd-deploy-1",
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("still runs the pre-deploy image", problems[0])

    def test_a_changed_image_under_a_changed_revision_passes(self) -> None:
        self.assertEqual(
            pdv.rollout_problems(
                service="api",
                previous_revision="ca-api-x--r1",
                current_revision="ca-api-x--r2",
                revision_detail=revision_payload(),
                require_replicas=True,
                previous_image="acr.azurecr.io/api:azd-deploy-1",
                current_image="acr.azurecr.io/api:azd-deploy-2",
            ),
            [],
        )

    def test_an_unknown_previous_image_does_not_invent_a_failure(self) -> None:
        self.assertEqual(
            pdv.rollout_problems(
                service="api",
                previous_revision="ca-api-x--r1",
                current_revision="ca-api-x--r2",
                revision_detail=revision_payload(),
                require_replicas=True,
                previous_image=None,
                current_image="acr.azurecr.io/api:azd-deploy-2",
            ),
            [],
        )

    def test_a_revision_that_returns_no_state_is_a_failure_not_a_pass(self) -> None:
        problems = pdv.rollout_problems(
            service="api",
            previous_revision="ca-api-x--r1",
            current_revision="ca-api-x--r2",
            revision_detail=None,
            require_replicas=True,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("returned no state", problems[0])


# ---------------------------------------------------------------------------
# deploying by digest (audit finding P1-7)
# ---------------------------------------------------------------------------

DIGEST_A = "acr.azurecr.io/ai4ia/api-prod@sha256:" + "a" * 64
DIGEST_B = "acr.azurecr.io/ai4ia/api-prod@sha256:" + "b" * 64


class ExpectedImageTests(unittest.TestCase):
    """`--expect-image` replaces 'the image changed' with 'it is OUR image'.

    Deploying by digest breaks the older heuristic's premise: two builds of
    identical content produce the same reference, so 'unchanged' stops meaning
    'the deploy did not land'. Reading that as a failure would roll back a
    healthy release -- the single worst outcome this gate can produce.
    """

    def problems(self, **kwargs: object) -> list[str]:
        base: dict = dict(
            service="api",
            previous_revision="ca-api-x--r1",
            current_revision="ca-api-x--r2",
            revision_detail=revision_payload(),
            require_replicas=True,
        )
        base.update(kwargs)
        return pdv.rollout_problems(**base)

    def test_the_expected_image_running_is_a_pass(self) -> None:
        self.assertEqual(
            self.problems(current_image=DIGEST_A, expected_image=DIGEST_A), []
        )

    def test_an_unchanged_digest_passes_when_it_is_the_one_we_deployed(self) -> None:
        """The false rollback this option exists to prevent."""
        self.assertEqual(
            self.problems(
                previous_image=DIGEST_A,
                current_image=DIGEST_A,
                expected_image=DIGEST_A,
            ),
            [],
        )

    def test_a_different_image_running_is_a_failure(self) -> None:
        problems = self.problems(current_image=DIGEST_B, expected_image=DIGEST_A)
        self.assertEqual(len(problems), 1)
        self.assertIn("not the image this deploy pushed", problems[0])

    def test_no_image_at_all_is_a_failure(self) -> None:
        problems = self.problems(current_image=None, expected_image=DIGEST_A)
        self.assertEqual(len(problems), 1)
        self.assertIn("runs no image", problems[0])

    def test_same_revision_passes_when_it_already_runs_the_exact_digest(self) -> None:
        """Content-addressed promotion is allowed to be a no-op.

        Building identical bytes yields the same digest, and Container Apps may
        keep the current revision when the template is byte-identical. The exact
        expected image plus the health/canary checks are stronger evidence than
        revision churn; rejecting this case rolls back unrelated services from a
        healthy, fully verified release.
        """
        self.assertEqual(
            self.problems(
            current_revision="ca-api-x--r1",
            current_image=DIGEST_A,
            expected_image=DIGEST_A,
            ),
            [],
        )

    def test_same_revision_with_the_wrong_digest_is_still_a_failure(self) -> None:
        problems = self.problems(
            current_revision="ca-api-x--r1",
            current_image=DIGEST_B,
            expected_image=DIGEST_A,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("not the image this deploy pushed", problems[0])

    def test_parsing_accepts_repeated_service_pairs(self) -> None:
        self.assertEqual(
            pdv.parse_expected_images([f"api={DIGEST_A}", f"web={DIGEST_B}"]),
            {"api": DIGEST_A, "web": DIGEST_B},
        )

    def test_parsing_tolerates_a_digest_bearing_reference(self) -> None:
        """`@sha256:` contains no `=`, but a naive split() would still be wrong."""
        parsed = pdv.parse_expected_images([f"proxy={DIGEST_A}"])
        self.assertEqual(parsed["proxy"], DIGEST_A)

    def test_parsing_rejects_a_missing_separator(self) -> None:
        with self.assertRaises(pdv.VerifyInputError):
            pdv.parse_expected_images(["api"])

    def test_parsing_rejects_an_empty_reference(self) -> None:
        with self.assertRaises(pdv.VerifyInputError):
            pdv.parse_expected_images(["api="])

    def test_parsing_rejects_an_unknown_service(self) -> None:
        with self.assertRaises(pdv.VerifyInputError):
            pdv.parse_expected_images([f"gateway={DIGEST_A}"])

    def test_parsing_rejects_two_references_for_one_service(self) -> None:
        with self.assertRaises(pdv.VerifyInputError):
            pdv.parse_expected_images([f"api={DIGEST_A}", f"api={DIGEST_B}"])

    def test_parsing_nothing_yields_nothing(self) -> None:
        """No expectation must leave the previous behaviour exactly in place."""
        self.assertEqual(pdv.parse_expected_images([]), {})


# ---------------------------------------------------------------------------
# rollback target selection
# ---------------------------------------------------------------------------


class RollbackCommandTests(unittest.TestCase):
    def snapshot(self, **overrides: Any) -> Any:
        base = {
            "service": "api",
            "name": "ca-api-slurmfactory",
            "exists": True,
            "revision": "ca-api-slurmfactory--r1",
            "revisionsMode": "Single",
        }
        base.update(overrides)
        return pdv.AppSnapshot(**base)

    def test_single_revision_mode_uses_revision_copy(self) -> None:
        """`ingress traffic set` is REJECTED in Single mode -- it would no-op."""
        commands = pdv.rollback_commands(
            resource_group="rg-ai4ia-slurmfactory",
            snapshot=self.snapshot(),
            current_revision="ca-api-slurmfactory--r2",
        )
        self.assertEqual(len(commands), 1)
        self.assertIn("revision", commands[0])
        self.assertIn("copy", commands[0])
        self.assertIn("--from-revision", commands[0])
        self.assertIn("ca-api-slurmfactory--r1", commands[0])
        self.assertNotIn("--revision-weight", commands[0])

    def test_multiple_revision_mode_shifts_traffic_weights(self) -> None:
        commands = pdv.rollback_commands(
            resource_group="rg-ai4ia-slurmfactory",
            snapshot=self.snapshot(revisionsMode="Multiple"),
            current_revision="ca-api-slurmfactory--r2",
        )
        self.assertEqual(len(commands), 1)
        self.assertIn("--revision-weight", commands[0])
        self.assertIn("ca-api-slurmfactory--r1=100", commands[0])

    def test_the_mode_comparison_is_not_case_sensitive(self) -> None:
        commands = pdv.rollback_commands(
            resource_group="rg",
            snapshot=self.snapshot(revisionsMode="multiple"),
            current_revision="ca-api-slurmfactory--r2",
        )
        self.assertIn("--revision-weight", commands[0])

    def test_an_app_that_never_moved_is_left_alone(self) -> None:
        """Restoring a revision that is already serving would create churn for nothing."""
        self.assertEqual(
            pdv.rollback_commands(
                resource_group="rg",
                snapshot=self.snapshot(),
                current_revision="ca-api-slurmfactory--r1",
            ),
            [],
        )

    def test_a_greenfield_app_has_nothing_to_roll_back_to(self) -> None:
        self.assertEqual(
            pdv.rollback_commands(
                resource_group="rg",
                snapshot=self.snapshot(exists=False, revision=None),
                current_revision="ca-api-slurmfactory--r1",
            ),
            [],
        )
        self.assertEqual(
            pdv.rollback_commands(
                resource_group="rg",
                snapshot=self.snapshot(revision=None),
                current_revision="ca-api-slurmfactory--r1",
            ),
            [],
        )


# ---------------------------------------------------------------------------
# HTTP probing
# ---------------------------------------------------------------------------


class ProbeTests(unittest.TestCase):
    def test_cold_start_shapes_are_retryable_and_real_answers_are_not(self) -> None:
        self.assertTrue(pdv.is_retryable(pdv.HttpOutcome(status=None, error="timeout")))
        for status in (429, 500, 502, 503, 504):
            self.assertTrue(pdv.is_retryable(pdv.HttpOutcome(status=status)), status)
        for status in (200, 401, 403, 404, 422):
            self.assertFalse(pdv.is_retryable(pdv.HttpOutcome(status=status)), status)

    def test_a_scale_to_zero_cold_start_is_retried_not_failed(self) -> None:
        """The proxy can be minReplicas=0; the first request wakes a replica."""
        responses = [
            pdv.HttpOutcome(status=None, error="TimeoutError"),
            pdv.HttpOutcome(status=503),
            pdv.HttpOutcome(status=200),
        ]
        slept: list[float] = []

        def request(method, url, **kwargs):
            return responses.pop(0)

        outcome, attempts = pdv.probe(
            "https://proxy.test/startup",
            attempts=5,
            delay=7.5,
            request=request,
            sleep=slept.append,
        )
        self.assertEqual(outcome.status, 200)
        self.assertEqual(attempts, 3)
        self.assertEqual(slept, [7.5, 7.5])

    def test_the_retry_budget_is_finite(self) -> None:
        def request(method, url, **kwargs):
            return pdv.HttpOutcome(status=503)

        outcome, attempts = pdv.probe(
            "https://proxy.test/startup",
            attempts=4,
            request=request,
            sleep=lambda _: None,
        )
        self.assertEqual(outcome.status, 503)
        self.assertEqual(attempts, 4)

    def test_a_definite_answer_is_not_retried(self) -> None:
        calls: list[str] = []

        def request(method, url, **kwargs):
            calls.append(url)
            return pdv.HttpOutcome(status=404)

        outcome, attempts = pdv.probe(
            "https://api.test/health/ready",
            attempts=9,
            request=request,
            sleep=lambda _: None,
        )
        self.assertEqual(outcome.status, 404)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(calls), 1)

    def test_the_proxy_probe_accepts_an_authenticating_gateways_rejection(self) -> None:
        """401/404 prove a replica answered; every 5xx means the container is faulted."""
        for status in (200, 401, 403, 404):
            self.assertTrue(pdv.ingress_responds(pdv.HttpOutcome(status=status)), status)
        # 500 in particular: /startup is defined to answer 200 or 503, so a 500
        # from it is a fault inside the proxy, not a healthy gateway saying no.
        for status in (500, 502, 503, 504):
            self.assertFalse(pdv.ingress_responds(pdv.HttpOutcome(status=status)), status)
        self.assertFalse(
            pdv.ingress_responds(pdv.HttpOutcome(status=None, error="TimeoutError"))
        )

    def test_the_web_root_may_redirect_but_not_error(self) -> None:
        """Redirects are never followed, so a root that 307s must still count."""
        for status in (200, 204, 301, 307, 308):
            self.assertTrue(pdv.ingress_or_redirect(pdv.HttpOutcome(status=status)), status)
        for status in (401, 404, 500, 503):
            self.assertFalse(pdv.ingress_or_redirect(pdv.HttpOutcome(status=status)), status)

    def test_redirects_are_not_followed(self) -> None:
        """Following one would replay the canary's Authorization header at
        whatever host the response named."""
        handler = pdv._NoRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test/")
        )

    def test_the_shared_deadline_stops_further_retries(self) -> None:
        """Per-check budgets multiply; without this the step timeout kills the run."""
        now = [0.0]
        deadline = pdv.Deadline(seconds=30.0, clock=lambda: now[0])

        def request(method, url, **kwargs):
            now[0] += 20.0
            return pdv.HttpOutcome(status=503)

        outcome, attempts = pdv.probe(
            "https://proxy.test/startup",
            attempts=50,
            delay=1.0,
            request=request,
            sleep=lambda _: None,
            deadline=deadline,
        )
        self.assertEqual(outcome.status, 503)
        self.assertEqual(attempts, 2)

    def test_no_deadline_means_the_attempt_budget_still_applies(self) -> None:
        def request(method, url, **kwargs):
            return pdv.HttpOutcome(status=503)

        _, attempts = pdv.probe(
            "https://proxy.test/startup",
            attempts=3,
            request=request,
            sleep=lambda _: None,
            deadline=None,
        )
        self.assertEqual(attempts, 3)

    def test_base_urls_must_be_credential_free_https(self) -> None:
        self.assertEqual(
            pdv.validate_https_base("https://api.test/", label="API URL"),
            "https://api.test",
        )
        for bad in (
            "http://api.test",
            "https://user:pw@api.test",
            "https://api.test/?api-key=abc",
            "ftp://api.test",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(pdv.VerifyInputError):
                    pdv.validate_https_base(bad, label="API URL")


# ---------------------------------------------------------------------------
# canary model selection
# ---------------------------------------------------------------------------


class CanaryModelSelectionTests(unittest.TestCase):
    CATALOG = {
        "catalog": [
            {"name": "big-chat", "category": "chat", "deployments": [{"region": "eastus2"}]},
            {"name": "a-chat", "category": "chat", "deployments": [{"region": "eastus2"}]},
            {
                "name": "tiny-fast",
                "category": "chat-fast",
                "deployments": [{"region": "eastus2"}],
            },
            {"name": "a-picture", "category": "image", "deployments": [{"region": "westus"}]},
            {"name": "undeployed", "category": "chat-fast", "deployments": []},
        ]
    }

    def test_preferences_are_cheapest_first_and_deterministic(self) -> None:
        """A canary that picks a different model per run tests a different path per run."""
        self.assertEqual(
            pdv.catalog_model_preferences(self.CATALOG),
            ["tiny-fast", "a-chat", "big-chat"],
        )

    def test_capability_models_and_undeployed_entries_are_excluded(self) -> None:
        preferences = pdv.catalog_model_preferences(self.CATALOG)
        self.assertNotIn("a-picture", preferences)
        self.assertNotIn("undeployed", preferences)

    def test_selection_takes_the_first_model_the_live_api_advertises(self) -> None:
        """The API filters by data-residency policy, so the catalog alone is not enough."""
        self.assertEqual(
            pdv.select_canary_model(self.CATALOG, ["a-chat", "big-chat"]), "a-chat"
        )

    def test_no_overlap_is_an_explained_failure_not_a_mystery_400(self) -> None:
        with self.assertRaises(pdv.VerifyInputError) as caught:
            pdv.select_canary_model(self.CATALOG, ["something-else"])
        self.assertIn("data-residency", str(caught.exception))
        with self.assertRaises(pdv.VerifyInputError):
            pdv.select_canary_model(self.CATALOG, [])

    def test_the_real_catalog_still_yields_a_candidate(self) -> None:
        """infra/models.json is the source of truth; a reshape must not silently
        leave the canary with nothing to call."""
        doc = pdv.load_model_catalog(ROOT / "infra" / "models.json")
        preferences = pdv.catalog_model_preferences(doc)
        self.assertGreater(len(preferences), 3)
        names = {
            entry["name"]
            for entry in doc["catalog"]
            if isinstance(entry, dict) and "name" in entry
        }
        self.assertTrue(set(preferences) <= names)
        # A *deployment* name is derived server-side from the catalog; this must
        # never be one (see AGENTS.md, "Catalog-driven models").
        for candidate in preferences:
            self.assertNotIn(doc["naming"]["subscriptionToken"], candidate)


# ---------------------------------------------------------------------------
# the canary itself
# ---------------------------------------------------------------------------

# Built at runtime, never stored as a literal: a committed JWT-shaped string is a
# gitleaks finding (see .gitleaks.toml entry 5, where exactly this fixture had to
# be constructed instead). Deliberately does NOT start with `ey`, and every
# secret-shaped value below is a low-entropy repeated-character placeholder for
# the same reason.
TOKEN = ".".join(("aGVhZGVyMTIz", "cGF5bG9hZDEyMw", "c2lnbmF0dXJlMTIz"))
CATALOG = {
    "catalog": [
        {"name": "tiny-fast", "category": "chat-fast", "deployments": [{"region": "e"}]}
    ]
}


def canary_script(chat: Any) -> dict[str, list[Any]]:
    return {
        "GET /api/models": [ok({"models": [{"id": "tiny-fast"}]})],
        "POST /api/sessions": [ok({"id": "sess-1"}, status=201)],
        "POST /api/chat": [chat],
        "DELETE /api/sessions/sess-1": [pdv.HttpOutcome(status=204)],
    }


class CanaryTests(unittest.TestCase):
    def run_canary(self, http: FakeHttp, **kwargs: Any) -> Any:
        with redirect_stdout(io.StringIO()) as captured:
            result = pdv.run_canary(
                api_base="https://api.test",
                token=TOKEN,
                catalog_doc=CATALOG,
                request=http,
                sleep=lambda _: None,
                monotonic=lambda: 0.0,
                **kwargs,
            )
        self.captured = captured.getvalue()
        return result

    def test_a_healthy_turn_traverses_the_whole_governed_path(self) -> None:
        http = FakeHttp(
            canary_script(ok({"message": {"content": "ready", "status": "complete"}}))
        )
        result = self.run_canary(http)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.model, "tiny-fast")
        self.assertEqual(result.reply_chars, 5)
        methods = [call[0] for call in http.calls]
        self.assertEqual(methods, ["GET", "POST", "POST", "DELETE"])
        self.assertTrue(http.calls[2][1].endswith("/api/chat"))

    def test_the_turn_is_authenticated_and_non_streaming(self) -> None:
        """Streaming would need SSE parsing; the point is the round trip, not the UX."""
        http = FakeHttp(
            canary_script(ok({"message": {"content": "ready", "status": "complete"}}))
        )
        self.run_canary(http)
        for headers in http.headers:
            self.assertEqual(headers.get("Authorization"), f"Bearer {TOKEN}")
        chat_body = json.loads(http.bodies[2])
        self.assertIs(chat_body["stream"], False)
        self.assertEqual(chat_body["model"], "tiny-fast")
        # A tight max_tokens is spent on reasoning tokens by the cheap models this
        # picks, which returns an empty completion and fails a healthy deploy.
        self.assertNotIn("params", chat_body)

    def test_an_empty_reply_is_a_failure(self) -> None:
        """A 200 with no content means the gateway answered but the model did not."""
        http = FakeHttp(
            canary_script(ok({"message": {"content": "   ", "status": "complete"}}))
        )
        result = self.run_canary(http)
        self.assertFalse(result.ok)
        self.assertIn("empty reply", result.detail)

    def test_a_non_complete_assistant_status_is_a_failure(self) -> None:
        http = FakeHttp(
            canary_script(ok({"message": {"content": "partial", "status": "error"}}))
        )
        result = self.run_canary(http)
        self.assertFalse(result.ok)
        self.assertIn("status is error", result.detail)

    def test_a_gateway_failure_is_retried_then_reported(self) -> None:
        """502 is what FastAPI returns for a ModelGatewayError -- the proxy/APIM hop."""
        http = FakeHttp(
            canary_script(
                pdv.HttpOutcome(status=502, body=b'{"detail":"upstream refused"}')
            )
        )
        result = self.run_canary(http, attempts=3)
        self.assertFalse(result.ok)
        self.assertIn("HTTP 502", result.detail)
        self.assertEqual(len([c for c in http.calls if c[1].endswith("/api/chat")]), 3)

    def test_an_unauthenticated_api_fails_immediately(self) -> None:
        http = FakeHttp({"GET /api/models": [pdv.HttpOutcome(status=401)]})
        result = self.run_canary(http)
        self.assertFalse(result.ok)
        self.assertIn("HTTP 401", result.detail)
        self.assertEqual(len(http.calls), 1)

    def test_the_canary_session_is_always_cleaned_up(self) -> None:
        """Even on failure -- otherwise every bad deploy leaves litter in Cosmos."""
        http = FakeHttp(canary_script(pdv.HttpOutcome(status=502)))
        self.run_canary(http, attempts=1)
        self.assertEqual(http.calls[-1], ("DELETE", "https://api.test/api/sessions/sess-1"))

    def test_ambiguous_session_create_is_never_retried(self) -> None:
        script = {
            "GET /api/models": [ok({"models": [{"id": "tiny-fast"}]})],
            "POST /api/sessions": [
                pdv.HttpOutcome(status=None, error="TimeoutError"),
                ok({"id": "orphan-2"}, status=201),
            ],
        }
        http = FakeHttp(script)
        result = self.run_canary(http, attempts=3)
        self.assertFalse(result.ok)
        creates = [call for call in http.calls if call[1].endswith("/api/sessions")]
        self.assertEqual(len(creates), 1)
        self.assertFalse(any(call[1].endswith("/api/chat") for call in http.calls))

    def test_cleanup_failure_fails_the_canary(self) -> None:
        script = canary_script(
            ok({"message": {"content": "ready", "status": "complete"}})
        )
        script["DELETE /api/sessions/sess-1"] = [pdv.HttpOutcome(status=503)]
        result = self.run_canary(FakeHttp(script), attempts=1)
        self.assertFalse(result.ok)
        self.assertIn("cleanup returned HTTP 503", result.detail)

    def test_neither_the_token_nor_the_model_reply_is_ever_printed(self) -> None:
        """The canary holds a bearer token and receives model output. CI logs are retained."""
        secret_reply = "ready; and here is a sentence the log must never keep"
        http = FakeHttp(
            canary_script(ok({"message": {"content": secret_reply, "status": "complete"}}))
        )
        with redirect_stdout(io.StringIO()) as captured:
            outcome = pdv.run_canary(
                api_base="https://api.test",
                token=TOKEN,
                catalog_doc=CATALOG,
                request=http,
                sleep=lambda _: None,
            )
            pdv.emit(
                "canary",
                outcome="passed" if outcome.ok else "failed",
                model=outcome.model,
                replyChars=outcome.reply_chars,
            )
        printed = captured.getvalue()
        self.assertNotIn(TOKEN, printed)
        self.assertNotIn(secret_reply, printed)
        self.assertNotIn("never keep", printed)
        self.assertIn(str(len(secret_reply)), printed)

    def test_an_error_body_is_bounded_and_redacted(self) -> None:
        leak = b'{"detail":"failed calling https://gw.test/openai?api-key=aaaaaaaaaaaaaaaaaa"}'
        http = FakeHttp(canary_script(pdv.HttpOutcome(status=502, body=leak)))
        result = self.run_canary(http, attempts=1)
        self.assertNotIn("aaaaaaaaaaaaaaaaaa", result.detail)
        self.assertIn("[REDACTED]", result.detail)

    def test_canary_only_command_is_available_without_deploy_state(self) -> None:
        args = pdv.build_parser().parse_args(
            ["canary", "--api-url", "https://api.test"]
        )
        self.assertIs(args.func, pdv.cmd_canary)


class RedactionTests(unittest.TestCase):
    def test_credentials_are_scrubbed_from_anything_headed_for_the_log(self) -> None:
        self.assertNotIn("abc", pdv.redact("Authorization: Bearer abcdefgh") or "")
        # Bare JWT, no surrounding keyword -- an access token pasted into an error
        # body looks exactly like this. Assembled rather than written out; see the
        # note on TOKEN above.
        jwt = ".".join(("aGVhZGVyMTIz", "cGF5bG9hZDEyMw", "c2lnbmF0dXJlMTIz"))
        self.assertEqual(pdv.redact(f"presented {jwt}"), "presented [REDACTED]")
        self.assertIn("[REDACTED]", pdv.redact("https://x/y?access_token=abc123") or "")
        # JSON-shaped, which is how the API and the gateway return error bodies.
        # `_http_detail` decodes those straight into the log.
        self.assertIn("[REDACTED]", pdv.redact('{"api_key": "hunter2"}') or "")
        self.assertNotIn("hunter2", pdv.redact('{"api_key": "hunter2"}') or "")

    def test_control_characters_cannot_forge_workflow_commands(self) -> None:
        """An unescaped newline would let a response body inject its own ::error::."""
        self.assertEqual(pdv.redact("a\n::error::forged"), "a ::error::forged")
        self.assertNotIn("\n", pdv.redact("a\r\nb") or "")

    def test_emit_redacts_nested_structures(self) -> None:
        with redirect_stdout(io.StringIO()) as captured:
            pdv.emit("t", problems=["Bearer abcdefghijk"], nested={"k": "api_key=zzz"})
        self.assertNotIn("abcdefghijk", captured.getvalue())
        self.assertNotIn("zzz", captured.getvalue())


# ---------------------------------------------------------------------------
# capture / verify / rollback, end to end with az stubbed
# ---------------------------------------------------------------------------

ENV = "slurmfactory"
APPS = {
    "api": f"ca-api-{ENV}",
    "web": f"ca-web-{ENV}",
    "proxy": f"ca-proxy-{ENV}",
}


def world(
    *,
    api_revision: str = f"ca-api-{ENV}--r2",
    web_revision: str = f"ca-web-{ENV}--r2",
    proxy_revision: str = f"ca-proxy-{ENV}--r2",
    api_detail: dict | None = None,
    custom_domains: list[dict] | None = None,
) -> FakeAz:
    apps = {
        APPS["api"]: container_app(
            name=APPS["api"], revision=api_revision, fqdn="api.test"
        ),
        APPS["web"]: container_app(
            name=APPS["web"],
            revision=web_revision,
            fqdn="web.test",
            custom_domains=custom_domains,
        ),
        APPS["proxy"]: container_app(
            name=APPS["proxy"], revision=proxy_revision, fqdn="proxy.test", min_replicas=0
        ),
    }
    revisions = {
        (APPS["api"], api_revision): api_detail or revision_payload(),
        (APPS["web"], web_revision): revision_payload(),
        (APPS["proxy"], proxy_revision): revision_payload(
            running="ScaledToZero", replicas=0
        ),
    }
    return FakeAz(apps=apps, revisions=revisions)


class CaptureTests(unittest.TestCase):
    def test_capture_records_a_rollback_target_for_every_app(self) -> None:
        import tempfile

        az = world(api_revision=f"ca-api-{ENV}--r1")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / STATE_FILE
            with patch.object(pdv, "run_az", az), redirect_stdout(io.StringIO()):
                code = pdv.main(
                    ["capture", "--state", str(state_path), "--environment", ENV]
                )
            self.assertEqual(code, 0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["resourceGroup"], f"rg-ai4ia-{ENV}")
        self.assertEqual({a["service"] for a in state["apps"]}, set(pdv.SERVICES))
        api = next(a for a in state["apps"] if a["service"] == "api")
        self.assertEqual(api["revision"], f"ca-api-{ENV}--r1")
        self.assertEqual(api["revisionsMode"], "Single")
        self.assertEqual(api["fqdn"], "api.test")

    def test_a_greenfield_app_is_recorded_as_absent_not_omitted(self) -> None:
        """Silently dropping it would make rollback claim success over a missing app."""
        import tempfile

        az = FakeAz(apps={})
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / STATE_FILE
            with patch.object(pdv, "run_az", az), redirect_stdout(io.StringIO()):
                pdv.main(["capture", "--state", str(state_path), "--environment", ENV])
            state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["apps"]), 3)
        self.assertTrue(all(a["exists"] is False for a in state["apps"]))
        self.assertTrue(all(a["revision"] is None for a in state["apps"]))

    def test_a_transient_read_failure_fails_the_capture_rather_than_guessing(self) -> None:
        """The worst outcome available here: a 403 or a blip becomes "absent",
        which silently drops BOTH the unchanged-revision assertion and the
        rollback target, and the gate then reports a clean run over a deploy it
        never checked. Capture runs before the deploy, so failing costs nothing."""
        import tempfile

        calls: list[list[str]] = []

        def flaky(args, **kwargs):
            calls.append(list(args))
            return 1, "", "ERROR: (AuthorizationFailed) does not have permission"

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / STATE_FILE
            with (
                patch.object(pdv, "run_az", flaky),
                patch.object(pdv.time, "sleep", lambda _: None),
                redirect_stdout(io.StringIO()) as captured,
            ):
                code = pdv.main(
                    ["capture", "--state", str(state_path), "--environment", ENV]
                )
            self.assertEqual(code, 2)
            self.assertFalse(state_path.exists())
        self.assertIn("::error::", captured.getvalue())
        # Retried before giving up: a single blip should not fail a deploy.
        self.assertGreater(len(calls), 1)

    def test_a_genuine_not_found_is_absent_and_is_not_retried(self) -> None:
        import tempfile

        az = FakeAz(apps={})
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / STATE_FILE
            with patch.object(pdv, "run_az", az), redirect_stdout(io.StringIO()):
                code = pdv.main(
                    ["capture", "--state", str(state_path), "--environment", ENV]
                )
        self.assertEqual(code, 0)
        # One read per app, no retries: the app is simply not there yet.
        self.assertEqual(len(az.calls), 3)

    def test_not_found_detection(self) -> None:
        self.assertTrue(pdv._is_not_found("ERROR: (ResourceNotFound) app not found"))
        self.assertTrue(pdv._is_not_found("The Resource 'x' was not found"))
        self.assertFalse(pdv._is_not_found("(AuthorizationFailed) no permission"))
        self.assertFalse(pdv._is_not_found("Read timed out"))

    def test_a_hostile_environment_name_is_rejected(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                pdv.main(["capture", "--state", "x.json", "--environment", "a b; rm -rf"]),
                2,
            )

    def test_the_workload_token_changes_the_resource_group(self) -> None:
        import tempfile

        az = world()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / STATE_FILE
            with patch.object(pdv, "run_az", az), redirect_stdout(io.StringIO()):
                pdv.main(
                    [
                        "capture",
                        "--state",
                        str(state_path),
                        "--environment",
                        ENV,
                        "--workload",
                        "nomad",
                    ]
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["resourceGroup"], f"rg-nomad-{ENV}")
        self.assertIn(f"rg-nomad-{ENV}", " ".join(az.calls[0]))


def healthy_http() -> FakeHttp:
    return FakeHttp(
        {
            "GET /health/live": [pdv.HttpOutcome(status=200)],
            "GET /health/ready": [pdv.HttpOutcome(status=200)],
            "GET https://web.test/": [pdv.HttpOutcome(status=200)],
            "GET /startup": [pdv.HttpOutcome(status=200)],
        }
    )


class VerifyTests(unittest.TestCase):
    def verify(
        self,
        *,
        az: FakeAz,
        previous: dict[str, str | None] | None = None,
        http: FakeHttp | None = None,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> tuple[int, str]:
        import tempfile

        previous = previous or {
            "api": f"ca-api-{ENV}--r1",
            "web": f"ca-web-{ENV}--r1",
            "proxy": f"ca-proxy-{ENV}--r1",
        }
        state = {
            "version": pdv.STATE_VERSION,
            "resourceGroup": f"rg-ai4ia-{ENV}",
            "apps": [
                {
                    "service": service,
                    "name": APPS[service],
                    # A greenfield capture records the app as absent with no FQDN;
                    # tie the two together so `previous=None` exercises that path.
                    "exists": previous[service] is not None,
                    "revision": previous[service],
                    "revisionsMode": "Single",
                    "minReplicas": 0 if service == "proxy" else 1,
                    "image": (
                        "acr.azurecr.io/x:1" if previous[service] is not None else None
                    ),
                    "fqdn": (
                        {"api": "api.test", "web": "web.test", "proxy": "proxy.test"}[
                            service
                        ]
                        if previous[service] is not None
                        else None
                    ),
                }
                for service in pdv.SERVICES
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / STATE_FILE
            state_path.write_text(json.dumps(state), encoding="utf-8")
            argv = [
                "verify",
                "--state",
                str(state_path),
                "--attempts",
                "1",
                "--proxy-attempts",
                "1",
                # One rollout read, no backoff: the polling behaviour has its own
                # tests below, and leaving the default here would make every
                # failure case in this class sleep for minutes.
                "--rollout-attempts",
                "1",
                "--rollout-delay",
                "0",
                "--delay",
                "0",
                *(extra_args or ["--skip-canary"]),
            ]
            with (
                patch.object(pdv, "run_az", az),
                patch.object(pdv, "http_request", http or healthy_http()),
                patch.dict("os.environ", env or {}, clear=False),
                redirect_stdout(io.StringIO()) as captured,
            ):
                code = pdv.main(argv)
        return code, captured.getvalue()

    def test_a_healthy_deploy_passes(self) -> None:
        code, out = self.verify(az=world())
        self.assertEqual(code, 0, out)
        self.assertIn('"outcome":"passed"', out)

    def test_a_deploy_that_promoted_nothing_fails(self) -> None:
        """azd exited 0, Container Apps is still serving the old template."""
        code, out = self.verify(
            az=world(api_revision=f"ca-api-{ENV}--r1"),
        )
        self.assertEqual(code, 3)
        self.assertIn("still serving the pre-deploy revision", out)
        self.assertIn("::error::", out)

    def test_a_broken_readiness_probe_fails(self) -> None:
        http = healthy_http()
        http.script["GET /health/ready"] = [pdv.HttpOutcome(status=500)]
        code, out = self.verify(az=world(), http=http)
        self.assertEqual(code, 3)
        self.assertIn("/health/ready is not 200", out)

    def test_a_dead_web_root_fails(self) -> None:
        http = healthy_http()
        http.script["GET https://web.test/"] = [pdv.HttpOutcome(status=503)]
        code, out = self.verify(az=world(), http=http)
        self.assertEqual(code, 3)
        self.assertIn("web: GET / did not render", out)

    def test_a_web_root_that_redirects_still_passes(self) -> None:
        """Redirects are not followed, so a root that 307s must not read as dead."""
        http = healthy_http()
        http.script["GET https://web.test/"] = [pdv.HttpOutcome(status=307)]
        code, out = self.verify(az=world(), http=http)
        self.assertEqual(code, 0, out)

    def test_an_unreachable_proxy_ingress_fails(self) -> None:
        http = healthy_http()
        http.script["GET /startup"] = [pdv.HttpOutcome(status=503)]
        code, out = self.verify(az=world(), http=http)
        self.assertEqual(code, 3)
        self.assertIn("no serving replica", out)

    def test_an_authenticating_proxy_that_rejects_the_probe_still_passes(self) -> None:
        http = healthy_http()
        http.script["GET /startup"] = [pdv.HttpOutcome(status=401)]
        code, out = self.verify(az=world(), http=http)
        self.assertEqual(code, 0, out)

    def test_a_crash_looping_api_fails(self) -> None:
        code, out = self.verify(
            az=world(api_detail=revision_payload(health="Unhealthy", replicas=0))
        )
        self.assertEqual(code, 3)
        self.assertIn("healthState is Unhealthy", out)

    def test_a_deleted_container_app_fails(self) -> None:
        az = world()
        del az.apps[APPS["web"]]
        code, out = self.verify(az=az)
        self.assertEqual(code, 3)
        self.assertIn("web", out)

    def test_a_wiped_custom_domain_fails(self) -> None:
        """The preflight refuses to START such a run; this catches it after the fact."""
        code, out = self.verify(
            az=world(custom_domains=[]),
            env={"AI4IA_WEB_CUSTOM_DOMAIN": "ai4ia.example.test"},
        )
        self.assertEqual(code, 3)
        self.assertIn("no longer bound", out)

    def test_a_custom_domain_that_lost_its_certificate_fails(self) -> None:
        code, out = self.verify(
            az=world(
                custom_domains=[
                    {"name": "ai4ia.example.test", "bindingType": "Disabled"}
                ]
            ),
            env={"AI4IA_WEB_CUSTOM_DOMAIN": "ai4ia.example.test"},
        )
        self.assertEqual(code, 3)
        self.assertIn("expected SniEnabled", out)

    def test_an_intact_custom_domain_passes(self) -> None:
        code, out = self.verify(
            az=world(
                custom_domains=[
                    {"name": "ai4ia.example.test", "bindingType": "SniEnabled"}
                ]
            ),
            env={"AI4IA_WEB_CUSTOM_DOMAIN": "ai4ia.example.test"},
        )
        self.assertEqual(code, 0, out)

    def test_a_missing_canary_token_skips_loudly_rather_than_passing_quietly(self) -> None:
        code, out = self.verify(
            az=world(),
            env={pdv.DEFAULT_TOKEN_ENV: ""},
            extra_args=[],
        )
        self.assertEqual(code, 0, out)
        self.assertIn('"outcome":"skipped"', out)
        self.assertIn("::warning::", out)

    def test_skipping_the_canary_is_announced(self) -> None:
        _, out = self.verify(az=world())
        self.assertIn("::warning::", out)
        self.assertIn("no end-to-end model proof", out)

    def test_a_greenfield_first_deploy_probes_the_live_ingress(self) -> None:
        """The capture recorded `exists: false` and no FQDN, because the app did
        not exist yet. Preferring the captured value would fail the very first
        deploy of a new tenant while the app was serving fine."""
        code, out = self.verify(
            az=world(),
            previous={"api": None, "web": None, "proxy": None},
        )
        self.assertEqual(code, 0, out)

    def test_a_corrupt_state_file_is_a_configuration_error_not_a_rollback(self) -> None:
        """Exit 2, not 3: a gate that cannot read its own input must not roll back."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / STATE_FILE
            bad.write_text("{not json", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pdv.main(["verify", "--state", str(bad)]), 2)
            bad.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pdv.main(["verify", "--state", str(bad)]), 2)


class AwaitRolloutTests(unittest.TestCase):
    """ARM lags `azd deploy`; a single read would roll back healthy releases."""

    def snapshot(self, revision: str | None = f"ca-api-{ENV}--r1") -> Any:
        return pdv.AppSnapshot(
            service="api",
            name=APPS["api"],
            exists=revision is not None,
            revision=revision,
            revisionsMode="Single",
            minReplicas=1,
        )

    def test_a_revision_that_becomes_healthy_on_a_later_read_passes(self) -> None:
        """The load-bearing case: replicas take seconds to start after promotion."""
        states = [
            revision_payload(health="Unhealthy", running="Processing", replicas=0),
            revision_payload(health="Healthy", running="Processing", replicas=0),
            revision_payload(),
        ]
        az = world()

        def run_az(args, **kwargs):
            if "revision show" in " ".join(args) and states:
                payload = states.pop(0)
                return 0, json.dumps(payload), ""
            return az(args, **kwargs)

        slept: list[float] = []
        with patch.object(pdv, "run_az", run_az):
            problems, app, current = pdv.await_rollout(
                resource_group=f"rg-ai4ia-{ENV}",
                service="api",
                snapshot=self.snapshot(),
                attempts=5,
                delay=3.0,
                sleep=slept.append,
            )
        self.assertEqual(problems, [])
        self.assertEqual(current, f"ca-api-{ENV}--r2")
        self.assertIsNotNone(app)
        self.assertEqual(slept, [3.0, 3.0])

    def test_a_permanently_broken_rollout_exhausts_the_budget_and_reports(self) -> None:
        az = world(api_detail=revision_payload(health="Unhealthy", replicas=0))
        slept: list[float] = []
        with patch.object(pdv, "run_az", az):
            problems, _, current = pdv.await_rollout(
                resource_group=f"rg-ai4ia-{ENV}",
                service="api",
                snapshot=self.snapshot(),
                attempts=3,
                delay=1.0,
                sleep=slept.append,
            )
        self.assertTrue(problems)
        self.assertEqual(current, f"ca-api-{ENV}--r2")
        # Two sleeps for three attempts: never sleep after the last one.
        self.assertEqual(slept, [1.0, 1.0])

    def test_a_healthy_app_is_read_once_and_not_polled(self) -> None:
        az = world()
        slept: list[float] = []
        with patch.object(pdv, "run_az", az):
            problems, _, _ = pdv.await_rollout(
                resource_group=f"rg-ai4ia-{ENV}",
                service="api",
                snapshot=self.snapshot(),
                attempts=20,
                delay=10.0,
                sleep=slept.append,
            )
        self.assertEqual(problems, [])
        self.assertEqual(slept, [])

    def test_a_missing_app_is_reported_rather_than_retried_forever(self) -> None:
        az = FakeAz(apps={})
        with patch.object(pdv, "run_az", az):
            problems, app, _ = pdv.await_rollout(
                resource_group=f"rg-ai4ia-{ENV}",
                service="api",
                snapshot=self.snapshot(),
                attempts=2,
                delay=0.0,
                sleep=lambda _: None,
            )
        self.assertTrue(problems)
        self.assertIsNone(app)


class RollbackTests(unittest.TestCase):
    def rollback(
        self, az: FakeAz, *, captured: dict[str, str | None] | None = None
    ) -> tuple[int, str, FakeAz]:
        import tempfile

        captured = captured or {
            "api": f"ca-api-{ENV}--r1",
            "web": f"ca-web-{ENV}--r1",
            "proxy": f"ca-proxy-{ENV}--r1",
        }
        state = {
            "version": pdv.STATE_VERSION,
            "resourceGroup": f"rg-ai4ia-{ENV}",
            "apps": [
                {
                    "service": service,
                    "name": APPS[service],
                    "exists": captured[service] is not None,
                    "revision": captured[service],
                    "revisionsMode": "Single",
                    "minReplicas": 1,
                    "image": RESTORED_IMAGE,
                    "fqdn": None,
                }
                for service in pdv.SERVICES
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / STATE_FILE
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with patch.object(pdv, "run_az", az), redirect_stdout(io.StringIO()) as cap:
                code = pdv.main(
                    [
                        "rollback",
                        "--state",
                        str(state_path),
                        # No backoff: confirm_restored's polling has its own tests.
                        "--confirm-attempts",
                        "2",
                        "--confirm-delay",
                        "0",
                    ]
                )
        return code, cap.getvalue(), az

    def test_every_moved_app_is_restored(self) -> None:
        code, out, az = self.rollback(world())
        self.assertEqual(code, 0, out)
        copies = [c for c in az.calls if "copy" in c]
        self.assertEqual(len(copies), 3)
        for call in copies:
            self.assertIn("--from-revision", call)
            self.assertTrue(any(arg.endswith("--r1") for arg in call))

    def test_an_app_that_did_not_move_is_left_alone(self) -> None:
        """Rolling back an untouched app would restart it for no reason."""
        code, out, az = self.rollback(
            world(web_revision=f"ca-web-{ENV}--r1"),
        )
        self.assertEqual(code, 0, out)
        copies = [c for c in az.calls if "copy" in c]
        self.assertEqual(len(copies), 2)
        self.assertFalse([c for c in copies if APPS["web"] in c])
        self.assertIn('"outcome":"skipped"', out)

    def test_a_greenfield_app_has_nothing_to_restore(self) -> None:
        code, out, az = self.rollback(
            world(), captured={"api": None, "web": None, "proxy": None}
        )
        self.assertEqual(code, 0, out)
        self.assertEqual([c for c in az.calls if "copy" in c], [])

    def test_a_failed_restore_exits_non_zero_and_says_so(self) -> None:
        """Silently 'restoring' nothing is worse than not trying."""
        az = world()
        az.failing_writes = {APPS["api"]}
        code, out, _ = self.rollback(az)
        self.assertEqual(code, 4)
        self.assertIn("::error::", out)
        self.assertIn("still serving the failed deploy", out)

    def test_a_restore_that_did_not_take_is_reported_as_unconfirmed(self) -> None:
        """`revision copy` exiting 0 only means ARM accepted the request. Claiming
        "restored" on that alone is the same unverified-success class this whole
        gate exists to stop believing."""
        az = world()
        # Accept the copy but never actually move the app.
        original_call = az.__call__

        def inert(args, **kwargs):
            if "revision copy" in " ".join(args):
                az.calls.append(list(args))
                return 0, "", ""
            return original_call(args, **kwargs)

        code, out, _ = self.rollback(inert_az(az, inert))
        self.assertEqual(code, 4)
        self.assertIn('"outcome":"unconfirmed"', out)
        self.assertIn("::error::", out)

    def test_a_restore_command_timeout_does_not_abandon_the_other_apps(self) -> None:
        """An AzError escaping the loop would exit 2 and leave web and proxy on
        the failed deploy."""
        az = world()
        original_call = az.__call__

        def timeout_on_api(args, **kwargs):
            argv = list(args)
            if "revision copy" in " ".join(argv) and APPS["api"] in argv:
                az.calls.append(argv)
                raise pdv.AzError("az timed out")
            return original_call(args, **kwargs)

        code, out, _ = self.rollback(inert_az(az, timeout_on_api))
        self.assertEqual(code, 4)
        self.assertIn("az timed out", out)
        # web and proxy were still attempted and restored.
        self.assertEqual(out.count('"outcome":"restored"'), 2)

    def test_an_unreadable_app_is_reported_not_skipped(self) -> None:
        az = world()
        del az.apps[APPS["proxy"]]
        code, out, _ = self.rollback(az)
        self.assertEqual(code, 4)
        self.assertIn("unreadable", out)


class AzInvocationTests(unittest.TestCase):
    def test_the_az_binary_is_overridable_for_testing(self) -> None:
        with patch.dict("os.environ", {"AI4IA_AZ_CLI": "/nonexistent/az"}, clear=False):
            self.assertEqual(pdv._az_binary(), "/nonexistent/az")
            with self.assertRaises(pdv.AzError):
                pdv.run_az(["containerapp", "list"])

    def test_non_json_output_is_an_error_not_a_silent_none(self) -> None:
        with patch.object(pdv, "run_az", lambda args, **kw: (0, "not json", "")):
            with self.assertRaises(pdv.AzError):
                pdv.az_json(["containerapp", "show"])

    def test_az_stderr_is_redacted_before_it_is_raised(self) -> None:
        def failing(args, **kwargs):
            return 1, "", "ERROR: token=bbbbbbbbbbbbbbbbbb rejected"

        with patch.object(pdv, "run_az", failing):
            with self.assertRaises(pdv.AzError) as caught:
                pdv.az_json(["containerapp", "show"])
        self.assertNotIn("bbbbbbbbbbbbbbbbbb", str(caught.exception))


# ---------------------------------------------------------------------------
# workflow wiring
# ---------------------------------------------------------------------------


class DeployWorkflowWiringTests(unittest.TestCase):
    """The script is inert unless deploy.yml calls it in the right order."""

    def setUp(self) -> None:
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.steps = self.workflow["jobs"]["deploy"]["steps"]
        self.names = [step.get("name", "") for step in self.steps]

    def index(self, needle: str) -> int:
        for position, name in enumerate(self.names):
            if needle.lower() in name.lower():
                return position
        raise AssertionError(f"no deploy.yml step matching {needle!r}: {self.names}")

    def step(self, needle: str) -> dict:
        return self.steps[self.index(needle)]

    def test_capture_runs_before_provision_can_change_the_rollback_target(self) -> None:
        self.assertLess(
            self.index("Capture pre-provision revisions"),
            self.index("Provision infrastructure"),
        )

    def test_verification_runs_after_the_deploy(self) -> None:
        self.assertLess(self.index("Deploy application"), self.index("Verify the deploy"))

    def test_the_canary_token_is_preflighted_before_the_deploy(self) -> None:
        """A grant problem must not be discovered after deployment, where it would
        roll back a perfectly healthy release for a reason unrelated to it."""
        self.assertLess(
            self.index("Preflight the post-deploy canary token"),
            self.index("Deploy application"),
        )

    def test_the_preflight_discards_its_token_and_verify_acquires_a_fresh_one(self) -> None:
        """A three-image deploy plus a retried rollout can outlive an access token,
        and an expired one presents as a 401 -- i.e. rolls back a healthy release."""
        preflight = self.step("Preflight the post-deploy canary token").get("run", "")
        fresh = self.step("Acquire the canary token").get("run", "")
        self.assertNotIn("GITHUB_ENV", preflight)
        self.assertIn("get-access-token", preflight)
        self.assertIn("get-access-token", fresh)
        self.assertIn("::add-mask::", fresh)
        self.assertLess(
            self.index("Deploy application"), self.index("Acquire the canary token")
        )

    def test_a_token_problem_does_not_roll_back_a_healthy_release(self) -> None:
        """The token step is separate precisely so the rollback gate can exclude
        it: an Entra blip is not evidence the deploy is bad."""
        condition = str(self.step("Roll back").get("if", ""))
        self.assertIn("steps.canary_token.outcome", condition)
        self.assertIn("!=", condition)

    def test_a_missing_audience_fails_rather_than_silently_skipping(self) -> None:
        """Only the explicit opt-out may skip the canary. A deploy that quietly
        drops its own end-to-end proof is the failure mode this gate removes."""
        preflight = self.step("Preflight the post-deploy canary token").get("run", "")
        self.assertIn("AI4IA_ENTRA_AUDIENCE", preflight)
        audience_branch = preflight.split("AI4IA_ENTRA_AUDIENCE")[1]
        self.assertIn("::error::", audience_branch.split("fi")[0])

    def test_rollback_is_gated_on_failure_and_on_having_a_capture(self) -> None:
        condition = str(self.step("Roll back").get("if", ""))
        self.assertIn("failure()", condition)
        self.assertIn("steps.capture.outcome", condition)

    def test_rollback_covers_provision_deploy_and_verification_failures(self) -> None:
        """A rollback cannot be gated only on the final verification outcome."""
        condition = str(self.step("Roll back").get("if", ""))
        self.assertIn("steps.provision.outcome", condition)
        self.assertIn("steps.deploy.outcome == 'failure'", condition)
        self.assertIn("steps.verify.outcome == 'failure'", condition)

    def test_the_az_login_is_not_gated_on_provisioning(self) -> None:
        """Verification and rollback need `az` on a provision-skipping manual run,
        which is exactly the run used to redeploy images against unchanged infra."""
        self.assertNotIn("if", self.step("Log in to Azure CLI"))

    def test_every_new_step_invokes_the_real_script(self) -> None:
        for needle, mode in (
            ("Capture pre-provision revisions", "capture"),
            ("Verify the deploy", "verify"),
            ("Roll back", "rollback"),
        ):
            with self.subTest(step=needle):
                run = self.step(needle).get("run", "")
                self.assertIn("scripts/post-deploy-verify.py", run)
                self.assertIn(mode, run)

    def test_the_canary_opt_out_is_forwarded_and_carries_no_shadowing_fallback(self) -> None:
        env = self.workflow["jobs"]["deploy"]["env"]
        self.assertIn("AI4IA_DEPLOY_VERIFY_CANARY", env)
        self.assertNotIn("||", env["AI4IA_DEPLOY_VERIFY_CANARY"])

    def test_the_opt_out_reaches_both_steps_that_must_honour_it(self) -> None:
        """Only skipping it in `verify` would still fail the preflight, and only
        skipping it in the preflight would leave `verify` warning about a token
        nobody meant to supply."""
        for needle in (
            "Preflight the post-deploy canary token",
            "Acquire the canary token",
            "Verify the deploy",
        ):
            with self.subTest(step=needle):
                self.assertIn(
                    "AI4IA_DEPLOY_VERIFY_CANARY", self.step(needle).get("run", "")
                )

    def test_the_verification_steps_are_time_bounded(self) -> None:
        """A hung probe must not consume the job budget a cold provision needs,
        and rollback must still get a chance to run after such a timeout."""
        for needle in ("Verify the deploy", "Roll back"):
            with self.subTest(step=needle):
                self.assertIsInstance(self.step(needle).get("timeout-minutes"), int)

    def test_the_runbook_documents_the_gate(self) -> None:
        runbook = (ROOT / "docs/runbooks/deployment.md").read_text(encoding="utf-8")
        self.assertIn("post-deploy-verify.py", runbook)
        self.assertIn("AI4IA_DEPLOY_VERIFY_CANARY", runbook)


if __name__ == "__main__":
    unittest.main()
