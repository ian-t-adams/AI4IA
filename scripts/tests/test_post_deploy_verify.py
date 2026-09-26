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

import io
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import sys
import unittest
import uuid
import warnings
from copy import deepcopy
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

import yaml

from scripts.tests._loader import load_script
from scripts.tests._platform import find_bash

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "post-deploy-verify.py"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


with patch("sys.path", [str(SCRIPT.parent), *sys.path]):
    pdv = load_script("post_deploy_verify", SCRIPT, register=True)

STATE_FILE = "state.json"
# The image a captured revision was running, and therefore the image the app must
# be back on before a rollback may claim it restored anything.
RESTORED_IMAGE = "acr.azurecr.io/x:1"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"
GROUP = "rg-ai4ia-slurmfactory"


def app_id(name: str, resource_group: str = GROUP) -> str:
    return (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.App/containerApps/{name}"
    )


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
    resource_group: str = GROUP,
) -> dict:
    """An `az containerapp show` payload, shaped the way ARM actually returns it."""

    return {
        "name": name,
        "id": app_id(name, resource_group),
        "type": "Microsoft.App/containerApps",
        "properties": {
            "provisioningState": "Succeeded",
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
                "revisionSuffix": revision.partition("--")[2],
                "scale": {
                    "minReplicas": min_replicas, "maxReplicas": 3,
                    "cooldownPeriod": None, "pollingInterval": None, "rules": None,
                },
                "containers": [{"name": "api", "image": image}],
            },
        },
    }


