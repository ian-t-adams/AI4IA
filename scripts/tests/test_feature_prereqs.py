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
