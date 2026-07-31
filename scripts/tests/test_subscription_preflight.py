"""Unit tests for the two subscription preflight scripts.

scripts/check-resource-providers.py and scripts/check-model-availability.py both
talk to Azure, so CI cannot run them end to end. What CI *can* pin is the pure
logic underneath -- the part that decides "this deployment will fail" -- plus the
two real Azure quirks that made the first drafts of these scripts wrong:

* ARM returns resource provider namespaces with inconsistent casing
  (`microsoft.insights` lowercase alongside `Microsoft.OperationalInsights`), so
  an exact-match lookup reports a registered provider as missing.
* The required provider set is derived from infra/**/*.bicep rather than
  hand-listed, so adding a resource type cannot silently skip the preflight.

Both scripts are loaded from their paths (they are scripts, not importable
modules) and no test in this file touches the network.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROVIDERS = _load("check_resource_providers", "check-resource-providers.py")
AVAILABILITY = _load("check_model_availability", "check-model-availability.py")


class ProviderDerivationTests(unittest.TestCase):
    def test_required_set_is_derived_from_the_templates(self) -> None:
        required = PROVIDERS.required_namespaces()
        # Spot-check namespaces that only appear inside a module, so a naive
        # "scan main.bicep only" implementation would miss them.
        for namespace in (
            "Microsoft.CognitiveServices",
            "Microsoft.DocumentDB",
            "Microsoft.App",
            "Microsoft.ApiManagement",
            "Microsoft.DBforPostgreSQL",
        ):
            self.assertIn(namespace, required, f"{namespace} should be derived from infra/")
        for namespace, sources in required.items():
            self.assertTrue(sources, f"{namespace} recorded with no source file")

    def test_universally_registered_namespaces_are_excluded(self) -> None:
        """Reporting these as missing would be noise: ARM always has them."""
        required = PROVIDERS.required_namespaces()
        for namespace in ("Microsoft.Authorization", "Microsoft.Resources"):
            self.assertNotIn(namespace, required)

    def test_implicit_additions_stay_empty_without_justification(self) -> None:
        """Guard the derive-don't-hardcode property.

        An unjustified entry here fails provisioning on a guess. If one is ever
        added it must carry evidence, which means updating this test too.
        """
        self.assertEqual(PROVIDERS.IMPLICIT, {})


class ProviderStateLookupTests(unittest.TestCase):
    def test_namespace_lookup_is_case_insensitive(self) -> None:
        """ARM really does return `microsoft.insights` lowercased.

        A case-sensitive lookup reported it as NotRegistered while
        `az provider show` said Registered -- a false alarm that trains
        operators to ignore the preflight.
        """
        states = {
            "microsoft.insights": "Registered",
            "microsoft.operationalinsights": "Registered",
        }
        self.assertEqual(PROVIDERS.state_of(states, "Microsoft.Insights"), "Registered")
        self.assertEqual(
            PROVIDERS.state_of(states, "Microsoft.OperationalInsights"), "Registered"
        )

    def test_unknown_namespace_reports_notfound(self) -> None:
        self.assertEqual(PROVIDERS.state_of({}, "Microsoft.Nope"), "NotFound")


class CatalogRequirementTests(unittest.TestCase):
    CATALOG = {
        "catalog": [
            {
                "name": "gpt-4.1-mini",
                "deployments": [
                    {"region": "eastus2", "sku": "GlobalStandard", "capacity": 50, "version": "2025-04-14"},
                    {"region": "westus", "sku": "Standard", "capacity": 1, "version": "2025-04-14"},
                ],
            },
            {
                "name": "o3-pro",
                "deployments": [
                    {"region": "eastus2", "sku": "GlobalStandard", "capacity": 50, "version": "2025-06-10"}
                ],
            },
        ]
    }

    def test_requirements_group_by_region(self) -> None:
        by_region = AVAILABILITY.catalog_requirements(self.CATALOG)
        self.assertEqual(sorted(by_region), ["eastus2", "westus"])
        self.assertEqual(len(by_region["eastus2"]), 2)
        self.assertEqual(by_region["westus"][0]["sku"], "Standard")

    def test_real_catalog_covers_every_declared_region(self) -> None:
        import json

        models = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        by_region = AVAILABILITY.catalog_requirements(models)
        self.assertEqual(sorted(by_region), sorted(models["regions"]))


class EvaluateTests(unittest.TestCase):
    INDEX = {
        "gpt-5.4-mini": {"GlobalStandard": {"2026-03-17"}, "Standard": {"2026-03-17"}},
        "cohere-rerank-v4.0-pro": {"GlobalStandard": {"1"}},
    }
    # Every model in INDEX is deployable unless a test says otherwise.
    LIFECYCLE = {
        "gpt-5.4-mini": {"2026-03-17": "GenerallyAvailable"},
        "cohere-rerank-v4.0-pro": {"1": "GenerallyAvailable"},
    }

    def test_missing_model_is_blocking(self) -> None:
        errors, warnings = AVAILABILITY.evaluate(
            [{"name": "o3-pro", "sku": "GlobalStandard", "version": "2025-06-10"}],
            self.INDEX,
            self.LIFECYCLE,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("not offered", errors[0])
        self.assertEqual(warnings, [])

    def test_missing_sku_is_blocking_and_lists_alternatives(self) -> None:
        errors, _ = AVAILABILITY.evaluate(
            [{"name": "gpt-5.4-mini", "sku": "DataZoneStandard", "version": "2026-03-17"}],
            self.INDEX,
            self.LIFECYCLE,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("GlobalStandard", errors[0])

    def test_version_drift_warns_but_does_not_block(self) -> None:
        """Azure commonly rolls a retired pinned version forward.

        Failing the standup over that would be worse than proceeding.
        """
        errors, warnings = AVAILABILITY.evaluate(
            [{"name": "gpt-5.4-mini", "sku": "GlobalStandard", "version": "2024-07-18"}],
            self.INDEX,
            self.LIFECYCLE,
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-03-17", warnings[0])

    def test_model_name_comparison_is_case_insensitive(self) -> None:
        """The catalog and the API disagree on case for partner models."""
        errors, warnings = AVAILABILITY.evaluate(
            [{"name": "Cohere-rerank-v4.0-pro", "sku": "GlobalStandard", "version": "1"}],
            self.INDEX,
            self.LIFECYCLE,
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_fully_satisfied_catalog_is_silent(self) -> None:
        errors, warnings = AVAILABILITY.evaluate(
            [{"name": "gpt-5.4-mini", "sku": "GlobalStandard", "version": "2026-03-17"}],
            self.INDEX,
            self.LIFECYCLE,
        )
        self.assertEqual((errors, warnings), ([], []))


class LifecycleTests(unittest.TestCase):
    """A deprecating model is offered, quotaed, and undeployable.

    This is the failure mode that took down the second cutover attempt: the
    preflight reported "78/78 available and within quota" and `azd provision`
    then died with `ServiceModelDeprecating` after the Foundry accounts, gateway
    and data tier had already been built.
    """

    INDEX = {"gpt-4.1-mini": {"GlobalStandard": {"2025-04-14"}}}

    def test_deprecating_model_is_blocking(self) -> None:
        errors, _ = AVAILABILITY.evaluate(
            [{"name": "gpt-4.1-mini", "sku": "GlobalStandard", "version": "2025-04-14"}],
            self.INDEX,
            {"gpt-4.1-mini": {"2025-04-14": "Deprecating"}},
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("Deprecating", errors[0])
        self.assertIn("ServiceModelDeprecating", errors[0])

    def test_deprecated_model_is_blocking(self) -> None:
        errors, _ = AVAILABILITY.evaluate(
            [{"name": "gpt-4.1-mini", "sku": "GlobalStandard", "version": "2025-04-14"}],
            self.INDEX,
            {"gpt-4.1-mini": {"2025-04-14": "Deprecated"}},
        )
        self.assertEqual(len(errors), 1)

    def test_error_names_a_deployable_version_when_one_exists(self) -> None:
        """Repinning is the cheap fix; removal is the expensive one."""
        index = {"gpt-4.1-mini": {"GlobalStandard": {"2025-04-14", "2026-01-01"}}}
        errors, _ = AVAILABILITY.evaluate(
            [{"name": "gpt-4.1-mini", "sku": "GlobalStandard", "version": "2025-04-14"}],
            index,
            {
                "gpt-4.1-mini": {
                    "2025-04-14": "Deprecating",
                    "2026-01-01": "GenerallyAvailable",
                }
            },
        )
        self.assertIn("repin to version 2026-01-01", errors[0])

    def test_error_says_remove_when_no_version_is_deployable(self) -> None:
        """The whole GPT-4.1 family went deprecating at once; there was nothing to repin to."""
        errors, _ = AVAILABILITY.evaluate(
            [{"name": "gpt-4.1-mini", "sku": "GlobalStandard", "version": "2025-04-14"}],
            self.INDEX,
            {"gpt-4.1-mini": {"2025-04-14": "Deprecating"}},
        )
        self.assertIn("no deployable version", errors[0])

    def test_generally_available_and_preview_are_silent(self) -> None:
        for status in ("GenerallyAvailable", "Preview"):
            with self.subTest(status=status):
                errors, warnings = AVAILABILITY.evaluate(
                    [{"name": "gpt-4.1-mini", "sku": "GlobalStandard", "version": "2025-04-14"}],
                    self.INDEX,
                    {"gpt-4.1-mini": {"2025-04-14": status}},
                )
                self.assertEqual((errors, warnings), ([], []))

    def test_absent_lifecycle_is_silent(self) -> None:
        """Older API versions do not report the field; warning on every model would be noise."""
        errors, warnings = AVAILABILITY.evaluate(
            [{"name": "gpt-4.1-mini", "sku": "GlobalStandard", "version": "2025-04-14"}],
            self.INDEX,
            {},
        )
        self.assertEqual((errors, warnings), ([], []))

    def test_unrecognized_lifecycle_warns_rather_than_blocking(self) -> None:
        """A new status string should surface, not strand a standup."""
        errors, warnings = AVAILABILITY.evaluate(
            [{"name": "gpt-4.1-mini", "sku": "GlobalStandard", "version": "2025-04-14"}],
            self.INDEX,
            {"gpt-4.1-mini": {"2025-04-14": "RetiringSoon"}},
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("RetiringSoon", warnings[0])

    def test_status_comparison_is_case_insensitive(self) -> None:
        errors, _ = AVAILABILITY.evaluate(
            [{"name": "gpt-4.1-mini", "sku": "GlobalStandard", "version": "2025-04-14"}],
            self.INDEX,
            {"gpt-4.1-mini": {"2025-04-14": "DEPRECATING"}},
        )
        self.assertEqual(len(errors), 1)

    def test_index_lifecycle_keys_by_casefolded_name_and_version(self) -> None:
        index = AVAILABILITY.index_lifecycle(
            [
                {"model": {"name": "GPT-4.1-Mini", "version": "2025-04-14",
                           "lifecycleStatus": "Deprecating"}},
                {"model": {"name": "gpt-4.1-mini", "version": "2026-01-01",
                           "lifecycleStatus": "GenerallyAvailable"}},
                {"model": {"name": "no-status", "version": "1"}},
                {"model": {}},
            ]
        )
        self.assertEqual(
            index["gpt-4.1-mini"],
            {"2025-04-14": "Deprecating", "2026-01-01": "GenerallyAvailable"},
        )
        self.assertNotIn("no-status", index)

    def test_real_catalog_pins_no_deprecating_version(self) -> None:
        """Regression guard for the two models that broke the cutover.

        This is a static check against the shipped catalog, so it runs in CI
        without Azure credentials.
        """
        import json

        models = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        names = {m["name"] for m in models["catalog"]}
        self.assertNotIn("gpt-4.1-mini", names)
        self.assertNotIn("o4-mini", names)


class IndexOfferedTests(unittest.TestCase):
    def test_index_collapses_skus_and_versions(self) -> None:
        raw = [
            {"model": {"name": "gpt-5.4-mini", "version": "2026-03-17",
                       "skus": [{"name": "GlobalStandard"}, {"name": "Standard"}]}},
            {"model": {"name": "gpt-5.4-mini", "version": "2024-07-18",
                       "skus": [{"name": "GlobalStandard"}]}},
        ]
        index = AVAILABILITY.index_offered(raw)
        self.assertEqual(
            index["gpt-5.4-mini"]["GlobalStandard"], {"2026-03-17", "2024-07-18"}
        )
        self.assertEqual(index["gpt-5.4-mini"]["Standard"], {"2026-03-17"})

    def test_entries_without_a_name_are_ignored(self) -> None:
        self.assertEqual(AVAILABILITY.index_offered([{"model": {}}, {}]), {})


class QuotaIndexTests(unittest.TestCase):
    """Quota counters are not named after the models they meter.

    Every counter name below was read from a live
    `az cognitiveservices usage list` against the target subscription. The
    catalog spelling is on the left, Azure's counter on the right -- reconciling
    them is the whole job of this index, and getting it wrong silently degrades
    the check into "no counter found" for every model.
    """

    # (catalog name, sku, Azure counter)
    REAL_PAIRS = [
        ("gpt-5.6-sol", "GlobalStandard", "OpenAI.GlobalStandard.gpt-5.6-sol"),
        # Publisher prefix differs for partner models.
        ("Mistral-Large-3", "GlobalStandard", "AIServices.GlobalStandard.Mistral-Large-3"),
        # Punctuation dropped.
        ("gpt-4.1-mini", "GlobalStandard", "OpenAI.GlobalStandard.gpt4.1-mini"),
        # Recased and de-hyphenated entirely.
        ("model-router", "GlobalStandard", "OpenAI.GlobalStandard.ModelRouter"),
        ("o3-deep-research", "GlobalStandard", "OpenAI.GlobalStandard.o3-DeepResearch"),
        # Partner counters drop a ".0" version suffix.
        ("Cohere-rerank-v4.0-pro", "GlobalStandard", "AIServices.GlobalStandard.Cohere-Rerank-V4-Pro"),
        ("embed-v-4-0", "GlobalStandard", "AIServices.GlobalStandard.Embed-V-4-0"),
    ]

    def test_real_counter_names_are_reconciled(self) -> None:
        for name, sku, counter in self.REAL_PAIRS:
            index = AVAILABILITY.index_quota(
                [{"name": {"value": counter}, "limit": 1000, "currentValue": 0}]
            )
            errors, warnings = AVAILABILITY.evaluate_quota(
                [{"name": name, "sku": sku, "capacity": 10}], index
            )
            self.assertEqual(errors, [], f"{name} should fit under {counter}")
            self.assertEqual(warnings, [], f"{name} did not match counter {counter}")

    def test_sku_must_also_match(self) -> None:
        """A GlobalStandard counter must not satisfy a Standard deployment."""
        index = AVAILABILITY.index_quota(
            [{"name": {"value": "OpenAI.GlobalStandard.whisper"}, "limit": 100, "currentValue": 0}]
        )
        _errors, warnings = AVAILABILITY.evaluate_quota(
            [{"name": "whisper", "sku": "Standard", "capacity": 3}], index
        )
        self.assertEqual(len(warnings), 1)

    def test_malformed_counters_are_skipped(self) -> None:
        raw = [{"name": {"value": "NoDotsHere"}}, {"name": {}}, {}]
        self.assertEqual(AVAILABILITY.index_quota(raw), {})


class QuotaEvaluationTests(unittest.TestCase):
    """Availability and quota fail independently.

    A brand-new subscription is offered nearly every model but ships small
    default quotas, so `evaluate` can pass while the provision still dies on
    `InsufficientQuota`. These pin the arithmetic that catches that.
    """

    @staticmethod
    def _index(limit: float, current: float = 0) -> dict:
        return AVAILABILITY.index_quota(
            [
                {
                    "name": {"value": "OpenAI.GlobalStandard.gpt-image-2"},
                    "limit": limit,
                    "currentValue": current,
                }
            ]
        )

    def test_capacity_over_limit_is_blocking(self) -> None:
        errors, _ = AVAILABILITY.evaluate_quota(
            [{"name": "gpt-image-2", "sku": "GlobalStandard", "capacity": 10}], self._index(2)
        )
        self.assertEqual(len(errors), 1)
        self.assertTrue("needs 10" in errors[0], errors[0])

    def test_capacity_exactly_at_the_limit_is_allowed(self) -> None:
        """16 catalog deployments sit exactly at their cap; that deploys fine."""
        errors, warnings = AVAILABILITY.evaluate_quota(
            [{"name": "gpt-image-2", "sku": "GlobalStandard", "capacity": 2}], self._index(2)
        )
        self.assertEqual((errors, warnings), ([], []))

    def test_quota_already_consumed_is_subtracted(self) -> None:
        """Comparing against `limit` instead of the remainder is the easy bug.

        It passes on an empty subscription and only fails on a redeploy into a
        populated one -- the case least likely to be tested.
        """
        errors, _ = AVAILABILITY.evaluate_quota(
            [{"name": "gpt-image-2", "sku": "GlobalStandard", "capacity": 2}],
            self._index(limit=2, current=2),
        )
        self.assertEqual(len(errors), 1)
        self.assertTrue("0 left" in errors[0], errors[0])

    def test_repeated_deployments_share_one_counter(self) -> None:
        """Two deployments that each fit can still exceed the shared quota.

        Quota is per subscription+region+model+SKU, not per deployment, so
        checking each in isolation would pass this and then fail in ARM.
        """
        errors, _ = AVAILABILITY.evaluate_quota(
            [
                {"name": "gpt-image-2", "sku": "GlobalStandard", "capacity": 2},
                {"name": "gpt-image-2", "sku": "GlobalStandard", "capacity": 2},
            ],
            self._index(3),
        )
        self.assertEqual(len(errors), 1)
        self.assertTrue("needs 4" in errors[0], errors[0])

    def test_unmatched_counter_warns_rather_than_blocks(self) -> None:
        """Absence is ambiguous: no grant, or a spelling we failed to reconcile.

        Blocking on it would strand a standup over a naming quirk, which is the
        likelier cause -- so it must degrade to a warning.
        """
        errors, warnings = AVAILABILITY.evaluate_quota(
            [{"name": "brand-new-model", "sku": "GlobalStandard", "capacity": 1}], self._index(10)
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertTrue("unverified" in warnings[0], warnings[0])

    def test_catalog_capacity_is_carried_through(self) -> None:
        """`catalog_requirements` must surface capacity or the check is inert."""
        import json

        models = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        by_region = AVAILABILITY.catalog_requirements(models)
        for region, items in by_region.items():
            for item in items:
                self.assertIn("capacity", item, f"{region}/{item['name']} lost its capacity")
                self.assertGreater(
                    int(item["capacity"]), 0, f"{region}/{item['name']} has no capacity"
                )


if __name__ == "__main__":
    unittest.main()
