"""Keeps the wildcard `Cognitive Services User` role off the Foundry accounts.

Audit finding **P1-4** is "gateway-only routing is convention, not IAM". Half of
it closed when `disableLocalAuth` went `true`, so an account key can no longer
bypass APIM. The other half is that an app identity holds *direct* Foundry
data-plane roles, so code running in the API container can call Foundry without
the gateway.

The obvious remediation -- move the Responses-API Code Interpreter into its own
workload and drop `Cognitive Services OpenAI User` from the api identity -- was
measured and **does not work**, which is why this guard exists rather than a
second Container App:

* `Cognitive Services User` (`a97b65f3-24c7-4388-baec-2e87135dc908`) has exactly
  one dataAction: `Microsoft.CognitiveServices/*`.
* That wildcard is a strict **superset** of every action in
  `Cognitive Services OpenAI User`, including
  `accounts/OpenAI/deployments/chat/completions/action` and
  `accounts/OpenAI/responses/*`.
* Content Understanding is enabled in production and authenticates with a
  `cognitiveservices.azure.com` token, so the broad role could not simply be
  deleted.

So while `Cognitive Services User` is assigned, removing the OpenAI role
accomplishes nothing at all -- the identity keeps full inference access through
the wildcard. Narrowing Content Understanding to
`Cognitive Services Content Understanding Contributor`
(`59a2dba3-6303-4fd8-9a2e-8cbb4bdda972`, dataAction
`Microsoft.CognitiveServices/accounts/MultiModalIntelligence/*`) is the
*prerequisite* that makes the OpenAI grant the only inference path, and therefore
makes removing it mean something.

This test pins that ordering so it cannot be undone by someone reading the
original, wrong, plan.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FOUNDRY = REPO / "infra" / "modules" / "foundry.bicep"

# The wildcard role. Matched by GUID, because the display name is not what Bicep
# carries and a rename upstream must not silently disarm this guard.
COGNITIVE_SERVICES_USER = "a97b65f3-24c7-4388-baec-2e87135dc908"
CONTENT_UNDERSTANDING_CONTRIBUTOR = "59a2dba3-6303-4fd8-9a2e-8cbb4bdda972"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"


def _bicep() -> str:
    return FOUNDRY.read_text(encoding="utf-8")


class WildcardCognitiveRoleStaysUnassigned(unittest.TestCase):
    def test_cognitive_services_user_is_not_granted_to_app_identities(self) -> None:
        self.assertNotIn(
            COGNITIVE_SERVICES_USER,
            _bicep().split("// Content Understanding, scoped")[0]
            + _bicep().split("makes removing it mean")[-1],
            "infra/modules/foundry.bicep grants `Cognitive Services User` "
            f"({COGNITIVE_SERVICES_USER}) again. Its only dataAction is the "
            "wildcard `Microsoft.CognitiveServices/*`, which re-opens direct "
            "chat-completions and responses access for every identity that "
            "holds it -- and silently makes any other P1-4 work pointless. "
            "Content Understanding needs "
            "`Cognitive Services Content Understanding Contributor` instead.",
        )

    def test_only_apim_may_hold_the_wildcard_role(self) -> None:
        """APIM is the gateway, so breadth there is not a bypass -- but it is the
        only defensible holder, and a third grant must not appear unnoticed.

        `gateway.bicep` grants it to the APIM system identity because APIM proxies
        more than one data plane (OpenAI inference, realtime, and Speech Voice
        Live). An app identity holding it is a gateway bypass; APIM holding it is
        the gateway doing its job. Narrowing APIM to
        `Cognitive Services Speech User` + `Cognitive Services OpenAI User` is a
        real follow-up, but it is a change to the live Voice Live path and is
        recorded as residual rather than done blind.
        """
        holders = sorted(
            p.name
            for p in (REPO / "infra").rglob("*.bicep")
            if re.search(rf"^\s*var\s+\w+\s*=\s*'{COGNITIVE_SERVICES_USER}'", p.read_text(encoding="utf-8"), re.M)
        )
        self.assertEqual(
            holders,
            ["gateway.bicep"],
            "the wildcard `Cognitive Services User` role is assigned by "
            f"{holders}. Only gateway.bicep (APIM, the gateway itself) may hold "
            "it. Any other module granting it hands an app identity full "
            "Cognitive Services data-plane access, which is exactly the "
            "gateway bypass P1-4 exists to close.",
        )

    def test_content_understanding_is_granted_narrowly(self) -> None:
        text = _bicep()
        self.assertIn(
            CONTENT_UNDERSTANDING_CONTRIBUTOR,
            text,
            "the narrow Content Understanding role is gone; document ingest "
            "authenticates with a cognitiveservices.azure.com token and will "
            "401 without it.",
        )
        self.assertRegex(
            text,
            r"resource contentUnderstandingAssignments[^\n]*roleAssignments",
            "the Content Understanding role id is present but no assignment "
            "resource uses it, so the grant is not actually created.",
        )

    def test_the_openai_grant_is_still_the_only_inference_path(self) -> None:
        """If this fails, inference RBAC changed shape and P1-4 needs re-reading."""
        text = _bicep()
        self.assertIn(OPENAI_USER, text)
        ids = set(re.findall(r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'", text))
        unexpected = ids - {
            OPENAI_USER,
            CONTENT_UNDERSTANDING_CONTRIBUTOR,
            "53ca6127-db72-4b80-b1b0-d745d6d5456d",  # Foundry User (project-scoped, opt-in)
        }
        self.assertEqual(
            unexpected,
            set(),
            "foundry.bicep references role definition ids this guard does not "
            f"know about: {sorted(unexpected)}. Every data-plane grant on a "
            "Foundry account is a potential gateway bypass -- add it here with "
            "its dataActions justified, or remove it.",
        )


if __name__ == "__main__":
    unittest.main()
