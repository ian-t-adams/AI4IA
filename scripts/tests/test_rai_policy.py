"""Pins the annotate-only Responsible AI posture across every model deployment.

This posture exists under an approved Azure guardrails-modification exception:
content filters stay *enabled* (so annotations are still emitted and logged) but
never *block*. The approval is evidenced by the deployed policy itself — Azure's
control plane refuses a RAI policy that disables blocking on the abuse filters
without one, and `ai4ia-annotate-only` is live with all 12 filters non-blocking
against a `Microsoft.DefaultV2` base that has only 1. See
`docs/rai-decision-record.md` for the accountable owner and the verification.
It is a governance commitment, not a preference, and the failure
mode is silent — a deployment that quietly reverts to the blocking default still
provisions cleanly and only shows up as unexplained refusals in production.

Three things have to hold together for the posture to be real, and each is
checked below:

1. Every filter in the policy has ``blocking: false``. The four harm categories
   are obvious; the trap is that ``Microsoft.DefaultV2`` ships Jailbreak /
   Prompt Shield and Protected Material Text/Code with blocking **ON**, so
   omitting them from the override list silently leaves them blocking.
2. Every model deployment references the policy. ``models.bicep`` accepts an
   empty name and maps it to ``null``, which falls back to the account default
   (blocking) — so the caller must always pass a real name.
3. The name actually flows: foundry outputs it and main.bicep wires that output
   into the models module.

Stdlib only, and text-based on purpose: compiling ARM would need the Bicep CLI,
and these are structural invariants of the source, not of the compiled output.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FOUNDRY = REPO / "infra" / "modules" / "foundry.bicep"
MODELS = REPO / "infra" / "modules" / "models.bicep"
MAIN = REPO / "infra" / "main.bicep"

# Optional DefaultV2 filters that are blocking by default. Enumerated explicitly
# rather than derived, so adding a model family cannot quietly shrink the set.
BLOCKING_BY_DEFAULT = ("jailbreak", "protected_material_text", "protected_material_code")
HARM_CATEGORIES = ("hate", "sexual", "selfharm", "violence")


def _policy_filters() -> list[str]:
    text = FOUNDRY.read_text(encoding="utf-8")
    match = re.search(
        r"resource annotateOnlyRaiPolicy .*?contentFilters:\s*\[(.*?)\n\s*\]",
        text,
        re.DOTALL,
    )
    assert match, "annotateOnlyRaiPolicy contentFilters block not found in foundry.bicep"
    return [line.strip() for line in match.group(1).splitlines() if line.strip().startswith("{")]


class AnnotateOnlyRaiPolicyTests(unittest.TestCase):
    def test_every_content_filter_is_non_blocking(self):
        for entry in _policy_filters():
            self.assertIn(
                "blocking: false",
                entry,
                f"content filter is not annotate-only, it will block requests: {entry}",
            )

    def test_every_content_filter_stays_enabled(self):
        """Annotate-only means *labeled but allowed*. Disabling a filter would
        drop the annotation too, losing the observability the exception assumes."""
        for entry in _policy_filters():
            self.assertIn("enabled: true", entry, f"content filter is disabled, not annotating: {entry}")

    def test_defaultv2_blocking_filters_are_all_overridden(self):
        entries = " ".join(_policy_filters())
        for name in BLOCKING_BY_DEFAULT:
            self.assertIn(
                f"name: '{name}'",
                entries,
                f"'{name}' blocks by default in Microsoft.DefaultV2 and is not overridden, "
                "so it would still block despite the annotate-only policy",
            )

    def test_harm_categories_are_covered_on_both_prompt_and_completion(self):
        entries = _policy_filters()
        for name in HARM_CATEGORIES:
            for source in ("Prompt", "Completion"):
                self.assertTrue(
                    any(f"name: '{name}'" in e and f"source: '{source}'" in e for e in entries),
                    f"harm category '{name}' is not overridden on {source}",
                )

    def test_indirect_attack_detection_is_enabled_but_non_blocking(self):
        entries = _policy_filters()
        self.assertTrue(
            any(
                "name: 'indirect_attack'" in entry
                and "source: 'Prompt'" in entry
                and "enabled: true" in entry
                and "blocking: false" in entry
                for entry in entries
            ),
            "indirect-attack assessment is not enabled in annotate-only mode",
        )

    def test_base_policy_is_defaultv2(self):
        self.assertIn(
            "basePolicyName: 'Microsoft.DefaultV2'",
            FOUNDRY.read_text(encoding="utf-8"),
            "annotate-only overrides are written against DefaultV2's filter set",
        )


class RaiPolicyIsAppliedToEveryDeploymentTests(unittest.TestCase):
    def test_model_deployments_set_the_rai_policy(self):
        self.assertIn(
            "raiPolicyName:",
            MODELS.read_text(encoding="utf-8"),
            "models.bicep no longer sets raiPolicyName; deployments would inherit "
            "the account default, which blocks",
        )

    def test_deployment_loop_has_no_per_model_rai_exemption(self):
        """One shared loop, one policy. A conditional here would let a model opt
        out of the posture without any signal at deploy time."""
        text = MODELS.read_text(encoding="utf-8")
        match = re.search(r"raiPolicyName:\s*(.+)", text)
        assert match
        expression = match.group(1).strip()
        self.assertEqual(
            expression,
            "empty(raiPolicyName) ? null : raiPolicyName",
            "raiPolicyName assignment changed; verify it still applies uniformly "
            "to every deployment in the loop",
        )

    def test_foundry_exports_the_policy_name(self):
        self.assertIn(
            "output raiPolicyName string = annotateOnlyRaiPolicy.name",
            FOUNDRY.read_text(encoding="utf-8"),
            "foundry.bicep must export the policy name for main.bicep to wire through",
        )

    def test_main_wires_the_foundry_output_into_the_models_module(self):
        """The empty-string default in models.bicep means a missing wire is not a
        compile error — it silently reverts every deployment to blocking."""
        self.assertIn(
            "raiPolicyName: foundry[i].outputs.raiPolicyName",
            MAIN.read_text(encoding="utf-8"),
            "main.bicep must pass the foundry RAI policy name to the models module; "
            "without it models.bicep defaults to '' -> null -> account default (blocking)",
        )


class AnthropicMarketplaceAttestationTests(unittest.TestCase):
    def test_claude_deployments_use_the_required_preview_api(self):
        self.assertIn(
            "accounts/deployments@2025-10-01-preview",
            MODELS.read_text(encoding="utf-8"),
        )

    def test_provider_data_is_scoped_only_to_anthropic(self):
        text = MODELS.read_text(encoding="utf-8")
        self.assertIn("d.format == 'Anthropic' ? {", text)
        for field in ("organizationName", "countryCode", "industry"):
            self.assertIn(f"{field}:", text)
        self.assertIn("} : {})", text)

    def test_main_wires_explicit_attestation_parameters(self):
        text = MAIN.read_text(encoding="utf-8")
        for name in (
            "claudeOrganizationName",
            "claudeCountryCode",
            "claudeIndustry",
        ):
            self.assertIn(f"{name}: {name}", text)


if __name__ == "__main__":
    unittest.main()