def revision_payload(
    *,
    name: str = "ca-api-slurmfactory--r2",
    active: bool = True,
    health: str | None = "Healthy",
    running: str | None = "Running",
    provisioned: str | None = "Provisioned",
    replicas: int = 1,
    image: str = "acr.azurecr.io/api:azd-deploy-2",
    min_replicas: int = 1,
    template: dict | None = None,
    resource_group: str = GROUP,
) -> dict:
    return {
        "name": name,
        "id": f"{app_id(name.partition('--')[0], resource_group)}/revisions/{name}",
        "type": "Microsoft.App/containerapps/revisions",
        "properties": {
            "active": active,
            "healthState": health,
            "runningState": running,
            "provisioningState": provisioned,
            "replicas": replicas,
            "template": deepcopy(template) if template is not None else container_app(
                revision=name, image=image, min_replicas=min_replicas
            )["properties"]["template"],
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
        resource_group: str = GROUP,
    ) -> None:
        self.apps = apps or {}
        self.revisions = revisions or {}
        self.failing_writes = failing_writes or set()
        self.resource_group = resource_group
        self.calls: list[list[str]] = []

    def __call__(self, args, *, timeout: float = 180.0) -> tuple[int, str, str]:
        argv = list(args)
        self.calls.append(argv)
        name = self._flag(argv, "-n")
        group = self._flag(argv, "-g")
        if group is None or name is None:
            raise AssertionError(f"unscoped az invocation: {argv}")
        app = self.apps.get(name)
        if group != self.resource_group:
            return 1, "", "ERROR: (ResourceNotFound) app not found in requested scope"
        if "--subscription" in argv and self._flag(argv, "--subscription") != SUBSCRIPTION:
            return 1, "", "ERROR: wrong subscription"

        if argv[:3] == ["containerapp", "revision", "show"]:
            revision = self._flag(argv, "--revision") or ""
            payload = self.revisions.get((name or "", revision))
            if payload is None:
                return 1, "", "ERROR: revision not found"
            return 0, json.dumps(payload), ""
        if argv[:2] == ["containerapp", "show"]:
            payload = self.apps.get(name or "")
            if payload is None:
                return 1, "", "ERROR: (ResourceNotFound) app not found"
            return 0, json.dumps(payload), ""
        if argv[:3] == ["containerapp", "revision", "copy"]:
            if name in self.failing_writes:
                return 1, "", "ERROR: (RevisionOperationFailed) could not restore"
            source = self._flag(argv, "--from-revision")
            detail = self.revisions.get((name, source or ""))
            if app is None or detail is None:
                return 1, "", "ERROR: source revision not found"
            if app["properties"]["configuration"]["activeRevisionsMode"].casefold() != "single":
                raise AssertionError("this rollback fixture only copies in Single mode")
            template = deepcopy(detail["properties"]["template"])
            number = 1 + sum(
                app_name == name and revision.startswith(f"{source}-restored")
                for app_name, revision in self.revisions
            )
            restored = f"{source}-restored" + (f"-{number}" if number > 1 else "")
            template["revisionSuffix"] = restored.partition("--")[2]
            minimum = template["scale"]["minReplicas"]
            for (app_name, _), old in self.revisions.items():
                if app_name == name:
                    old["properties"]["active"] = False
                    for field in ("healthState", "provisioningState", "runningState"):
                        old["properties"].pop(field, None)
            self.revisions[(name, restored)] = revision_payload(
                name=restored, template=template, replicas=minimum,
                running="Running" if minimum else "ScaledToZero",
                resource_group=group,
            )
            app["properties"].update(
                latestReadyRevisionName=restored, latestRevisionName=restored,
                template=deepcopy(template), provisioningState="Succeeded",
            )
            return 0, "", ""
        if argv[:4] == ["containerapp", "ingress", "traffic", "set"]:
            if name in self.failing_writes:
                return 1, "", "ERROR: (RevisionOperationFailed) could not restore"
            if app is None:
                return 1, "", "ERROR: app not found"
            config = app["properties"]["configuration"]
            if config["activeRevisionsMode"].casefold() != "multiple":
                raise AssertionError("traffic set is not supported in Single mode")
            weight = self._flag(argv, "--revision-weight")
            if weight is None:
                raise AssertionError("missing exact traffic restore target")
            target, _, percent = weight.partition("=")
            if (name, target) not in self.revisions:
                return 1, "", "ERROR: revision not found"
            config["ingress"]["traffic"] = [{"revisionName": target, "weight": int(percent)}]
            return 0, "", ""
        raise AssertionError(f"unexpected az invocation: {argv}")

    def promote_pending(self, name: str, revision: str) -> bool:
        """Finish readiness only while the pending candidate still owns Single-mode promotion."""
        app = self.apps[name]
        candidate = self.revisions[(name, revision)]
        if (
            candidate["properties"]["active"] is not True
            or app["properties"]["latestRevisionName"] != revision
        ):
            return False
        candidate["properties"].update(
            provisioningState="Provisioned", healthState="Healthy", runningState="Running", replicas=1
        )
        app["properties"]["latestReadyRevisionName"] = revision
        app["properties"]["provisioningState"] = "Succeeded"
        return True

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
        self.options: list[dict[str, Any]] = []

    def __call__(
        self, method, url, *, headers=None, body=None, timeout=30.0,
        deadline=None, body_limit=None,
    ):
        self.calls.append((method, url))
        self.headers.append(dict(headers or {}))
        self.bodies.append(body)
        self.options.append({"timeout": timeout, "deadline": deadline, "body_limit": body_limit})
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
            "image": RESTORED_IMAGE,
            "minReplicas": 1,
        }
        base.update(overrides)
        return pdv.AppSnapshot(**base)

    def observation(self, revision: str = "r2", mode: str = "Single") -> Any:
        az = world(api_revision=f"ca-api-{ENV}--{revision}")
        config = az.apps[APPS["api"]]["properties"]["configuration"]
        config["activeRevisionsMode"] = mode
        if mode.casefold() == "multiple":
            config["ingress"]["traffic"] = [
                {"revisionName": f"ca-api-{ENV}--{revision}", "weight": 100}
            ]
        with patch.object(pdv, "run_az", az):
            return pdv.read_current_observation(
                GROUP, APPS["api"], reference_revision=f"ca-api-{ENV}--r1"
            )

    def test_single_revision_mode_uses_revision_copy(self) -> None:
        """`ingress traffic set` is REJECTED in Single mode -- it would no-op."""
        commands = pdv.rollback_commands(
            resource_group="rg-ai4ia-slurmfactory",
            snapshot=self.snapshot(),
            observation=self.observation(),
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
            observation=self.observation(mode="Multiple"),
        )
        self.assertEqual(len(commands), 1)
        self.assertIn("--revision-weight", commands[0])
        self.assertIn("ca-api-slurmfactory--r1=100", commands[0])

    def test_the_mode_comparison_is_not_case_sensitive(self) -> None:
        commands = pdv.rollback_commands(
            resource_group="rg",
            snapshot=self.snapshot(revisionsMode="multiple"),
            observation=self.observation(mode="multiple"),
        )
        self.assertIn("--revision-weight", commands[0])

    def test_an_app_that_never_moved_is_left_alone(self) -> None:
        """Restoring a revision that is already serving would create churn for nothing."""
        self.assertEqual(
            pdv.rollback_commands(
                resource_group="rg",
                snapshot=self.snapshot(),
                observation=self.observation("r1"),
            ),
            [],
        )

    def test_a_greenfield_app_has_nothing_to_roll_back_to(self) -> None:
        self.assertEqual(
            pdv.rollback_commands(
                resource_group="rg",
                snapshot=self.snapshot(exists=False, revision=None),
                observation=self.observation("r1"),
            ),
            [],
        )
        self.assertEqual(
            pdv.rollback_commands(
                resource_group="rg",
                snapshot=self.snapshot(revision=None),
                observation=self.observation("r1"),
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
CLEANUP_FIXTURE = json.loads(
    (ROOT / "scripts" / "fixtures" / "conversation-cleanup.json").read_text(encoding="utf-8")
)
SID = CLEANUP_FIXTURE["session"]["id"]
SESSION_PATH = f"/api/sessions/{SID}"


def canary_script(chat: Any, *, v1: bool = False) -> dict[str, list[Any]]:
    script = {
        "GET /api/models": [ok({"models": [{"id": "tiny-fast"}]})],
        "POST /api/sessions": [ok(CLEANUP_FIXTURE["session"], status=201)],
        "POST /api/chat": [chat],
        f"DELETE {SESSION_PATH}": [pdv.HttpOutcome(status=204)],
    }
    if v1:
        script[f"DELETE {SESSION_PATH}"] = [ok(CLEANUP_FIXTURE["pending"], status=202)]
        script[f"POST {SESSION_PATH}/deletion/reconcile"] = [
            ok(CLEANUP_FIXTURE["progress"], 202), ok(CLEANUP_FIXTURE["verified"]),
        ]
        script[f"GET {SESSION_PATH}/deletion"] = [ok(CLEANUP_FIXTURE["verified"])]
    return script


def cleanup_calls(http: FakeHttp) -> list[tuple[str, str]]:
    return [
        (method, urlsplit(url).path) for method, url in http.calls
        if urlsplit(url).path.startswith(SESSION_PATH)
    ]


def two_pass_cleanup_script(*, complete: bool) -> dict[str, list[Any]]:
    script = canary_script(ok({"message": {"content": "ready", "status": "complete"}}), v1=True)
    pending = deepcopy(CLEANUP_FIXTURE["progress"])
    final = {**CLEANUP_FIXTURE["verified" if complete else "progress"], "attempts": 2}
    script[f"POST {SESSION_PATH}/deletion/reconcile"] = [
        ok(pending, status=202), ok(final, status=200 if complete else 202),
    ]
    script[f"GET {SESSION_PATH}/deletion"] = [ok(final)]
    return script


class CanaryTests(unittest.TestCase):
    def run_canary(self, http: FakeHttp, **kwargs: Any) -> Any:
        kwargs.setdefault("monotonic", lambda: 0.0)
        kwargs.setdefault("sleep", lambda _: None)
        with redirect_stdout(io.StringIO()) as captured:
            result = pdv.run_canary(
                api_base="https://api.test",
                token=TOKEN,
                catalog_doc=CATALOG,
                request=http,
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
        self.assertEqual(http.calls[-1], ("DELETE", f"https://api.test{SESSION_PATH}"))

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
        script[f"DELETE {SESSION_PATH}"] = [pdv.HttpOutcome(status=503)]
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

    def test_legacy_and_v1_use_only_the_created_owner_session(self) -> None:
        for v1 in (False, True):
            with self.subTest(v1=v1):
                http = FakeHttp(canary_script(
                    ok({"message": {"content": "ready", "status": "complete"}}), v1=v1,
                ))
                result = self.run_canary(http)
                self.assertTrue(result.ok, result.detail)
                expected = [("DELETE", SESSION_PATH)]
                if v1:
                    expected += [
                        ("POST", f"{SESSION_PATH}/deletion/reconcile"),
                        ("POST", f"{SESSION_PATH}/deletion/reconcile"),
                        ("GET", f"{SESSION_PATH}/deletion"),
                    ]
                self.assertEqual(cleanup_calls(http), expected)
                self.assertEqual(http.calls[:3], [
                    ("GET", "https://api.test/api/models"),
                    ("POST", "https://api.test/api/sessions"),
                    ("POST", "https://api.test/api/chat"),
                ])
                for index in range(3, len(http.calls)):
                    self.assertEqual(http.headers[index], http.headers[0])
                    self.assertIsNone(http.bodies[index])
                    self.assertEqual(http.options[index]["body_limit"], pdv.MAX_CLEANUP_BODY_BYTES)
                    self.assertLessEqual(http.options[index]["timeout"], 22)

    def test_accepted_cleanup_is_not_success_and_reconciles_are_finite(self) -> None:
        for complete in (False, True):
            with self.subTest(complete=complete):
                http = FakeHttp(two_pass_cleanup_script(complete=complete))
                result = self.run_canary(http, attempts=9)
                self.assertEqual(result.ok, complete, result.detail)
                expected = [
                    ("DELETE", SESSION_PATH),
                    ("POST", f"{SESSION_PATH}/deletion/reconcile"),
                    ("POST", f"{SESSION_PATH}/deletion/reconcile"),
                ]
                if complete:
                    expected.append(("GET", f"{SESSION_PATH}/deletion"))
                else:
                    self.assertIn("pending after reconcile budget exhausted", result.detail)
                self.assertEqual(cleanup_calls(http), expected)
                self.assertEqual(sum(url.endswith("/api/chat") for _, url in http.calls), 1)
                self.assertEqual(sum(url.endswith("/api/sessions") for _, url in http.calls), 1)

    def test_request_budget_includes_the_verified_readback(self) -> None:
        for maximum in (3, 4):
            with self.subTest(maximum=maximum), patch.object(pdv, "MAX_CLEANUP_REQUESTS", maximum):
                http = FakeHttp(two_pass_cleanup_script(complete=True))
                result = self.run_canary(http)
                self.assertEqual(result.ok, maximum == 4, result.detail)
                self.assertEqual(len(cleanup_calls(http)), maximum)
                if maximum == 3:
                    self.assertIn("request budget exhausted", result.detail)

    def test_already_verified_delete_still_requires_identical_status_readback(self) -> None:
        for matched in (False, True):
            script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
            script[f"DELETE {SESSION_PATH}"] = [ok(CLEANUP_FIXTURE["verified"])]
            if not matched:
                changed = {**CLEANUP_FIXTURE["verified"], "attempts": 3}
                script[f"GET {SESSION_PATH}/deletion"] = [ok(changed)]
            http = FakeHttp(script)
            result = self.run_canary(http)
            self.assertEqual(result.ok, matched, result.detail)
            self.assertEqual(cleanup_calls(http), [
                ("DELETE", SESSION_PATH), ("GET", f"{SESSION_PATH}/deletion"),
            ])

    def test_cleanup_http_and_transport_failures_never_retry_or_downgrade(self) -> None:
        steps = [
            ("DELETE", SESSION_PATH), ("POST", f"{SESSION_PATH}/deletion/reconcile"),
            ("GET", f"{SESSION_PATH}/deletion"),
        ]
        for method, path in steps:
            for code in (None, 301, 302, 307, 401, 403, 404, 409, 429, 500, 503):
                with self.subTest(method=method, code=code):
                    script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
                    script[f"{method} {path}"] = [pdv.HttpOutcome(
                        status=code, error="private transport text" if code is None else None,
                        body=json.dumps({"detail": f"private cleanup content {TOKEN} {SID}"}).encode(),
                    )]
                    http = FakeHttp(script)
                    result = self.run_canary(http, attempts=5)
                    self.assertFalse(result.ok)
                    expected = [steps[0]]
                    if method != "DELETE":
                        expected.append(steps[1])
                    if method == "GET":
                        expected += [steps[1], steps[2]]
                    self.assertEqual(cleanup_calls(http), expected)
                    for private in ("private", TOKEN, SID):
                        self.assertNotIn(private, result.detail)
                    if code is not None:
                        self.assertIn(f"HTTP {code}", result.detail)
        for method, path in steps[1:]:
            script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
            script[f"{method} {path}"] = [pdv.HttpOutcome(status=204)]
            self.assertFalse(self.run_canary(FakeHttp(script)).ok)

    def test_legacy_accepts_only_empty_204_not_missing_or_accepted(self) -> None:
        for code, body, success in [
            (204, b"", True), (204, b"not empty", False), (404, b"", False),
            (200, b"", False), (202, b"{}", False), (204.0, b"", False),
        ]:
            script = canary_script(ok({"message": {"content": "ready"}}))
            script[f"DELETE {SESSION_PATH}"] = [pdv.HttpOutcome(status=code, body=body)]
            http = FakeHttp(script)
            result = self.run_canary(http)
            self.assertEqual(result.ok, success, result.detail)
            self.assertEqual(cleanup_calls(http), [("DELETE", SESSION_PATH)])

    def test_v1_identity_and_undeclared_protocol_fields_cannot_authorize_cleanup(self) -> None:
        steps = [
            ("DELETE", SESSION_PATH, "pending", 202),
            ("POST", f"{SESSION_PATH}/deletion/reconcile", "verified", 200),
            ("GET", f"{SESSION_PATH}/deletion", "verified", 200),
        ]
        for method, path, fixture, code in steps:
            for changes in (
                {"sessionId": "d" * 32}, {"sessionId": None},
                {"deletionProtocol": 0}, {"deletionProtocol": 1},
                {"deletionEpoch": "another-generation"},
                {"reconcileUrl": "https://untrusted.invalid/cleanup"},
                {"scope": "everything"}, {"coordinationRetained": False},
                {"backupsErased": True}, {"autonomousCleanup": True},
            ):
                with self.subTest(method=method, changes=changes):
                    script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
                    script[f"{method} {path}"] = [ok({**CLEANUP_FIXTURE[fixture], **changes}, code)]
                    http = FakeHttp(script)
                    self.assertFalse(self.run_canary(http).ok)
                    self.assertTrue(all(
                        url.startswith(f"https://api.test{SESSION_PATH}")
                        for _, url in http.calls[3:]
                    ))
                    self.assertEqual(http.calls[-1], (method, f"https://api.test{path}"))

    def test_every_verification_field_is_required_at_reconcile_and_readback(self) -> None:
        for method, suffix in (("POST", "/deletion/reconcile"), ("GET", "/deletion")):
            for missing in CLEANUP_FIXTURE["verified"]:
                with self.subTest(method=method, missing=missing):
                    incomplete = deepcopy(CLEANUP_FIXTURE["verified"])
                    del incomplete[missing]
                    script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
                    script[f"{method} {SESSION_PATH}{suffix}"] = [ok(incomplete)]
                    self.assertFalse(self.run_canary(FakeHttp(script)).ok)
        for missing in CLEANUP_FIXTURE["pending"]:
            incomplete = deepcopy(CLEANUP_FIXTURE["pending"])
            del incomplete[missing]
            script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
            script[f"DELETE {SESSION_PATH}"] = [ok(incomplete, 202)]
            http = FakeHttp(script)
            self.assertFalse(self.run_canary(http).ok)
            self.assertEqual(cleanup_calls(http), [("DELETE", SESSION_PATH)])

    def test_verified_label_requires_consistent_typed_evidence(self) -> None:
        changes = [
            {"state": "pending"}, {"phase": "messages"}, {"lastVerifiedAt": None},
            {"messagesVerified": False}, {"documentsVerified": False},
            {"attachmentsVerified": False}, {"messagesVerified": 1},
            {"pendingUploadsTruncated": 0}, {"pendingUploads": {}},
            {"pendingUploadsTruncated": True}, {"retryReason": "uploads_unresolved"},
            {"attempts": 0}, {"attempts": True}, {"attempts": 1.0}, {"attempts": 1001},
        ]
        for field in ("requestedAt", "updatedAt", "lastVerifiedAt"):
            changes.extend({field: invalid} for invalid in (
                None, "", "not a date", "2026-09-10T12:00:00", "2999-01-01T00:00:00Z",
            ))
        changes += [
            {"lastVerifiedAt": "2026-09-10T11:59:59Z"},
            {"updatedAt": "2026-09-10T11:59:59Z"},
            {"lastVerifiedAt": "2026-09-10T12:00:02Z"},
        ]
        for change in changes:
            with self.subTest(change=change):
                script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
                script[f"POST {SESSION_PATH}/deletion/reconcile"] = [
                    ok({**CLEANUP_FIXTURE["verified"], **change}),
                ]
                script[f"GET {SESSION_PATH}/deletion"] = [
                    ok({**CLEANUP_FIXTURE["verified"], **change}),
                ]
                http = FakeHttp(script)
                self.assertFalse(self.run_canary(http).ok)
                self.assertEqual(len(cleanup_calls(http)), 2)

    def test_cleanup_json_is_strict_and_bounded_before_any_further_request(self) -> None:
        pending = json.dumps(CLEANUP_FIXTURE["pending"]).encode()
        malformed = [
            b"", b"[]", b"null", b"{}", b"\xff", b"{", b'{"attempts":NaN}',
            b'{"attempts":1e999}', b'{"sessionId":"wrong",' + pending[1:],
            b"[" * 20 + b"0" + b"]" * 20,
            pending + b" " * (pdv.MAX_CLEANUP_BODY_BYTES + 1),
        ]
        for body in malformed:
            with self.subTest(body=body[:30]):
                script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
                script[f"DELETE {SESSION_PATH}"] = [pdv.HttpOutcome(status=202, body=body)]
                http = FakeHttp(script)
                self.assertFalse(self.run_canary(http).ok)
                self.assertEqual(cleanup_calls(http), [("DELETE", SESSION_PATH)])
        # Exact body bound is allowed; overflow is not silently truncated.
        for extra in (0, 1):
            script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
            body = pending + b" " * (pdv.MAX_CLEANUP_BODY_BYTES - len(pending) + extra)
            script[f"DELETE {SESSION_PATH}"] = [pdv.HttpOutcome(status=202, body=body)]
            self.assertEqual(self.run_canary(FakeHttp(script)).ok, extra == 0)

    def test_changed_intent_or_regressing_attempt_cannot_be_read_as_verified(self) -> None:
        for changed in (False, True):
            script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
            proof = deepcopy(CLEANUP_FIXTURE["verified"])
            if changed:
                proof["requestedAt"] = "2026-09-10T11:59:59Z"
            script[f"POST {SESSION_PATH}/deletion/reconcile"] = [ok(proof)]
            script[f"GET {SESSION_PATH}/deletion"] = [ok(proof)]
            self.assertEqual(self.run_canary(FakeHttp(script)).ok, not changed)
        script = two_pass_cleanup_script(complete=True)
        script[f"POST {SESSION_PATH}/deletion/reconcile"][0] = ok(
            {**CLEANUP_FIXTURE["pending"], "attempts": 3}, 202,
        )
        result = self.run_canary(FakeHttp(script))
        self.assertFalse(result.ok)
        self.assertIn("progress regressed", result.detail)

    def test_unresolved_uploads_and_integrity_unknowns_are_not_cleared_by_an_empty_scan(self) -> None:
        for changes in (
            {"pendingUploads": [{
                "id": "synthetic-upload", "documentId": "synthetic-document",
                "startedAt": "2026-09-10T12:00:00Z",
            }]},
            {"pendingUploadsTruncated": True}, {"retryReason": "uploads_unresolved"},
            {"state": "retryable", "retryReason": "integrity_mismatch"},
            {"state": "retryable", "retryReason": "artifact_store_required"},
        ):
            for method, suffix in (("DELETE", ""), ("POST", "/deletion/reconcile")):
                with self.subTest(changes=changes, method=method):
                    script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
                    script[f"{method} {SESSION_PATH}{suffix}"] = [
                        ok({**CLEANUP_FIXTURE["pending"], **changes}, 202),
                        ok(CLEANUP_FIXTURE["verified"]),
                    ]
                    http = FakeHttp(script)
                    self.assertFalse(self.run_canary(http).ok)
                    self.assertEqual(http.calls[-1], (method, f"https://api.test{SESSION_PATH}{suffix}"))

    def test_retryable_status_can_resume_without_replaying_chat_or_delete(self) -> None:
        script = two_pass_cleanup_script(complete=True)
        script[f"POST {SESSION_PATH}/deletion/reconcile"][0] = ok({
            **CLEANUP_FIXTURE["pending"], "state": "retryable",
            "retryReason": "storage_unavailable", "attempts": 1,
        }, 202)
        http = FakeHttp(script)
        self.assertTrue(self.run_canary(http).ok)
        self.assertEqual(len(cleanup_calls(http)), 4)
        self.assertEqual(sum(method == "DELETE" for method, _ in http.calls), 1)
        self.assertEqual(sum(url.endswith("/api/chat") for _, url in http.calls), 1)

    def test_invalid_or_ambiguous_created_id_never_drives_chat_or_cleanup(self) -> None:
        for identifier in (
            None, "", [], True, "../other", f"{SID}/deletion", f"{SID}?other=1",
            f"{SID}#ignored", f"{SID}\n", "https://untrusted.invalid", SID.upper(), "x" * 1024,
        ):
            script = canary_script(ok({"message": {"content": "ready"}}), v1=True)
            script["POST /api/sessions"] = [ok({**CLEANUP_FIXTURE["session"], "id": identifier}, 201)]
            http = FakeHttp(script)
            result = self.run_canary(http)
            self.assertFalse(result.ok)
            self.assertIn("cleanup unknown", result.detail)
            self.assertEqual(len(http.calls), 2)
        for body in (
            b"not json", b'{"id":"wrong","id":"' + SID.encode() + b'"}',
            b'{"id":"' + SID.encode() + b'",',
        ):
            script = canary_script(ok({"message": {"content": "ready"}}))
            script["POST /api/sessions"] = [pdv.HttpOutcome(status=201, body=body)]
            http = FakeHttp(script)
            self.assertFalse(self.run_canary(http).ok)
            self.assertEqual(len(http.calls), 2)

    def test_cleanup_preserves_failures_before_and_after_model_replies(self) -> None:
        for chat, detail in (
            (pdv.HttpOutcome(status=502), "HTTP 502"),
            (pdv.HttpOutcome(status=None, error="TimeoutError"), "did not complete"),
            (ok({"message": {"content": "", "status": "complete"}}), "empty reply"),
            (ok({"message": {"content": "partial", "status": "error"}}), "status is error"),
        ):
            for failed_cleanup in (False, True):
                script = canary_script(chat, v1=True)
                if failed_cleanup:
                    script[f"GET {SESSION_PATH}/deletion"] = [pdv.HttpOutcome(status=404)]
                http = FakeHttp(script)
                result = self.run_canary(http, attempts=1)
                self.assertFalse(result.ok)
                self.assertIn(detail, result.detail)
                if failed_cleanup:
                    self.assertIn("cleanup returned HTTP 404", result.detail)
                self.assertEqual(len(cleanup_calls(http)), 4)

    def test_expired_run_cannot_begin_cleanup_after_a_model_reply(self) -> None:
        for expired in (False, True):
            now = [0.0]
            class TimedHttp(FakeHttp):
                def __call__(self, method, url, **kwargs):
                    outcome = super().__call__(method, url, **kwargs)
                    if url.endswith("/api/chat"):
                        now[0] = 10.0 if expired else 9.0
                    return outcome
            http = TimedHttp(canary_script(ok({"message": {"content": "ready"}})))
            deadline = pdv.Deadline(10.0, clock=lambda: now[0])
            result = self.run_canary(http, monotonic=lambda: now[0], deadline=deadline)
            self.assertEqual(result.ok, not expired)
            self.assertEqual(len(cleanup_calls(http)), 0 if expired else 1)
            if not expired:
                self.assertEqual(http.options[-1]["timeout"], 1.0)

    def test_local_and_shared_cleanup_deadlines_clamp_every_request(self) -> None:
        for boundary in ("cleanup", "run"):
            for expired in (False, True):
                with self.subTest(boundary=boundary, expired=expired):
                    now = [0.0]
                    class TimedHttp(FakeHttp):
                        def __call__(self, method, url, **kwargs):
                            outcome = super().__call__(method, url, **kwargs)
                            if method == "DELETE":
                                now[0] += 1.0
                            if url.endswith("/deletion/reconcile") and sum(
                                path.endswith("/deletion/reconcile") for _, path in self.calls
                            ) == 1:
                                now[0] += 4.0 if expired else 3.0
                            return outcome
                    http = TimedHttp(canary_script(ok({"message": {"content": "ready"}}), v1=True))
                    deadline = pdv.Deadline(5.0 if boundary == "run" else 100, clock=lambda: now[0])
                    with patch.object(pdv, "MAX_CLEANUP_SECONDS", 5.0 if boundary == "cleanup" else 60):
                        result = self.run_canary(http, monotonic=lambda: now[0], deadline=deadline)
                    self.assertEqual(result.ok, not expired, result.detail)
                    self.assertEqual(
                        [option["timeout"] for option in http.options[3:]],
                        [5, 4] if expired else [5, 4, 1, 1],
                    )
                    if expired:
                        self.assertIn("deadline exhausted", result.detail)

    def test_a_late_cleanup_response_cannot_pass_even_if_it_says_verified(self) -> None:
        for target in ("DELETE", "POST", "GET"):
            for expired in (False, True):
                with self.subTest(target=target, expired=expired):
                    now = [0.0]
                    class TimedHttp(FakeHttp):
                        def __call__(self, method, url, **kwargs):
                            outcome = super().__call__(method, url, **kwargs)
                            if kwargs.get("deadline") is not None and method == target:
                                now[0] += kwargs["timeout"] * (1.0 if expired else 0.5)
                            return outcome
                    http = TimedHttp(canary_script(ok({"message": {"content": "ready"}}), v1=True))
                    result = self.run_canary(http, monotonic=lambda: now[0])
                    self.assertEqual(result.ok, not expired, result.detail)
                    if expired:
                        self.assertEqual(http.calls[-1][0], target)

    def test_unverified_cleanup_cannot_be_successful_command_or_release_evidence(self) -> None:
        for complete in (False, True):
            for release in (False, True):
                with self.subTest(complete=complete, release=release):
                    http = FakeHttp(two_pass_cleanup_script(complete=complete))
                    args = pdv.build_parser().parse_args(
                        ["verify", "--state", STATE_FILE] if release
                        else ["canary", "--api-url", "https://api.test"]
                    )
                    with (
                        patch.dict("os.environ", {pdv.DEFAULT_TOKEN_ENV: TOKEN}),
                        patch.object(pdv, "load_model_catalog", return_value=CATALOG),
                        patch.object(pdv, "http_request", http),
                        redirect_stdout(io.StringIO()) as captured,
                    ):
                        result = pdv._canary_failures(args, "https://api.test") if release else pdv.cmd_canary(args)
                    self.assertEqual(result == [] if release else result == 0, complete)
                    if not release and not complete:
                        self.assertEqual(result, 3)
                    printed = captured.getvalue()
                    self.assertIn('"outcome":"passed"' if complete else '"outcome":"failed"', printed)
                    for private in (TOKEN, SID, "synthetic-owner", "requestedAt", "lastVerifiedAt"):
                        self.assertNotIn(private, printed)

    def test_deadline_includes_final_proof_validation(self) -> None:
        for expired in (False, True):
            now = [0.0]
            http = FakeHttp(canary_script(ok({"message": {"content": "ready"}}), v1=True))
            verify = pdv.verified_cleanup

            def proof(*args, **kwargs):
                result = verify(*args, **kwargs)
                if len(cleanup_calls(http)) == 4:
                    now[0] = pdv.MAX_CLEANUP_SECONDS - (0 if expired else 1)
                return result

            with patch.object(pdv, "verified_cleanup", proof):
                result = self.run_canary(http, monotonic=lambda: now[0])
            self.assertEqual(result.ok, not expired, result.detail)
            self.assertEqual(len(cleanup_calls(http)), 4)
            if expired:
                self.assertIn("deadline exhausted", result.detail)

    def test_canary_only_command_is_available_without_deploy_state(self) -> None:
        args = pdv.build_parser().parse_args(
            ["canary", "--api-url", "https://api.test"]
        )
        self.assertIs(args.func, pdv.cmd_canary)


class CleanupTransportTests(unittest.TestCase):
    def assert_cleanup_tls_context(self, context, expected_minimum):
        preserved = {
            "protocol": context.protocol,
            "maximum": context.maximum_version,
            "verify_mode": context.verify_mode,
            "check_hostname": context.check_hostname,
            "verify_flags": context.verify_flags,
            "certificates": context.cert_store_stats(),
            "ciphers": context.get_ciphers(),
        }
        raw = Mock(spec=socket.socket)
        encrypted = Mock(spec=ssl.SSLSocket)
        observed = []

        def wrap(active, sock, *, server_hostname):
            self.assertIs(active, context)
            self.assertIs(sock, raw)
            self.assertEqual(server_hostname, "api.test")
            self.assertEqual(active.minimum_version, expected_minimum)
            self.assertEqual(active.protocol, ssl.PROTOCOL_TLS_CLIENT)
            self.assertEqual(active.verify_mode, ssl.CERT_REQUIRED)
            self.assertIs(active.check_hostname, True)
            self.assertEqual(active.maximum_version, ssl.TLSVersion.MAXIMUM_SUPPORTED)
            self.assertTrue(ssl.HAS_TLSv1_3)
            self.assertIn("TLSv1.3", {cipher["protocol"] for cipher in active.get_ciphers()})
            self.assertEqual({
                "protocol": active.protocol,
                "maximum": active.maximum_version,
                "verify_mode": active.verify_mode,
                "check_hostname": active.check_hostname,
                "verify_flags": active.verify_flags,
                "certificates": active.cert_store_stats(),
                "ciphers": active.get_ciphers(),
            }, preserved)
            observed.append(active.minimum_version)
            return encrypted

        with (
            patch.object(pdv.ssl, "create_default_context", return_value=context) as factory,
            patch.object(pdv.socket, "create_connection", return_value=raw) as dial,
            patch.object(ssl.SSLContext, "wrap_socket", autospec=True, side_effect=wrap) as handshake,
        ):
            connection = pdv._CleanupConnection("api.test", 443, 1)
            try:
                self.assertIs(connection.tls_context, context)
                connection.connect()
                self.assertIs(connection.sock, encrypted)
                self.assertIs(connection.read_socket, encrypted)
                factory.assert_called_once_with()
                dial.assert_called_once_with(("api.test", 443), timeout=1)
                handshake.assert_called_once_with(context, raw, server_hostname="api.test")
                self.assertEqual(observed, [expected_minimum])
            finally:
                connection.close()
        encrypted.close.assert_called_once_with()

    def test_cleanup_tls_floor_upgrades_lowered_real_contexts(self) -> None:
        for minimum in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1):
            with self.subTest(initial_floor=minimum):
                context = ssl.create_default_context()
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    context.minimum_version = minimum
                self.assertEqual(context.minimum_version, minimum)
                self.assertLess(context.minimum_version, ssl.TLSVersion.TLSv1_2)
                self.assert_cleanup_tls_context(context, ssl.TLSVersion.TLSv1_2)

    def test_cleanup_tls_preserves_default_and_stricter_verified_contexts(self) -> None:
        for minimum in (None, ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
            with self.subTest(initial_floor=minimum):
                context = ssl.create_default_context()
                if minimum is not None:
                    context.minimum_version = minimum
                expected = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
                self.assert_cleanup_tls_context(context, expected)

    def native_canary(self, *, framing: str, v1: bool):
        http = FakeHttp(canary_script(ok({"message": {"content": "ready"}}), v1=v1))
        responses = []

        class Connection(pdv._CleanupConnection):
            peer = None

            def connect(self):
                self.sock, self.peer = socket.socketpair()
                self.read_socket = self.sock
                outcome = responses.pop(0)
                body = outcome.body
                headers = b"Connection: close\r\nContent-Type: application/json\r\n"
                if framing == "overflow":
                    body += b" " * (pdv.MAX_CLEANUP_BODY_BYTES + 1 - len(body))
                if outcome.status == 204:
                    headers += b"Content-Length: 0\r\n"
                elif framing.startswith("length") or framing == "overflow":
                    length = len(body) + (1 if framing == "length-truncated" else 0)
                    headers += f"Content-Length: {length}\r\n".encode()
                elif framing.startswith("chunked"):
                    headers += b"Transfer-Encoding: chunked\r\n"
                    body = (f"{len(body):x}\r\n".encode() + body + b"\r\n") if body else b""
                    if framing != "chunked-truncated":
                        body += b"0\r\n\r\n"
                elif framing == "eof-malformed":
                    body = body[:-1]
                self.peer.sendall(f"HTTP/1.1 {outcome.status} Synthetic\r\n".encode() + headers + b"\r\n" + body)

            def request(self, method, path, **kwargs):
                super().request(method, path, **kwargs)
                # An unread request makes Windows close the peer with a reset,
                # not the orderly EOF this response-framing control needs.
                self.peer.settimeout(1)
                received = b""
                while b"\r\n\r\n" not in received:
                    chunk = self.peer.recv(1024)
                    assert chunk and len(received) + len(chunk) <= 8192
                    received += chunk
                assert received.startswith(f"{method} {path} HTTP/1.1\r\n".encode())
                assert kwargs["body"] is None

            def close(self):
                super().close()
                if self.peer is not None:
                    self.peer.close()

        def request(method, url, **kwargs):
            outcome = http(method, url, **kwargs)
            if kwargs.get("deadline") is not None:
                responses.append(outcome)
                return pdv.http_request(method, url, **kwargs)
            return outcome

        # Real HTTPConnection/HTTPResponse and socket lifetime, without DNS,
        # TLS, Azure or a model. Only connection establishment is substituted.
        with patch.object(pdv, "_CleanupConnection", Connection):
            result = pdv.run_canary(
                api_base="https://api.test", token=TOKEN, catalog_doc=CATALOG, request=request,
            )
        return result, http

    def test_actual_stdlib_framing_preserves_complete_cleanup(self) -> None:
        for framing in ("length", "chunked", "eof"):
            for v1 in (False, True):
                with self.subTest(framing=framing, v1=v1):
                    result, http = self.native_canary(framing=framing, v1=v1)
                    self.assertTrue(result.ok, result.detail)
                    self.assertEqual(cleanup_calls(http), [
                        ("DELETE", SESSION_PATH),
                        ("POST", f"{SESSION_PATH}/deletion/reconcile"),
                        ("POST", f"{SESSION_PATH}/deletion/reconcile"),
                        ("GET", f"{SESSION_PATH}/deletion"),
                    ] if v1 else [("DELETE", SESSION_PATH)])

    def test_actual_stdlib_truncation_and_overflow_cannot_pass(self) -> None:
        for framing in ("length-truncated", "chunked-truncated", "eof-malformed", "overflow"):
            with self.subTest(framing=framing):
                result, http = self.native_canary(framing=framing, v1=True)
                self.assertFalse(result.ok)
                self.assertEqual(cleanup_calls(http), [("DELETE", SESSION_PATH)])

    def test_dns_headers_and_body_deadlines_bound_the_actual_canary_cleanup(self) -> None:
        for boundary in ("connect", "headers", "body"):
            for blocked in (False, True):
                with self.subTest(boundary=boundary, blocked=blocked):
                    connections = []
                    wire_calls = []

                    class Connection:
                        def __init__(self, *args):
                            self.released = threading.Event()
                            self.closed = threading.Event()
                            self.read_socket = SimpleNamespace(settimeout=lambda _: None)
                            connections.append(self)

                        def pause(self, phase):
                            if blocked and phase == boundary:
                                self.released.wait(1)

                        def connect(self):
                            self.pause("connect")

                        def request(self, method, path, **kwargs):
                            wire_calls.append((method, path))

                        def getresponse(self):
                            self.pause("headers")
                            return SimpleNamespace(
                                status=204, read1=self.read1,
                                getheader=lambda name, default=None: "0" if name == "Content-Length" else default,
                            )

                        def read1(self, limit):
                            self.pause("body")
                            return b""

                        def interrupt_read(self):
                            self.released.set()

                        def close(self):
                            self.closed.set()

                    http = FakeHttp(canary_script(ok({"message": {"content": "ready"}})))

                    def request(method, url, **kwargs):
                        scripted = http(method, url, **kwargs)
                        if kwargs.get("deadline") is not None:
                            return pdv.http_request(method, url, **kwargs)
                        return scripted

                    with patch.object(pdv, "_CleanupConnection", Connection):
                        result = pdv.run_canary(
                            api_base="https://api.test", token=TOKEN, catalog_doc=CATALOG,
                            request=request, timeout=0.05,
                        )
                        self.assertEqual(result.ok, not blocked, result.detail)
                        self.assertEqual(len(connections), 1)
                        self.assertTrue(connections[0].closed.wait(1))
                    self.assertEqual(wire_calls, [] if blocked and boundary == "connect" else [
                        ("DELETE", SESSION_PATH),
                    ])
                    self.assertEqual(cleanup_calls(http), [("DELETE", SESSION_PATH)])

    def test_native_cleanup_body_limit_and_response_headers_fail_closed(self) -> None:
        proof = json.dumps(CLEANUP_FIXTURE["verified"]).encode()
        for fault in ("none", "overflow", "truncated", "encoding", "redirect"):
            with self.subTest(fault=fault):
                reads = []
                limit = pdv.MAX_CLEANUP_BODY_BYTES
                body = proof + b" " * (limit - len(proof) + (1 if fault == "overflow" else 0))

                class Connection:
                    def __init__(self, *args):
                        self.stream = io.BytesIO(body)
                        self.read_socket = SimpleNamespace(settimeout=lambda _: None)

                    def connect(self):
                        pass

                    def request(self, method, path, **kwargs):
                        pass

                    def getresponse(self):
                        def header(name, default=None):
                            if name == "Content-Encoding" and fault == "encoding":
                                return "gzip"
                            if name == "Content-Length" and fault == "truncated":
                                return str(limit - 1)
                            return default
                        return SimpleNamespace(
                            status=302 if fault == "redirect" else 200,
                            getheader=header, read1=self.read1, isclosed=lambda: False,
                        )

                    def read1(self, amount):
                        reads.append(amount)
                        return self.stream.read(amount)

                    def interrupt_read(self):
                        pass

                    def close(self):
                        self.stream.close()

                http = FakeHttp(canary_script(ok({"message": {"content": "ready"}}), v1=True))

                def request(method, url, **kwargs):
                    scripted = http(method, url, **kwargs)
                    return pdv.http_request(method, url, **kwargs) if kwargs.get("deadline") else scripted

                with patch.object(pdv, "_CleanupConnection", Connection):
                    result = pdv.run_canary(
                        api_base="https://api.test", token=TOKEN, catalog_doc=CATALOG,
                        request=request,
                    )
                self.assertEqual(result.ok, fault == "none", result.detail)
                self.assertLessEqual(max(reads, default=0), limit)
                self.assertEqual(cleanup_calls(http), [
                    ("DELETE", SESSION_PATH), ("GET", f"{SESSION_PATH}/deletion"),
                ] if fault == "none" else [("DELETE", SESSION_PATH)])


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
    resource_group: str = GROUP,
) -> FakeAz:
    apps = {
        APPS["api"]: container_app(
            name=APPS["api"], revision=api_revision, fqdn="api.test", resource_group=resource_group
        ),
        APPS["web"]: container_app(
            name=APPS["web"],
            revision=web_revision,
            fqdn="web.test",
            custom_domains=custom_domains,
            resource_group=resource_group,
        ),
        APPS["proxy"]: container_app(
            name=APPS["proxy"], revision=proxy_revision, fqdn="proxy.test", min_replicas=0,
            resource_group=resource_group,
        ),
    }
    revisions = {}
    for service, name in APPS.items():
        props = apps[name]["properties"]
        current = props["latestReadyRevisionName"]
        captured = f"{name}--r1"
        minimum = 0 if service == "proxy" else 1
        if current == captured:
            props["template"]["containers"][0]["image"] = RESTORED_IMAGE
        revisions[(name, captured)] = revision_payload(
            name=captured, active=False, health=None, running=None, provisioned=None,
            replicas=0, image=RESTORED_IMAGE, min_replicas=minimum,
            resource_group=resource_group,
        )
        revisions[(name, current)] = revision_payload(
            name=current, template=props["template"], min_replicas=minimum, replicas=minimum,
            running="Running" if minimum else "ScaledToZero", resource_group=resource_group,
        )
    if api_detail is not None:
        revisions[(APPS["api"], api_revision)] = api_detail
    return FakeAz(apps=apps, revisions=revisions, resource_group=resource_group)


def pending_world(*, image: str = "mcr.microsoft.com/k8se/quickstart:latest") -> FakeAz:
    az = world(api_revision=f"{APPS['api']}--r1")
    pending = f"{APPS['api']}--pending"
    detail = revision_payload(
        name=pending, image=image, health="None", running="Processing", replicas=0
    )
    az.revisions[(APPS["api"], pending)] = detail
    az.apps[APPS["api"]]["properties"].update(
        latestRevisionName=pending,
        template=deepcopy(detail["properties"]["template"]),
        provisioningState="Failed",
    )
    return az


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

        az = world(resource_group=f"rg-nomad-{ENV}")
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
            "GET /liveness": [pdv.HttpOutcome(status=200)],
            "GET /readiness": [pdv.HttpOutcome(status=200)],
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

    def test_an_unreachable_supported_proxy_probe_fails(self) -> None:
        for path in pdv.PROXY_PROBE_PATHS:
            with self.subTest(path=path):
                http = healthy_http()
                http.script[f"GET {path}"] = [pdv.HttpOutcome(status=503)]
                code, out = self.verify(az=world(), http=http)
                self.assertEqual(code, 3)
                self.assertIn(f"proxy: {path} never answered", out)

    def test_an_authenticating_proxy_that_rejects_the_probe_still_passes(self) -> None:
        http = healthy_http()
        http.script["GET /startup"] = [pdv.HttpOutcome(status=401)]
        code, out = self.verify(az=world(), http=http)
        self.assertEqual(code, 0, out)

    def test_live_verification_only_calls_side_effect_free_proxy_probes(self) -> None:
        http = healthy_http()
        code, out = self.verify(az=world(), http=http)
        self.assertEqual(code, 0, out)
        proxy_paths = {
            urlsplit(url).path
            for _, url in http.calls
            if urlsplit(url).hostname == "proxy.test"
        }
        self.assertEqual(set(pdv.PROXY_PROBE_PATHS), proxy_paths)

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


class CutoverEvidenceTests(unittest.TestCase):
    def verify_web(self, az: FakeAz) -> tuple[int, dict, str]:
        code, out = VerifyTests().verify(az=az)
        events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        event = next(row for row in events if row["event"] == "rollout" and row["service"] == "web")
        return code, event["cutover"], out

    def test_each_failed_cutover_condition_is_distinguishable(self) -> None:
        changes = (
            ("latest", "latestMatchesServing"),
            ("provisioning", "appProvisioningSucceeded"),
            ("template", "templateComparison"),
        )
        for change, field in changes:
            with self.subTest(change=change):
                az = world()
                code, healthy, _ = self.verify_web(az)
                self.assertEqual(code, 0)
                self.assertEqual(healthy["templateComparison"], "equal")
                self.assertTrue(healthy["latestMatchesServing"])
                self.assertTrue(healthy["appProvisioningSucceeded"])
                props = az.apps[APPS["web"]]["properties"]
                saved = deepcopy(props)
                if change == "latest":
                    props["latestRevisionName"] = f"{APPS['web']}--pending"
                elif change == "provisioning":
                    props["provisioningState"] = "Updating"
                else:
                    props["template"]["scale"]["maxReplicas"] = 4
                code, evidence, _ = self.verify_web(az)
                self.assertEqual(code, 3)
                self.assertEqual(evidence[field], "different" if change == "template" else False)
                if change == "template":
                    self.assertEqual(evidence["templateDifferenceAreas"], ["scale.maxReplicas"])
                az.apps[APPS["web"]]["properties"] = saved
                self.assertEqual(self.verify_web(az)[0], 0)

    def test_diagnostics_reuse_projection_and_preserve_json_types(self) -> None:
        az = world()
        desired = az.apps[APPS["web"]]["properties"]["template"]
        actual = az.revisions[(APPS["web"], f"{APPS['web']}--r2")]["properties"]["template"]
        desired["containers"][0]["resources"] = {"cpu": 0.5, "memory": "1Gi", "ephemeralStorage": "2Gi"}
        actual["containers"][0]["resources"] = {"cpu": 0.5, "memory": "1Gi"}
        desired["scale"].update(cooldownPeriod=300, pollingInterval=30)
        code, evidence, _ = self.verify_web(az)
        self.assertEqual(code, 0)
        self.assertEqual(evidence["templateComparison"], "equal")
        for changed in (3.0, True):
            with self.subTest(changed=changed):
                desired["scale"]["maxReplicas"] = changed
                code, evidence, _ = self.verify_web(az)
                self.assertEqual(code, 3)
                self.assertEqual(evidence["templateDifferenceAreas"], ["scale.maxReplicas"])
        desired["scale"]["maxReplicas"] = 3
        self.assertEqual(self.verify_web(az)[0], 0)

    def test_values_and_unknown_field_names_never_reach_logs(self) -> None:
        az = world()
        self.assertEqual(self.verify_web(az)[0], 0)
        desired = az.apps[APPS["web"]]["properties"]["template"]
        private_name = "private-infrastructure-label"
        private_value = "unstructured-sensitive-setting"
        desired["containers"][0]["env"] = [{"name": private_name, "value": private_value}]
        desired[private_name] = private_value
        code, evidence, out = self.verify_web(az)
        self.assertEqual(code, 3)
        self.assertEqual(evidence["templateDifferenceAreas"], ["containers.env", "other"])
        self.assertNotIn(private_name, out)
        self.assertNotIn(private_value, out)
        self.assertNotIn("api.test", json.dumps(evidence))
        del desired["containers"][0]["env"]
        del desired[private_name]
        self.assertEqual(self.verify_web(az)[0], 0)

    def test_difference_summary_has_a_fixed_bound(self) -> None:
        az = world()
        desired = az.apps[APPS["web"]]["properties"]["template"]
        actual = az.revisions[(APPS["web"], f"{APPS['web']}--r2")]["properties"]["template"]
        for template in (desired, actual):
            template["initContainers"] = [{"name": "init", "image": "acr.azurecr.io/init:1"}]
        self.assertEqual(self.verify_web(az)[0], 0)
        for collection in ("containers", "initContainers"):
            desired[collection][0].update(
                name="different", image="acr.azurecr.io/changed:1",
                env=[{"name": "private", "value": "not-for-logs"}],
                resources={"cpu": 0.25}, command=["private-command"],
                args=["private-argument"], probes=[{"type": "Liveness"}],
                volumeMounts=[{"volumeName": "private-volume", "mountPath": "/private"}],
                private_field="not-for-logs",
            )
        code, evidence, _ = self.verify_web(az)
        self.assertEqual(code, 3)
        self.assertEqual(len(evidence["templateDifferenceAreas"]), 12)
        self.assertTrue(evidence["differenceAreasTruncated"])
        self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=True).encode()), 1024)

    def test_probe_field_shapes_distinguish_missing_null_empty_and_other_json_types(self) -> None:
        cases = (
            ({}, {"kind": "missing"}),
            ({"probes": None}, {"kind": "null"}),
            ({"probes": [{"private-field": "private-probe-value"}]}, {"kind": "array", "count": 1}),
            ({"probes": {"private-field": "private-probe-value"}}, {"kind": "object"}),
            ({"probes": "private-probe-value"}, {"kind": "string"}),
            ({"probes": True}, {"kind": "boolean"}),
            ({"probes": 1}, {"kind": "number"}),
            ({"probes": 1.5}, {"kind": "number"}),
        )
        for changed, expected_shape in cases:
            with self.subTest(shape=expected_shape):
                az = world()
                desired = az.apps[APPS["web"]]["properties"]["template"]["containers"][0]
                actual = az.revisions[(APPS["web"], f"{APPS['web']}--r2")]["properties"]["template"]["containers"][0]
                desired["probes"] = []
                actual["probes"] = []
                code, healthy, _ = self.verify_web(az)
                self.assertEqual(code, 0)
                self.assertNotIn("probeFieldShapes", healthy)
                del desired["probes"]
                desired.update(changed)
                before_apps, before_revisions = deepcopy(az.apps), deepcopy(az.revisions)
                az.calls.clear()
                code, evidence, out = self.verify_web(az)
                self.assertEqual(code, 3)
                self.assertEqual(evidence["templateComparison"], "different")
                self.assertEqual(evidence["templateDifferenceAreas"], ["containers.probes"])
                self.assertIn("probeFieldShapes", evidence)
                self.assertEqual(evidence["probeFieldShapes"], [{
                    "area": "containers.probes", "index": 0,
                    "desired": expected_shape, "serving": {"kind": "array", "count": 0},
                }])
                self.assertFalse(evidence["probeFieldShapesTruncated"])
                self.assertNotIn("private-field", out)
                self.assertNotIn("private-probe-value", out)
                self.assertEqual(len(az.calls), 9)
                self.assertEqual(az.apps, before_apps)
                self.assertEqual(az.revisions, before_revisions)
                desired["probes"] = []
                self.assertEqual(self.verify_web(az)[0], 0)

    def test_matching_probe_shapes_do_not_hide_changed_configuration(self) -> None:
        az = world()
        desired = az.apps[APPS["web"]]["properties"]["template"]["containers"][0]
        actual = az.revisions[(APPS["web"], f"{APPS['web']}--r2")]["properties"]["template"]["containers"][0]
        probes = [{
            "type": "Readiness",
            "httpGet": {
                "path": "/private-probe-path", "port": 8080,
                "httpHeaders": [{"name": "private-header-name", "value": "private-header-value"}],
            },
        }]
        desired["probes"] = deepcopy(probes)
        actual["probes"] = deepcopy(probes)
        self.assertEqual(self.verify_web(az)[0], 0)
        desired["probes"][0]["httpGet"]["httpHeaders"][0]["value"] = "private-changed-value"
        code, evidence, out = self.verify_web(az)
        self.assertEqual(code, 3)
        self.assertEqual(evidence["templateComparison"], "different")
        for private in ("private-probe-path", "private-header-name", "private-header-value", "private-changed-value"):
            self.assertNotIn(private, out)
        self.assertIn("probeFieldShapes", evidence)
        self.assertEqual(evidence["probeFieldShapes"], [{
            "area": "containers.probes", "index": 0,
            "desired": {"kind": "array", "count": 1}, "serving": {"kind": "array", "count": 1},
        }])
        desired["probes"] = deepcopy(probes)
        self.assertEqual(self.verify_web(az)[0], 0)

    def test_probe_shape_bound_covers_both_container_collections(self) -> None:
        for collection in ("containers", "initContainers"):
            with self.subTest(collection=collection):
                az = world()
                desired = az.apps[APPS["web"]]["properties"]["template"]
                actual = az.revisions[(APPS["web"], f"{APPS['web']}--r2")]["properties"]["template"]
                containers = [
                    {**deepcopy(actual["containers"][0]), "name": f"private-container-{i}", "probes": []}
                    for i in range(3)
                ]
                desired[collection] = deepcopy(containers)
                actual[collection] = deepcopy(containers)
                self.assertEqual(self.verify_web(az)[0], 0)
                for container in desired[collection]:
                    container["probes"] = None
                code, evidence, out = self.verify_web(az)
                self.assertEqual(code, 3)
                self.assertIn("probeFieldShapes", evidence)
                self.assertEqual(evidence["probeFieldShapes"], [
                    {
                        "area": f"{collection}.probes", "index": i,
                        "desired": {"kind": "null"}, "serving": {"kind": "array", "count": 0},
                    }
                    for i in range(2)
                ])
                self.assertTrue(evidence["probeFieldShapesTruncated"])
                self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=True).encode()), 1024)
                self.assertNotIn("private-container", out)
                desired[collection] = deepcopy(containers)
                self.assertEqual(self.verify_web(az)[0], 0)

    def test_unavailable_and_invalid_are_not_equal(self) -> None:
        az = world()
        self.assertEqual(self.verify_web(az)[0], 0)
        az.apps[APPS["web"]]["properties"]["template"]["scale"]["cooldownPeriod"] = "private-invalid"
        code, evidence, out = self.verify_web(az)
        self.assertEqual(code, 3)
        self.assertEqual(evidence["templateComparison"], "invalid")
        self.assertNotIn("private-invalid", out)
        del az.apps[APPS["web"]]
        code, evidence, _ = self.verify_web(az)
        self.assertEqual(code, 3)
        self.assertEqual(evidence, {"observation": "unavailable"})

    def test_multiple_mode_does_not_claim_single_mode_checks(self) -> None:
        az = world()
        self.assertEqual(self.verify_web(az)[0], 0)
        props = az.apps[APPS["web"]]["properties"]
        props["configuration"]["activeRevisionsMode"] = "Multiple"
        props["configuration"]["ingress"]["traffic"] = [
            {"revisionName": f"{APPS['web']}--r2", "weight": 100}
        ]
        props["latestRevisionName"] = f"{APPS['web']}--pending"
        props["template"]["scale"]["maxReplicas"] = 4
        code, evidence, _ = self.verify_web(az)
        self.assertEqual(code, 0)
        self.assertFalse(evidence["singleMode"])
        self.assertTrue(evidence["servingObserved"])
        self.assertNotIn("templateComparison", evidence)

    def test_evidence_uses_existing_observation_without_additional_reads(self) -> None:
        az = world()
        code, evidence, _ = self.verify_web(az)
        self.assertEqual(code, 0)
        self.assertEqual(evidence["observation"], "complete")
        self.assertEqual(len(az.calls), 9)
        self.assertTrue(all(
            call[:2] == ["containerapp", "show"] or call[:3] == ["containerapp", "revision", "show"]
            for call in az.calls
        ))


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
        self, az: FakeAz, *, captured: dict[str, str | None] | None = None,
        snapshot_overrides: dict[str, dict] | None = None,
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
                    "minReplicas": 0 if service == "proxy" else 1,
                    "image": RESTORED_IMAGE,
                    "fqdn": None,
                    **(snapshot_overrides or {}).get(service, {}),
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
        self.assertIn("serving or pending failed-deploy state remains unverified", out)

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


