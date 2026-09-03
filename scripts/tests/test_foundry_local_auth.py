"""Pins identity-only auth on the Foundry accounts.

Gateway-first routing is the repo's first non-negotiable rule, but with account
keys live it is a *convention*, not a boundary: anything holding a Cognitive
Services account key can call a Foundry deployment directly and skip APIM's rate
limiting, residency policy, usage metering and priority routing. Setting
`disableLocalAuth: true` is what makes ARM enforce it.

The failure mode this guards is quiet in both directions. Flipping the default
back to `false` restores the bypass with no error anywhere, and adding a new
Foundry account module that simply omits the parameter would inherit whatever the
module default happens to be. So this asserts on the *compiled* template when one
is available, and on the source in either case.

Evidence gathered before the flip (2026-08-06), recorded here because it is what
a future reader will want when something 401s:

* APIM reaches Foundry with its managed identity -- 37 `auth: MI` entries in the
  generated gateway catalog and zero `api-key`.
* Content Understanding is the only direct Foundry data plane held by FastAPI,
  and it uses its narrow contributor role with managed identity.
* Responses-API Code Interpreter bypasses SimpleL7Proxy because its Files and
  stateful container surfaces do not fit the compatible catalog route, but it no
  longer bypasses APIM. FastAPI holds an API-scoped subscription key; APIM strips
  it and authenticates to Foundry with managed identity.
* The main API identity has no Cognitive Services OpenAI User assignment.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FOUNDRY = REPO / "infra" / "modules" / "foundry.bicep"
MAIN = REPO / "infra" / "main.bicep"
PARAMS = REPO / "infra" / "main.parameters.json"


class FoundryLocalAuthStaysDisabled(unittest.TestCase):
    def test_module_default_is_secure(self) -> None:
        text = FOUNDRY.read_text(encoding="utf-8")
        match = re.search(r"param\s+disableLocalAuth\s+bool\s*=\s*(\w+)", text)
        self.assertIsNotNone(match, "foundry.bicep no longer declares disableLocalAuth")
        assert match is not None
        self.assertEqual(
            match.group(1),
            "true",
            "foundry.bicep's disableLocalAuth default is no longer true. A module "
            "that omits the parameter would then silently provision an account "
            "reachable by key, which turns gateway-only routing back into a "
            "convention. If this is deliberate, record why in the parameter's "
            "own description -- the bypass is not visible from anywhere else.",
        )

    def test_the_account_actually_consumes_the_parameter(self) -> None:
        """A secure default is worthless if the resource stops reading it."""
        text = FOUNDRY.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"disableLocalAuth:\s*disableLocalAuth",
            "the Foundry account resource no longer assigns disableLocalAuth from "
            "the parameter, so the parameter is inert.",
        )

    def test_main_passes_it_explicitly(self) -> None:
        """Relying on the module default would make the posture invisible in main."""
        text = MAIN.read_text(encoding="utf-8")
        self.assertRegex(text, r"param\s+foundryDisableLocalAuth\s+bool\s*=\s*true")
        self.assertRegex(text, r"disableLocalAuth:\s*foundryDisableLocalAuth")

    def test_the_azd_variable_defaults_to_true(self) -> None:
        params = json.loads(PARAMS.read_text(encoding="utf-8"))
        value = params["parameters"]["foundryDisableLocalAuth"]["value"]
        self.assertEqual(
            value,
            "${AI4IA_FOUNDRY_DISABLE_LOCAL_AUTH=true}",
            "the escape hatch must default to the secure value. azd resolves an "
            "unset variable to the default, so a `=false` default would disable "
            "the boundary for every deployment that never sets the variable.",
        )

    def test_compiled_template_disables_local_auth_on_every_account(self) -> None:
        """Catches an account added by a module that never passes the parameter."""
        compiled = REPO / "infra" / "main.json"
        if not compiled.exists():
            self.skipTest("no prebuilt infra/main.json; the source checks cover this")
        text = compiled.read_text(encoding="utf-8")
        enabled = re.findall(r'"disableLocalAuth":\s*(true|false)', text)
        self.assertNotIn(
            "false",
            enabled,
            "the compiled template provisions at least one Cognitive Services "
            "account with local (key) auth still enabled.",
        )


class TheEvidenceForTheFlipStaysTrue(unittest.TestCase):
    """Pin the remaining direct data plane and the APIM Code Interpreter route."""

    def test_content_understanding_defaults_to_managed_identity(self) -> None:
        config = (REPO / "app" / "api" / "src" / "ai4ia_api" / "config.py").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"cu_auth_mode:\s*GatewayAuthMode\s*=\s*GatewayAuthMode\.(\w+)",
            config,
        )
        self.assertIsNotNone(match, "cu_auth_mode is no longer declared")
        assert match is not None
        self.assertEqual(match.group(1), "bearer")

    def test_code_interpreter_is_wired_to_a_scoped_apim_key(self) -> None:
        main = MAIN.read_text(encoding="utf-8")
        api = (REPO / "infra" / "modules" / "api.bicep").read_text(encoding="utf-8")
        direct = main.split("var nativeFoundryPrincipalIds =", 1)[1].split("\n", 1)[0]
        self.assertIn("[]", direct)
        self.assertIn(
            "codeInterpreterBaseUrl: gateway.outputs.codeInterpreterGatewayUrl",
            main,
        )
        self.assertIn(
            "codeInterpreterApiKey: gateway.outputs.codeInterpreterGatewayKey",
            main,
        )
        self.assertIn("codeInterpreterAuthMode: 'api_key'", main)
        self.assertIn("name: 'AI4IA_CODE_INTERPRETER_API_KEY'", api)

    def test_deploy_fails_closed_on_incrementally_retained_direct_roles(self) -> None:
        workflow = (
            REPO / ".github" / "workflows" / "deploy.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Verify legacy API inference roles are revoked", workflow)
        self.assertIn("id-api-${AZURE_ENV_NAME}", workflow)
        self.assertIn("5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", workflow)
        self.assertIn("a97b65f3-24c7-4388-baec-2e87135dc908", workflow)
        self.assertIn('--scope "$account_scope"', workflow)
        self.assertIn("Stale assignment ID:", workflow)
        block = workflow.split(
            "- name: Verify legacy API inference roles are revoked",
            1,
        )[1].split("# ---------------------------------------------------------------------", 1)[0]
        self.assertNotIn("< <(", block)
        self.assertIn('if ! account_scopes="$(az cognitiveservices account list', block)
        self.assertIn('if ! assignment_ids="$(az role assignment list', block)
        self.assertNotIn(
            'az role assignment delete --ids "${stale[@]}"',
            workflow,
            "deployment must not silently revoke live RBAC without human review",
        )


if __name__ == "__main__":
    unittest.main()
