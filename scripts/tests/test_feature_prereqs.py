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

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-feature-prereqs.py"
REAL_PARAMETERS = ROOT / "infra" / "main.parameters.json"

# Minimum environment for a production / new-tenant standup. The committed
# parameters file reads these through ${VAR=default} placeholders.
PROD_ENV = {
    "AI4IA_APP_ENVIRONMENT": "prod",
    "AI4IA_AUTH_PROVIDER": "entra",
    "AI4IA_ENTRA_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "AI4IA_ENTRA_AUDIENCE": "api://ai4ia-api",
    "AI4IA_ENTRA_WEB_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    "AI4IA_OWNER": "ai4ia-operations",
    "AI4IA_APIM_PUBLISHER_EMAIL": "ai4ia-ops@contoso.com",
}


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_feature_prereqs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


@contextmanager
def _environment(**values: str):
    """Run with *values* set and every other AI4IA_* placeholder var cleared.

    The validator resolves ${VAR=default} placeholders from os.environ, so a
    stray AI4IA_* variable inherited from the developer's shell would otherwise
    change what these tests actually assert.
    """
    removed = {k: v for k, v in os.environ.items() if k.startswith(("AI4IA_", "AZURE_"))}
    with patch.dict(os.environ, values, clear=False):
        for key in removed:
            if key not in values:
                del os.environ[key]
        yield


def _run(parameters_file: Path) -> tuple[int, str, str]:
    """Run the validator against *parameters_file*; return (exit code, stdout, stderr)."""
    out, err = StringIO(), StringIO()
    with patch.object(VALIDATOR, "PARAMETERS_FILE", parameters_file):
        with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            code = VALIDATOR.main()
    return code, out.getvalue(), err.getvalue()


def _write_parameters(tmpdir: str, overrides: dict[str, Any]) -> Path:
    raw = json.loads(REAL_PARAMETERS.read_text(encoding="utf-8"))
    for name, value in overrides.items():
        raw["parameters"][name] = {"value": value}
    path = Path(tmpdir) / "main.parameters.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class CommittedParametersTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