class ServingRevisionContractTests(unittest.TestCase):
    def snapshot(self, az: FakeAz) -> Any:
        with patch.object(pdv, "run_az", az):
            return pdv.snapshot_app(GROUP, "api", APPS["api"], attempts=1)

    def test_capture_uses_the_ready_template_not_the_pending_template(self) -> None:
        az = pending_world()
        az.apps[APPS["api"]]["properties"]["template"]["scale"]["minReplicas"] = 0
        snapshot = self.snapshot(az)
        self.assertEqual(snapshot.revision, f"{APPS['api']}--r1")
        self.assertEqual(snapshot.image, RESTORED_IMAGE)
        self.assertEqual(snapshot.minReplicas, 1)
        self.assertEqual(snapshot.to_dict()["image"], RESTORED_IMAGE)
        reads = [call for call in az.calls if call[:3] == ["containerapp", "revision", "show"]]
        self.assertEqual(
            reads,
            [[
                "containerapp", "revision", "show", "-g", GROUP, "-n", APPS["api"],
                "--subscription", SUBSCRIPTION, "--revision", snapshot.revision, "-o", "json",
            ]],
        )
        self.assertEqual(len(az.calls), 3)
        self.assertFalse([call for call in az.calls if "copy" in call or "set" in call])

    def test_no_ready_revision_never_captures_the_placeholder(self) -> None:
        az = pending_world()
        self.assertEqual(self.snapshot(az).image, RESTORED_IMAGE)
        az.apps[APPS["api"]]["properties"]["latestReadyRevisionName"] = ""
        az.calls.clear()
        snapshot = self.snapshot(az)
        self.assertTrue(snapshot.exists)
        self.assertIsNone(snapshot.revision)
        self.assertIsNone(snapshot.image)
        self.assertIsNone(snapshot.minReplicas)
        self.assertFalse([call for call in az.calls if "--revision" in call])

    def test_a_non_ready_selected_revision_cannot_be_captured(self) -> None:
        az = pending_world()
        self.assertEqual(self.snapshot(az).image, RESTORED_IMAGE)
        az.apps[APPS["api"]]["properties"]["latestReadyRevisionName"] = f"{APPS['api']}--pending"
        with self.assertRaisesRegex(pdv.VerifyInputError, "healthState is None"):
            self.snapshot(az)

    def test_expected_desired_image_is_not_proof_of_the_serving_image(self) -> None:
        az = pending_world(image=DIGEST_B)
        options = ["--skip-canary", "--expect-image", f"api={DIGEST_B}"]
        code, out = VerifyTests().verify(az=az, extra_args=options)
        self.assertEqual(code, 3, out)
        self.assertIn("not the image this deploy pushed", out)
        events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        api = next(row for row in events if row["event"] == "rollout" and row["service"] == "api")
        self.assertEqual(api["current"], f"{APPS['api']}--r1")
        self.assertEqual(api["image"], RESTORED_IMAGE)
        self.assertTrue(az.promote_pending(APPS["api"], f"{APPS['api']}--pending"))
        code, out = VerifyTests().verify(az=az, extra_args=options)
        self.assertEqual(code, 0, out)

    def test_even_the_expected_serving_image_does_not_hide_a_pending_cutover(self) -> None:
        az = pending_world(image=RESTORED_IMAGE)
        options = ["--skip-canary", "--expect-image", f"api={RESTORED_IMAGE}"]
        code, out = VerifyTests().verify(az=az, extra_args=options)
        self.assertEqual(code, 3, out)
        self.assertIn("pending or different desired template", out)
        self.assertTrue(az.promote_pending(APPS["api"], f"{APPS['api']}--pending"))
        code, out = VerifyTests().verify(az=az, extra_args=options)
        self.assertEqual(code, 0, out)

    def test_replica_requirement_comes_from_the_selected_revision(self) -> None:
        az = world()
        props = az.apps[APPS["api"]]["properties"]
        props["configuration"]["activeRevisionsMode"] = "Multiple"
        props["configuration"]["ingress"]["traffic"] = [
            {"revisionName": f"{APPS['api']}--r2", "weight": 100}
        ]
        detail = az.revisions[(APPS["api"], f"{APPS['api']}--r2")]["properties"]
        detail.update(runningState="ScaledToZero", replicas=0)
        props["template"]["scale"]["minReplicas"] = 0
        code, out = VerifyTests().verify(az=az)
        self.assertEqual(code, 3, out)
        self.assertIn("0 running replicas", out)
        detail["template"]["scale"]["minReplicas"] = 0
        props["template"]["scale"]["minReplicas"] = 1
        code, out = VerifyTests().verify(az=az)
        self.assertEqual(code, 0, out)

    def test_missing_and_cross_scope_revision_metadata_fail_closed(self) -> None:
        changes = [
            (("name",), None),
            (("name",), f"{APPS['web']}--r2"),
            (("type",), "Microsoft.App/containerapps"),
            (("id",), f"{app_id(APPS['web'])}/revisions/{APPS['api']}--r2"),
            (("id",), f"{app_id(APPS['api'], 'rg-other')}/revisions/{APPS['api']}--r2"),
            (("id",), f"{app_id(APPS['api'])}/revisions/{APPS['api']}--r1"),
            (("properties", "template", "containers"), []),
            (("properties", "template", "scale", "minReplicas"), None),
            (("properties", "template", "scale", "minReplicas"), True),
            (("properties", "template", "scale", "minReplicas"), -1),
            (("properties", "active"), None),
            (("properties", "healthState"), None),
            (("properties", "provisioningState"), None),
            (("properties", "runningState"), None),
        ]
        for path, value in changes:
            with self.subTest(path=path, value=value):
                az = world()
                self.assertIsNotNone(self.snapshot(az).revision)
                detail = az.revisions[(APPS["api"], f"{APPS['api']}--r2")]
                parent = detail
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = value
                with self.assertRaises(pdv.VerifyInputError):
                    self.snapshot(az)

    def test_missing_revision_is_not_a_greenfield_app(self) -> None:
        az = world()
        self.assertTrue(self.snapshot(az).exists)
        del az.revisions[(APPS["api"], f"{APPS['api']}--r2")]
        with self.assertRaisesRegex(pdv.VerifyInputError, "revision not found"):
            self.snapshot(az)

    def test_missing_or_foreign_app_identity_is_not_a_valid_observation(self) -> None:
        for key, value in (
            ("id", None), ("id", app_id(APPS["api"], "rg-other")),
            ("name", APPS["web"]), ("type", None),
        ):
            with self.subTest(key=key, value=value):
                az = world()
                self.assertTrue(self.snapshot(az).exists)
                az.apps[APPS["api"]][key] = value
                with self.assertRaises(pdv.VerifyInputError):
                    self.snapshot(az)

    def test_app_changes_during_exact_revision_reads_are_not_mixed(self) -> None:
        for field, value in (
            ("latestReadyRevisionName", f"{APPS['api']}--r1"),
            ("latestRevisionName", f"{APPS['api']}--pending"),
            ("provisioningState", "Updating"),
            ("template", {"containers": [], "scale": {"minReplicas": 0}}),
        ):
            with self.subTest(field=field):
                az = world()
                self.assertTrue(self.snapshot(az).exists)

                def change_during_read(args, **kwargs):
                    response = az(args, **kwargs)
                    if list(args)[:3] == ["containerapp", "revision", "show"] and APPS["api"] in args:
                        az.apps[APPS["api"]]["properties"][field] = value
                    return response

                with self.assertRaisesRegex(pdv.VerifyInputError, "app changed"):
                    self.snapshot(inert_az(az, change_during_read))

    def test_failed_provision_pending_revision_is_copied_away_not_skipped(self) -> None:
        az = pending_world()
        pending = f"{APPS['api']}--pending"
        unprotected = deepcopy(az)
        self.assertTrue(unprotected.promote_pending(APPS["api"], pending))
        self.assertNotEqual(self.snapshot(unprotected).image, RESTORED_IMAGE)
        code, out, _ = RollbackTests().rollback(az)
        self.assertEqual(code, 0, out)
        copies = [call for call in az.calls if call[:3] == ["containerapp", "revision", "copy"]]
        self.assertEqual(len(copies), 3)
        self.assertIn([
            "containerapp", "revision", "copy", "-g", GROUP, "-n", APPS["api"],
            "--subscription", SUBSCRIPTION, "--from-revision", f"{APPS['api']}--r1", "-o", "none",
        ], copies)
        self.assertIn('"restored":3,"failed":0', out)
        self.assertFalse(az.promote_pending(APPS["api"], pending))
        self.assertEqual(self.snapshot(az).image, RESTORED_IMAGE)
        props = az.apps[APPS["api"]]["properties"]
        self.assertEqual(props["latestRevisionName"], props["latestReadyRevisionName"])
        self.assertNotEqual(props["latestReadyRevisionName"], pending)
        self.assertIs(az.revisions[(APPS["api"], pending)]["properties"]["active"], False)

    def test_true_unchanged_ready_latest_and_template_is_a_noop(self) -> None:
        az = world(api_revision=f"{APPS['api']}--r1")
        code, out, _ = RollbackTests().rollback(az)
        self.assertEqual(code, 0, out)
        self.assertFalse([call for call in az.calls if "copy" in call and APPS["api"] in call])
        self.assertIn("healthy with no pending cutover", out)
        # Even same-image environment drift must restore the full captured template.
        az = world(api_revision=f"{APPS['api']}--r1")
        az.apps[APPS["api"]]["properties"]["template"]["containers"][0]["env"] = [
            {"name": "MODE", "value": "pending"}
        ]
        code, out, _ = RollbackTests().rollback(az)
        self.assertEqual(code, 0, out)
        self.assertTrue([call for call in az.calls if "copy" in call and APPS["api"] in call])
        self.assertNotIn("env", az.apps[APPS["api"]]["properties"]["template"]["containers"][0])

    def test_copy_preserves_the_captured_full_template_and_zero_minimum(self) -> None:
        az = pending_world()
        source = az.revisions[(APPS["api"], f"{APPS['api']}--r1")]["properties"]
        source["template"]["containers"][0].update(
            env=[{"name": "MODE", "value": "captured"}],
            probes=[{"type": "Readiness", "httpGet": {"path": "/ready", "port": 8080}}],
        )
        source["template"]["scale"]["minReplicas"] = 0
        source.update(runningState="ScaledToZero", replicas=0)
        expected = deepcopy(source["template"])
        code, out, _ = RollbackTests().rollback(
            az, snapshot_overrides={"api": {"minReplicas": 0}}
        )
        self.assertEqual(code, 0, out)
        snapshot = self.snapshot(az)
        self.assertEqual(snapshot.minReplicas, 0)
        self.assertEqual(snapshot.image, RESTORED_IMAGE)
        actual = az.revisions[(APPS["api"], snapshot.revision)]["properties"]["template"]
        self.assertNotEqual(actual["revisionSuffix"], expected["revisionSuffix"])
        actual.pop("revisionSuffix")
        expected.pop("revisionSuffix")
        self.assertEqual(actual, expected)

    def test_old_state_must_match_current_captured_revision_bytes_before_any_copy(self) -> None:
        for override in (
            {"image": None}, {"image": "mcr.microsoft.com/k8se/quickstart:latest"},
            {"minReplicas": None}, {"minReplicas": 0},
        ):
            with self.subTest(override=override):
                az = pending_world()
                code, out, _ = RollbackTests().rollback(az, snapshot_overrides={"api": override})
                self.assertEqual(code, 4, out)
                self.assertIn("existing state cannot authorize a restore", out)
                self.assertFalse([call for call in az.calls if "copy" in call and APPS["api"] in call])
                self.assertEqual(out.count('"outcome":"restored"'), 2)
                control = pending_world()
                code, out, _ = RollbackTests().rollback(control)
                self.assertEqual(code, 0, out)
                self.assertTrue([call for call in control.calls if "copy" in call and APPS["api"] in call])

    def test_incomplete_or_changed_current_reads_do_not_abandon_other_apps(self) -> None:
        for failure in ("missing", "identity", "configuration", "changed", "unavailable"):
            with self.subTest(failure=failure):
                az = world()
                if failure == "missing":
                    del az.revisions[(APPS["api"], f"{APPS['api']}--r1")]
                elif failure == "identity":
                    az.revisions[(APPS["api"], f"{APPS['api']}--r1")]["name"] = f"{APPS['web']}--r1"
                elif failure == "configuration":
                    del az.apps[APPS["api"]]["properties"]["latestRevisionName"]

                def bad_read(args, **kwargs):
                    if APPS["api"] in args and list(args)[:3] == ["containerapp", "revision", "show"]:
                        if failure == "unavailable":
                            az.calls.append(list(args))
                            return 1, "", "ERROR: read unavailable"
                        response = az(args, **kwargs)
                        if failure == "changed":
                            az.apps[APPS["api"]]["properties"]["provisioningState"] = "Updating"
                        return response
                    return az(args, **kwargs)

                code, out, _ = RollbackTests().rollback(inert_az(az, bad_read))
                self.assertEqual(code, 4, out)
                self.assertIn('"outcome":"unreadable"', out)
                self.assertEqual(out.count('"outcome":"restored"'), 2)
                self.assertFalse([call for call in az.calls if "copy" in call and APPS["api"] in call])

    def test_accepted_copy_needs_actual_healthy_template_and_retired_pending_candidate(self) -> None:
        for fault in ("image", "health", "provisioned", "replicas", "active_pending", "latest_pending"):
            with self.subTest(fault=fault):
                az = pending_world()
                pending = f"{APPS['api']}--pending"

                def incomplete_copy(args, **kwargs):
                    response = az(args, **kwargs)
                    if list(args)[:3] != ["containerapp", "revision", "copy"] or APPS["api"] not in args:
                        return response
                    app = az.apps[APPS["api"]]["properties"]
                    serving = az.revisions[(APPS["api"], app["latestReadyRevisionName"])]["properties"]
                    if fault == "image":
                        serving["template"]["containers"][0]["image"] = DIGEST_B
                    elif fault == "health":
                        serving["healthState"] = None
                    elif fault == "provisioned":
                        serving["provisioningState"] = "Provisioning"
                    elif fault == "replicas":
                        serving["replicas"] = 0
                    else:
                        az.revisions[(APPS["api"], pending)]["properties"]["active"] = True
                        if fault == "latest_pending":
                            app["latestRevisionName"] = pending
                    return response

                code, out, _ = RollbackTests().rollback(inert_az(az, incomplete_copy))
                self.assertEqual(code, 4, out)
                self.assertIn('"outcome":"unconfirmed"', out)
                self.assertEqual(out.count('"outcome":"restored"'), 2)
                if fault == "latest_pending":
                    self.assertTrue(az.promote_pending(APPS["api"], pending))
                    self.assertNotEqual(self.snapshot(az).image, RESTORED_IMAGE)
                code, out, _ = RollbackTests().rollback(pending_world())
                self.assertEqual(code, 0, out)

    def test_confirmation_unknown_revision_read_is_not_success(self) -> None:
        az = pending_world()

        def missing_copy_metadata(args, **kwargs):
            if "--revision" in args and any(arg.endswith("-restored") for arg in args):
                az.calls.append(list(args))
                return 1, "", "ERROR: copied revision read unavailable"
            return az(args, **kwargs)

        code, out, _ = RollbackTests().rollback(inert_az(az, missing_copy_metadata))
        self.assertEqual(code, 4, out)
        self.assertEqual(out.count('"outcome":"unconfirmed"'), 3)
        self.assertEqual(len([call for call in az.calls if "copy" in call]), 3)

    def test_an_accepted_copy_with_lost_acknowledgement_is_not_replayed(self) -> None:
        az = pending_world()

        def lost_ack(args, **kwargs):
            response = az(args, **kwargs)
            if list(args)[:3] == ["containerapp", "revision", "copy"] and APPS["api"] in args:
                raise pdv.AzError("copy acknowledgement was lost")
            return response

        code, out, _ = RollbackTests().rollback(inert_az(az, lost_ack))
        self.assertEqual(code, 4, out)
        self.assertIn("copy acknowledgement was lost", out)
        self.assertEqual(out.count('"outcome":"restored"'), 2)
        self.assertEqual(len([call for call in az.calls if "copy" in call and APPS["api"] in call]), 1)
        self.assertEqual(self.snapshot(az).image, RESTORED_IMAGE)

    def test_confirmation_stays_bound_to_the_written_subscription(self) -> None:
        other_subscription = "00000000-0000-0000-0000-000000000002"
        for wrong_response in (False, True):
            with self.subTest(wrong_response=wrong_response):
                az = pending_world()
                copied = False
                confirmation_calls = []

                def changed_default(args, **kwargs):
                    nonlocal copied
                    argv = list(args)
                    if argv[:3] == ["containerapp", "revision", "copy"] and APPS["api"] in argv:
                        copied = True
                        return az(argv, **kwargs)
                    if copied and APPS["api"] in argv:
                        confirmation_calls.append(argv)
                        requested = FakeAz._flag(argv, "--subscription")
                        if wrong_response or requested in (None, other_subscription):
                            translated = [
                                SUBSCRIPTION if value == other_subscription else value for value in argv
                            ]
                            code, output, error = az(translated, **kwargs)
                            if code == 0:
                                payload = json.loads(output)
                                payload["id"] = payload["id"].replace(SUBSCRIPTION, other_subscription)
                                output = json.dumps(payload)
                            return code, output, error
                    return az(argv, **kwargs)

                code, out, _ = RollbackTests().rollback(inert_az(az, changed_default))
                self.assertEqual(code, 4 if wrong_response else 0, out)
                self.assertTrue(confirmation_calls)
                self.assertTrue(all(
                    FakeAz._flag(call, "--subscription") == SUBSCRIPTION for call in confirmation_calls
                ))
                if wrong_response:
                    self.assertIn("different subscription", out)
                    self.assertIn('"outcome":"unconfirmed"', out)
                    self.assertEqual(out.count('"outcome":"restored"'), 2)

    def test_multiple_mode_restores_exact_weights_without_copying_the_unrouted_latest(self) -> None:
        az = pending_world()
        props = az.apps[APPS["api"]]["properties"]
        captured = f"{APPS['api']}--r1"
        pending = f"{APPS['api']}--pending"
        props["configuration"]["activeRevisionsMode"] = "Multiple"
        props["configuration"]["ingress"]["traffic"] = [
            {"revisionName": captured, "weight": 80},
            {"revisionName": f"{APPS['api']}--other", "weight": 20},
            {"revisionName": pending, "weight": 0},
        ]
        self.assertEqual(self.snapshot(az).image, RESTORED_IMAGE)
        code, out = VerifyTests().verify(
            az=az, extra_args=["--skip-canary", "--expect-image", f"api={RESTORED_IMAGE}"]
        )
        self.assertEqual(code, 0, out)
        code, out, _ = RollbackTests().rollback(
            az, snapshot_overrides={"api": {"revisionsMode": "Multiple"}}
        )
        self.assertEqual(code, 0, out)
        writes = [call for call in az.calls if "set" in call and APPS["api"] in call]
        self.assertEqual(writes, [[
            "containerapp", "ingress", "traffic", "set", "-g", GROUP, "-n", APPS["api"],
            "--subscription", SUBSCRIPTION, "--revision-weight", f"{captured}=100", "-o", "none",
        ]])
        self.assertFalse([call for call in az.calls if "copy" in call and APPS["api"] in call])
        self.assertEqual(props["latestRevisionName"], pending)
        self.assertIs(az.revisions[(APPS["api"], pending)]["properties"]["active"], True)
        az.calls.clear()
        code, out, _ = RollbackTests().rollback(
            az, snapshot_overrides={"api": {"revisionsMode": "Multiple"}}
        )
        self.assertEqual(code, 0, out)
        self.assertFalse([call for call in az.calls if "set" in call and APPS["api"] in call])


class TemplateProjectionTests(unittest.TestCase):
    def world(self) -> FakeAz:
        fixture = json.loads(
            (ROOT / "scripts/tests/fixtures/container_app_template_projection.json").read_text(
                encoding="utf-8"
            )
        )
        az = world()
        for name in APPS.values():
            for revision in (f"{name}--r1", f"{name}--r2"):
                props = az.revisions[(name, revision)]["properties"]
                template = props["template"]
                template["containers"][0].update(
                    image=RESTORED_IMAGE,
                    resources={**fixture["revision"]["resources"], "memory": "1Gi"},
                    env=[
                        {"name": "MODE", "value": "synthetic"},
                        {"name": "TOKEN", "secretRef": "synthetic-secret"},
                    ],
                    probes=[{"type": "Readiness", "httpGet": {"path": "/ready", "port": 8080}}],
                )
                template["scale"].update(minReplicas=1, **fixture["revision"]["scale"])
                template["volumes"] = [{"name": "work", "storageType": "EmptyDir"}]
                if revision.endswith("--r2"):
                    props.update(replicas=1, runningState="Running")
            desired = deepcopy(az.revisions[(name, f"{name}--r2")]["properties"]["template"])
            desired["containers"][0]["resources"].update(fixture["desired"]["resources"])
            desired["scale"].update(fixture["desired"]["scale"])
            az.apps[name]["properties"]["template"] = desired
        return az

    def snapshot(self, service: str) -> Any:
        return pdv.AppSnapshot(
            service=service, name=APPS[service], exists=True,
            revision=f"{APPS[service]}--r1", revisionsMode="Single",
            image=RESTORED_IMAGE, minReplicas=1,
        )

    def observe(self, az: FakeAz, service: str = "api") -> Any:
        with patch.object(pdv, "run_az", az):
            return pdv.read_current_observation(
                GROUP, APPS[service], subscription=SUBSCRIPTION,
                reference_revision=self.snapshot(service).revision,
            )

    def test_real_projection_passes_pending_restoration_and_rollout_predicates(self) -> None:
        az = self.world()
        before_apps, before_revisions = deepcopy(az.apps), deepcopy(az.revisions)
        for service in pdv.SERVICES:
            with self.subTest(service=service):
                snapshot = self.snapshot(service)
                observation = self.observe(az, service)
                pending = pdv._pending_cutover_problems(observation)
                restoration = pdv._restoration_problems(
                    snapshot, observation, observation.reference
                )
                slept = []
                with patch.object(pdv, "run_az", az):
                    problems, current, revision = pdv.await_rollout(
                        resource_group=GROUP, service=service, snapshot=snapshot,
                        expected_image=RESTORED_IMAGE, attempts=1, sleep=slept.append,
                    )
                    confirmed, serving = pdv.confirm_restored(
                        resource_group=GROUP, snapshot=snapshot,
                        target=observation.reference, pending_revision=snapshot.revision,
                        replaced_revision=snapshot.revision, attempts=1, sleep=slept.append,
                    )
                self.assertEqual(
                    {"pending": pending, "restoration": restoration, "rollout": problems,
                     "confirmed": confirmed},
                    {"pending": [], "restoration": [], "rollout": [], "confirmed": True},
                )
                self.assertIsNotNone(current)
                self.assertEqual(revision, f"{APPS[service]}--r2")
                self.assertTrue(confirmed)
                self.assertEqual(serving, revision)
                self.assertEqual(slept, [])
        self.assertEqual(az.apps, before_apps)
        self.assertEqual(az.revisions, before_revisions)
        self.assertFalse([call for call in az.calls if "copy" in call or "set" in call])

    def test_projection_alone_does_not_restart_an_unchanged_revision(self) -> None:
        az = self.world()
        for service, name in APPS.items():
            source = deepcopy(az.revisions[(name, f"{name}--r2")])
            source["name"] = f"{name}--r1"
            source["id"] = f"{app_id(name)}/revisions/{name}--r1"
            source["properties"]["template"]["revisionSuffix"] = "r1"
            az.revisions[(name, f"{name}--r1")] = source
            az.apps[name]["properties"].update(
                latestRevisionName=f"{name}--r1", latestReadyRevisionName=f"{name}--r1"
            )
            observation = self.observe(az, service)
            self.assertEqual(
                pdv.rollback_commands(
                    resource_group=GROUP, snapshot=self.snapshot(service), observation=observation
                ),
                [],
            )

    def test_verify_and_rollback_use_the_real_projection_comparison(self) -> None:
        az = self.world()
        expected = [
            value for service in pdv.SERVICES
            for value in ("--expect-image", f"{service}={RESTORED_IMAGE}")
        ]
        code, out = VerifyTests().verify(az=az, extra_args=["--skip-canary", *expected])
        self.assertEqual(code, 0, out)

        def projected_copy(args, **kwargs):
            response = az(args, **kwargs)
            if list(args)[:3] == ["containerapp", "revision", "copy"] and response[0] == 0:
                name = FakeAz._flag(list(args), "-n")
                template = az.apps[name]["properties"]["template"]
                template["containers"][0]["resources"]["ephemeralStorage"] = "2Gi"
                for field, default in (("cooldownPeriod", 300), ("pollingInterval", 30)):
                    if template["scale"].get(field) is None:
                        template["scale"][field] = default
            return response

        code, out, _ = RollbackTests().rollback(
            inert_az(az, projected_copy), snapshot_overrides={"proxy": {"minReplicas": 1}}
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count('"outcome":"restored"'), 3)
        self.assertEqual(len([call for call in az.calls if "copy" in call]), 3)

    def test_nullable_or_omitted_scale_matches_only_its_documented_default(self) -> None:
        for field, default in (("cooldownPeriod", 300), ("pollingInterval", 30)):
            for omitted in (False, True):
                with self.subTest(field=field, omitted=omitted):
                    az = self.world()
                    serving = az.revisions[(APPS["api"], f"{APPS['api']}--r2")]["properties"]["template"]
                    if omitted:
                        del serving["scale"][field]
                    self.assertEqual(pdv._pending_cutover_problems(self.observe(az)), [])
                    for changed in (0, default + 1):
                        az.apps[APPS["api"]]["properties"]["template"]["scale"][field] = changed
                        self.assertTrue(pdv._pending_cutover_problems(self.observe(az)))
                    az.apps[APPS["api"]]["properties"]["template"]["scale"][field] = default
                    self.assertEqual(pdv._pending_cutover_problems(self.observe(az)), [])

    def test_matching_explicit_nondefault_scale_values_remain_comparable(self) -> None:
        for field, value in (("cooldownPeriod", 0), ("cooldownPeriod", 600), ("pollingInterval", 45)):
            with self.subTest(field=field, value=value):
                az = self.world()
                app = az.apps[APPS["api"]]["properties"]["template"]
                serving = az.revisions[(APPS["api"], f"{APPS['api']}--r2")]["properties"]["template"]
                app["scale"][field] = serving["scale"][field] = value
                self.assertEqual(pdv._pending_cutover_problems(self.observe(az)), [])
                serving["scale"][field] = value + 1
                self.assertTrue(pdv._pending_cutover_problems(self.observe(az)))

    def test_writable_and_unknown_template_changes_are_not_normalized_away(self) -> None:
        changes = [
            (("containers", 0, "image"), DIGEST_B),
            (("containers", 0, "env", 0, "value"), "changed"),
            (("containers", 0, "env", 1, "secretRef"), "changed-secret"),
            (("containers", 0, "resources", "cpu"), 1),
            (("containers", 0, "resources", "memory"), "2Gi"),
            (("containers", 0, "resources", "unknownResource"), "changed"),
            (("containers", 0, "probes", 0, "httpGet", "port"), 8081),
            (("containers", 0, "probes", 0, "httpGet", "path"), "/changed"),
            (("scale", "minReplicas"), 2),
            (("scale", "maxReplicas"), 4),
            (("scale", "rules"), [{"name": "changed", "http": {"metadata": {"concurrentRequests": "7"}}}]),
            (("scale", "unknownScale"), None),
            (("volumes", 0, "storageType"), "AzureFile"),
            (("unknownTemplate",), None),
        ]
        for side in ("desired", "serving", "captured"):
            for path, value in changes:
                with self.subTest(side=side, path=path):
                    az = self.world()
                    observation = self.observe(az)
                    self.assertEqual(
                        pdv._restoration_problems(self.snapshot("api"), observation, observation.reference), []
                    )
                    if side == "desired":
                        template = az.apps[APPS["api"]]["properties"]["template"]
                    else:
                        suffix = "r2" if side == "serving" else "r1"
                        template = az.revisions[(APPS["api"], f"{APPS['api']}--{suffix}")]["properties"]["template"]
                    parent = template
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = value
                    observation = self.observe(az)
                    self.assertTrue(
                        pdv._restoration_problems(self.snapshot("api"), observation, observation.reference)
                    )
                    if side == "desired":
                        self.assertTrue(pdv._pending_cutover_problems(observation))
                        with patch.object(pdv, "run_az", az):
                            problems, _, _ = pdv.await_rollout(
                                resource_group=GROUP, service="api", snapshot=self.snapshot("api"),
                                expected_image=RESTORED_IMAGE, attempts=1,
                            )
                        self.assertTrue(problems)

    def test_scale_type_errors_refuse_even_when_python_numeric_equality_would_match(self) -> None:
        for field, default in (("cooldownPeriod", 300), ("pollingInterval", 30)):
            for invalid in (False, True, float(default), str(default), [], {}, 2 ** 31):
                with self.subTest(field=field, invalid=invalid):
                    az = self.world()
                    self.assertEqual(pdv._pending_cutover_problems(self.observe(az)), [])
                    app = az.apps[APPS["api"]]["properties"]["template"]
                    serving = az.revisions[(APPS["api"], f"{APPS['api']}--r2")]["properties"]["template"]
                    app["scale"][field] = serving["scale"][field] = invalid
                    observation = self.observe(az)
                    self.assertTrue(pdv._pending_cutover_problems(observation))
                    self.assertTrue(
                        pdv._restoration_problems(self.snapshot("api"), observation, observation.reference)
                    )
                    expected = [
                        value for service in pdv.SERVICES
                        for value in ("--expect-image", f"{service}={RESTORED_IMAGE}")
                    ]
                    code, out = VerifyTests().verify(
                        az=az, extra_args=["--skip-canary", *expected]
                    )
                    self.assertEqual(code, 3, out)
                    self.assertEqual(out.count('"event":"rollout"'), 3)

    def test_unknown_json_value_types_remain_distinct(self) -> None:
        for original, changed in ((True, 1), (300, 300.0), (False, 0)):
            with self.subTest(original=original, changed=changed):
                az = self.world()
                app = az.apps[APPS["api"]]["properties"]["template"]
                serving = az.revisions[(APPS["api"], f"{APPS['api']}--r2")]["properties"]["template"]
                app["unknownValue"] = serving["unknownValue"] = original
                self.assertEqual(pdv._pending_cutover_problems(self.observe(az)), [])
                serving["unknownValue"] = changed
                self.assertTrue(pdv._pending_cutover_problems(self.observe(az)))

    def test_read_only_ephemeral_storage_is_narrow_and_not_an_in_place_edit(self) -> None:
        az = self.world()
        app = az.apps[APPS["api"]]["properties"]["template"]
        serving = az.revisions[(APPS["api"], f"{APPS['api']}--r2")]["properties"]["template"]
        for key in ("containers", "initContainers"):
            if key == "initContainers":
                app[key] = [deepcopy(app["containers"][0])]
                serving[key] = [deepcopy(serving["containers"][0])]
            self.assertEqual(pdv._pending_cutover_problems(self.observe(az)), [])
            self.assertEqual(app[key][0]["resources"]["ephemeralStorage"], "2Gi")
            self.assertNotIn("ephemeralStorage", serving[key][0]["resources"])
            app[key][0]["resources"]["ephemeralStorage"] = {"unrecognized": "shape"}
            self.assertTrue(pdv._pending_cutover_problems(self.observe(az)))
            app[key][0]["resources"]["ephemeralStorage"] = "2Gi"
            app[key][0]["resources"]["memory"] = "2Gi"
            self.assertTrue(pdv._pending_cutover_problems(self.observe(az)))
            app[key][0]["resources"]["memory"] = "1Gi"

    def test_real_pending_identity_and_failed_provisioning_still_refuse(self) -> None:
        for field, value in (
            ("latestRevisionName", f"{APPS['api']}--pending"),
            ("provisioningState", "Updating"),
        ):
            with self.subTest(field=field):
                az = self.world()
                self.assertEqual(pdv._pending_cutover_problems(self.observe(az)), [])
                az.apps[APPS["api"]]["properties"][field] = value
                observation = self.observe(az)
                self.assertTrue(pdv._pending_cutover_problems(observation))
                self.assertTrue(
                    pdv.rollback_commands(
                        resource_group=GROUP, snapshot=self.snapshot("api"), observation=observation
                    )
                )

    def test_malformed_captured_scale_is_not_copied_and_other_apps_continue(self) -> None:
        az = self.world()
        source = az.revisions[(APPS["api"], f"{APPS['api']}--r1")]["properties"]["template"]
        source["scale"]["cooldownPeriod"] = 300.0
        code, out, _ = RollbackTests().rollback(
            az, snapshot_overrides={"proxy": {"minReplicas": 1}}
        )
        self.assertEqual(code, 4, out)
        self.assertFalse([call for call in az.calls if "copy" in call and APPS["api"] in call])
        self.assertEqual(out.count('"outcome":"restored"'), 2)


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

    def test_a_canary_preflight_problem_does_not_roll_back(self) -> None:
        """A missing grant or audience is a gate problem, not a bad release."""
        preflight = self.step("Preflight the post-deploy canary token")
        condition = str(self.step("Roll back").get("if", ""))
        self.assertEqual(preflight.get("id"), "canary_preflight")
        self.assertIn("steps.canary_preflight.outcome", condition)
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


# ---------------------------------------------------------------------------
# canary token acquisition, executed
# ---------------------------------------------------------------------------

BASH = find_bash()
PREFLIGHT_STEP = "Preflight the post-deploy canary token"
TOKEN_STEP = "Acquire the canary token for verification"
PREFLIGHT_ERROR = (
    "::error::The deploy identity could not obtain an access token for the API "
    "audience in AI4IA_ENTRA_AUDIENCE, so the post-deploy canary could not run. The "
    "API app registration needs a service principal in this tenant "
    "(scripts/provision-entra-apps.ps1 runs 'az ad sp create'), and if that app "
    "requires assignment, the deploy identity needs an app role on it. Fix the "
    "grant, or set AI4IA_DEPLOY_VERIFY_CANARY=false to ship without end-to-end proof."
)
TOKEN_ERROR = (
    "::error::Could not obtain a canary token even though the pre-deploy preflight "
    "could. That is an Entra or network problem, not a bad release, so this run "
    "stops WITHOUT rolling back. The deploy is live but unverified -- check it, "
    "then re-run the workflow."
)
EXPIRED_ASSERTION = "AADSTS700024: Client assertion is not within its valid time range."

# Each CLI stub also writes its own token to stderr on every call, so a step that
# let either CLI's stderr through would leak it. Tokens are minted per test.
AZD_TOKEN_STUB = r"""#!/usr/bin/env bash
printf 'azd %s\n' "$*" >> "$STUB_CALLS"
echo "azd stub diagnostics for $STUB_AZD_TOKEN" >&2
count=$(( $(cat "$STUB_DIR/azd-count" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$count" > "$STUB_DIR/azd-count"
if [ "$count" -le "${STUB_AZD_FAILURES:-0}" ]; then
  exit 1
fi
case "$STUB_AZD_MODE" in
  ok) printf '%s\n' "$STUB_AZD_TOKEN" ;;
  empty) ;;
  noisy) printf 'WARNING: stub notice\n%s\n' "$STUB_AZD_TOKEN" ;;
  *) exit 1 ;;
esac
"""
# The Azure CLI whose login-time assertion has expired fails the way the deploy
# runs did, unless a test says its token request works.
AZ_TOKEN_STUB = r"""#!/usr/bin/env bash
printf 'az %s\n' "$*" >> "$STUB_CALLS"
echo "az stub diagnostics for $STUB_AZ_TOKEN" >&2
if [ "$STUB_AZ_MODE" = "ok" ]; then
  printf '%s\n' "$STUB_AZ_TOKEN"
  exit 0
fi
if [ "$STUB_AZ_MODE" = "noisy" ]; then
  printf 'WARNING: stub notice\n%s\n' "$STUB_AZ_TOKEN"
  exit 0
fi
echo "ERROR: AADSTS700024: Client assertion is not within its valid time range." >&2
exit 1
"""
SLEEP_STUB = r"""sleep() {
  printf 'sleep %s\n' "$*" >> "$STUB_CALLS"
}
"""


@unittest.skipIf(BASH is None, "bash is unavailable on this machine")
class CanaryTokenAcquisitionTests(unittest.TestCase):
    """Run both API-audience token steps with azd, az and sleep stubbed.

    The Azure CLI holds the single GitHub OIDC assertion `azure/login` gave it,
    and Entra rejects that assertion about ten minutes after login. A deploy run
    failed the canary preflight 11.1 minutes after login for exactly that
    reason. azd's GitHub federated credential fetches a new assertion for every
    token, so both steps must ask azd first and use the CLI only as a fallback.
    """

    def setUp(self) -> None:
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.steps = {step.get("name"): step for step in workflow["jobs"]["deploy"]["steps"]}
        self.audience = str(uuid.uuid4())
        self.azd_token = "stub-azd-" + secrets.token_hex(16)
        self.az_token = "stub-az-" + secrets.token_hex(16)

    def script(self, name: str) -> str:
        return self.steps[name]["run"]

    def run_step(
        self,
        name: str,
        *,
        azd: str = "ok",
        az: str = "ok",
        azd_failures: int = 0,
        audience: str | None = None,
        without_audience: bool = False,
        canary: str | None = None,
        script: str | None = None,
    ) -> dict[str, Any]:
        assert BASH is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for tool, body in (("azd", AZD_TOKEN_STUB), ("az", AZ_TOKEN_STUB)):
                path = bin_dir / tool
                path.write_text(body, encoding="utf-8", newline="\n")
                path.chmod(0o755)
            # Git for Windows' bash launcher puts /usr/bin ahead of PATH, so a
            # PATH stub cannot shadow `sleep`. BASH_ENV defines it as a function
            # before the step runs, and leaves the step text unchanged.
            bash_env = root / "stub-sleep.sh"
            bash_env.write_text(SLEEP_STUB, encoding="utf-8", newline="\n")
            calls = root / "calls"
            github_env = root / "github-env"
            for path in (calls, github_env):
                path.write_text("", encoding="utf-8")
            step_file = root / "step.sh"
            step_file.write_text(
                self.script(name) if script is None else script, encoding="utf-8", newline="\n"
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"AI4IA_ENTRA_AUDIENCE", "AI4IA_DEPLOY_VERIFY_CANARY", "BASH_ENV"}
            }
            env.update(
                PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                BASH_ENV=str(bash_env),
                STUB_DIR=str(root),
                STUB_CALLS=str(calls),
                STUB_AZD_MODE=azd,
                STUB_AZ_MODE=az,
                STUB_AZD_FAILURES=str(azd_failures),
                STUB_AZD_TOKEN=self.azd_token,
                STUB_AZ_TOKEN=self.az_token,
                GITHUB_ENV=str(github_env),
            )
            if not without_audience:
                env["AI4IA_ENTRA_AUDIENCE"] = audience or self.audience
            if canary is not None:
                env["AI4IA_DEPLOY_VERIFY_CANARY"] = canary
            # The runner's default for a `run:` block without `shell:`.
            result = subprocess.run(
                [BASH, "-e", str(step_file)],
                capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60,
            )
            return {
                "code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "calls": calls.read_text(encoding="utf-8").splitlines(),
                "env": github_env.read_text(encoding="utf-8"),
            }

    def azd_call(self, audience: str | None = None) -> str:
        return f"azd auth token --scope {audience or self.audience}/.default"

    def az_call(self, audience: str | None = None) -> str:
        return (
            f"az account get-access-token --resource {audience or self.audience} "
            "--query accessToken -o tsv"
        )

    def assert_token_confined(self, run: dict[str, Any], *, masked: str | None) -> None:
        """No token on either stream, except the mask command for the one in use."""
        output = run["stdout"] + run["stderr"]
        for token in (self.azd_token, self.az_token):
            leaked = [line for line in output.splitlines() if token in line]
            expected = [f"::add-mask::{token}"] if token == masked else []
            if leaked != expected:
                raise AssertionError(f"token lines {leaked!r}, expected {expected!r}")

    def test_both_steps_ask_azd_first_for_the_cli_equivalent_scope(self) -> None:
        # az turns `--resource X` into the scope `X/.default` for either form.
        for audience in (self.audience, "api://ai4ia-api"):
            for name in (PREFLIGHT_STEP, TOKEN_STEP):
                with self.subTest(step=name, audience=audience):
                    run = self.run_step(name, audience=audience)
                    self.assertEqual(run["code"], 0, run)
                    self.assertEqual(run["calls"], [self.azd_call(audience)])
                    if name == PREFLIGHT_STEP:
                        self.assertIn("The deploy identity can obtain a token", run["stdout"])
                        self.assertEqual(run["env"], "")
                        self.assert_token_confined(run, masked=None)
                    else:
                        self.assertEqual(run["env"], f"AI4IA_DEPLOY_CANARY_TOKEN={self.azd_token}\n")
                        self.assert_token_confined(run, masked=self.azd_token)

    def test_the_cli_is_the_fallback_only_after_azd_yields_no_token(self) -> None:
        # A multi-line result is not a token: it would reach GITHUB_ENV as two
        # lines, or fail the canary and roll back a healthy release.
        for azd in ("fail", "empty", "noisy"):
            for name in (PREFLIGHT_STEP, TOKEN_STEP):
                with self.subTest(azd=azd, step=name):
                    run = self.run_step(name, azd=azd)
                    self.assertEqual(run["code"], 0, run)
                    self.assertEqual(run["calls"], [self.azd_call(), self.az_call()])
                    if name == TOKEN_STEP:
                        self.assertEqual(run["env"], f"AI4IA_DEPLOY_CANARY_TOKEN={self.az_token}\n")
                    self.assert_token_confined(run, masked=self.az_token if name == TOKEN_STEP else None)
        # Control: the same fixture never reaches the CLI while azd has a token.
        for name in (PREFLIGHT_STEP, TOKEN_STEP):
            with self.subTest(azd="ok", step=name):
                self.assertEqual(self.run_step(name)["calls"], [self.azd_call()])

    def test_an_expired_cli_assertion_no_longer_fails_the_canary_steps(self) -> None:
        # The failed deploy: every Azure CLI token request for the API audience
        # was rejected with AADSTS700024 while azd could still mint tokens.
        for name in (PREFLIGHT_STEP, TOKEN_STEP):
            with self.subTest(step=name):
                run = self.run_step(name, az="fail")
                self.assertEqual(run["code"], 0, run)
                self.assertEqual(run["calls"], [self.azd_call()])
                self.assertNotIn(EXPIRED_ASSERTION, run["stdout"] + run["stderr"])
                self.assert_token_confined(run, masked=self.azd_token if name == TOKEN_STEP else None)
                # Control: the same fixture fails once azd has no token either.
                self.assertEqual(self.run_step(name, azd="fail", az="fail")["code"], 1)
        token = self.run_step(TOKEN_STEP, az="fail")
        self.assertEqual(token["env"], f"AI4IA_DEPLOY_CANARY_TOKEN={self.azd_token}\n")

    def test_each_retry_asks_azd_again_before_the_cli(self) -> None:
        run = self.run_step(TOKEN_STEP, azd_failures=1, az="fail")
        self.assertEqual(run["code"], 0, run)
        self.assertEqual(run["calls"], [self.azd_call(), self.az_call(), "sleep 10", self.azd_call()])
        self.assertEqual(run["env"], f"AI4IA_DEPLOY_CANARY_TOKEN={self.azd_token}\n")
        self.assert_token_confined(run, masked=self.azd_token)

    def test_no_token_keeps_the_existing_errors_and_writes_nothing(self) -> None:
        # A multi-line CLI result is no more a token than a multi-line azd one.
        for az in ("fail", "noisy"):
            with self.subTest(az=az):
                preflight = self.run_step(PREFLIGHT_STEP, azd="fail", az=az)
                self.assertEqual(preflight["code"], 1, preflight)
                self.assertEqual(preflight["stdout"].splitlines(), [PREFLIGHT_ERROR])
                self.assertEqual(preflight["calls"], [self.azd_call(), self.az_call()])
                token = self.run_step(TOKEN_STEP, azd="fail", az=az)
                self.assertEqual(token["code"], 1, token)
                self.assertEqual(token["stdout"].splitlines(), [TOKEN_ERROR])
                self.assertEqual(token["calls"], [self.azd_call(), self.az_call(), "sleep 10"] * 3)
                for run in (preflight, token):
                    self.assertEqual(run["env"], "")
                    self.assertNotIn(EXPIRED_ASSERTION, run["stdout"] + run["stderr"])
                    self.assert_token_confined(run, masked=None)

    def test_the_opt_out_and_a_missing_audience_request_no_token(self) -> None:
        for name in (PREFLIGHT_STEP, TOKEN_STEP):
            with self.subTest(step=name):
                run = self.run_step(name, canary="false")
                self.assertEqual(run["code"], 0, run)
                self.assertEqual(run["calls"], [])
                self.assertEqual(run["env"], "")
        missing = self.run_step(PREFLIGHT_STEP, without_audience=True)
        self.assertEqual(missing["code"], 1, missing)
        self.assertIn("::error::AI4IA_ENTRA_AUDIENCE is empty", missing["stdout"])
        self.assertEqual(missing["calls"], [])

    def test_the_leak_check_sees_a_token_the_step_would_print(self) -> None:
        # Controls for every confinement assertion above: the same fixture, with
        # only the step text flipped to print what it currently suppresses.
        def edited(name: str, old: str, new: str) -> str:
            script = self.script(name)
            self.assertEqual(script.count(old), 1, old)
            return script.replace(old, new)

        azd_stderr = '--scope "${AI4IA_ENTRA_AUDIENCE}/.default" 2>/dev/null)"'
        cases = {
            "preflight prints its token": (
                PREFLIGHT_STEP, edited(PREFLIGHT_STEP, "if ! api_token >/dev/null; then", "if ! api_token; then"), None,
            ),
            "preflight lets azd stderr through": (
                PREFLIGHT_STEP, edited(PREFLIGHT_STEP, azd_stderr, '--scope "${AI4IA_ENTRA_AUDIENCE}/.default")"'), None,
            ),
            "token step lets azd stderr through": (
                TOKEN_STEP, edited(TOKEN_STEP, azd_stderr, '--scope "${AI4IA_ENTRA_AUDIENCE}/.default")"'), self.azd_token,
            ),
            "token step echoes its token": (
                TOKEN_STEP, self.script(TOKEN_STEP) + "\nprintf 'debug %s\\n' \"$token\"\n", self.azd_token,
            ),
            "token step never masks": (
                TOKEN_STEP, edited(TOKEN_STEP, 'echo "::add-mask::$token"\n', ""), self.azd_token,
            ),
        }
        for case, (name, script, masked) in cases.items():
            with self.subTest(case=case):
                run = self.run_step(name, script=script)
                self.assertEqual(run["code"], 0, run)
                with self.assertRaises(AssertionError):
                    self.assert_token_confined(run, masked=masked)

    def test_both_steps_share_one_acquisition_function(self) -> None:
        pattern = re.compile(r"^api_token\(\) \{\n.*?^\}\n", re.MULTILINE | re.DOTALL)
        functions = [pattern.findall(self.script(name)) for name in (PREFLIGHT_STEP, TOKEN_STEP)]
        self.assertEqual([len(found) for found in functions], [1, 1])
        (preflight,), (token,) = functions
        self.assertEqual(preflight, token)
        self.assertLess(preflight.index("azd auth token --scope"), preflight.index("az account get-access-token"))


if __name__ == "__main__":
    unittest.main()
