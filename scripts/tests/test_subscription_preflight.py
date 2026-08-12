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
import json
import subprocess
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

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
            # Declared only in infra/modules/search.bicep. Replaced
            # Microsoft.DBforPostgreSQL here when PostgreSQL was retired on
            # 2026-08-06; the point of the list is coverage of module-only
            # providers, so it needs at least one that main.bicep never names.
            "Microsoft.Search",
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
        "naming": {
            "subscriptionToken": "slurmfactory",
            "pattern": "{model}-{subscriptionToken}-{region}-{skuShort}",
            "skuShort": {"GlobalStandard": "glbl", "Standard": "std"},
        },
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
        self.assertEqual(
            by_region["eastus2"][0]["deploymentName"],
            "gpt-4.1-mini-slurmfactory-eastus2-glbl",
        )

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


class ExistingStateAwareLifecycleTests(unittest.TestCase):
    """The same deprecating desired record must fail greenfield and pass exact reconcile."""

    DESIRED = {
        "deploymentName": "gpt-4.1-mini-slurmfactory-eastus2-glbl",
        "name": "gpt-4.1-mini",
        "format": "OpenAI",
        "sku": "GlobalStandard",
        "version": "2025-04-14",
        "capacity": 50,
        "versionUpgradeOption": "NoAutoUpgrade",
        "region": "eastus2",
    }
    OFFERED = {"gpt-4.1-mini": {"GlobalStandard": {"2025-04-14"}}}

    @classmethod
    def exact_existing(cls) -> dict[tuple[str, str], dict[str, object]]:
        return {
            ("eastus2", cls.DESIRED["deploymentName"].casefold()): {
                "accountName": "mf-aiforia-prod-eastus2-suffix",
                "deploymentName": cls.DESIRED["deploymentName"],
                "region": "eastus2",
                "modelName": cls.DESIRED["name"],
                "format": cls.DESIRED["format"],
                "version": cls.DESIRED["version"],
                "sku": cls.DESIRED["sku"],
                "capacity": cls.DESIRED["capacity"],
                "versionUpgradeOption": "NoAutoUpgrade",
                "provisioningState": "Succeeded",
            }
        }

    def test_greenfield_absent_deprecating_deployment_is_blocking(self) -> None:
        errors, warnings = AVAILABILITY.evaluate(
            [self.DESIRED],
            self.OFFERED,
            {"gpt-4.1-mini": {"2025-04-14": "Deprecating"}},
            {},
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("deployment is absent", errors[0])
        self.assertIn("would be created or changed", errors[0])

    def test_exact_existing_deprecating_routine_reconcile_passes_with_warning(self) -> None:
        errors, warnings = AVAILABILITY.evaluate(
            [self.DESIRED],
            self.OFFERED,
            {"gpt-4.1-mini": {"2025-04-14": "Deprecating"}},
            self.exact_existing(),
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("exact existing deployment", warnings[0])
        self.assertIn("Routine reconcile is allowed", warnings[0])
        self.assertIn("migrate before retirement", warnings[0])

    def test_exact_existing_deprecated_record_uses_same_routine_exception(self) -> None:
        errors, warnings = AVAILABILITY.evaluate(
            [self.DESIRED],
            self.OFFERED,
            {"gpt-4.1-mini": {"2025-04-14": "Deprecated"}},
            self.exact_existing(),
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_retired_model_absent_from_offer_list_exact_existing_warns_and_passes(self) -> None:
        errors, warnings = AVAILABILITY.evaluate(
            [self.DESIRED],
            {},
            {},
            self.exact_existing(),
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("no longer listed as offered", warnings[0])
        self.assertIn("routine reconcile", warnings[0])

    def test_retired_sku_absent_from_offer_list_exact_existing_warns_and_passes(self) -> None:
        errors, warnings = AVAILABILITY.evaluate(
            [self.DESIRED],
            {"gpt-4.1-mini": {"Standard": {"2025-04-14"}}},
            {},
            self.exact_existing(),
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("SKU GlobalStandard is no longer listed", warnings[0])
        self.assertIn("routine reconcile", warnings[0])

    def test_same_name_with_version_drift_remains_blocking(self) -> None:
        existing = self.exact_existing()
        existing[("eastus2", self.DESIRED["deploymentName"].casefold())][
            "version"
        ] = "2024-01-01"
        errors, _ = AVAILABILITY.evaluate(
            [self.DESIRED],
            self.OFFERED,
            {"gpt-4.1-mini": {"2025-04-14": "Deprecating"}},
            existing,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("version is 2024-01-01, wants 2025-04-14", errors[0])


class ExistingDeploymentInventoryTests(unittest.TestCase):
    MODELS = {
        "naming": {
            "foundryToken": "aiforia",
            "subscriptionToken": "slurmfactory",
            "pattern": "{model}-{subscriptionToken}-{region}-{skuShort}",
            "skuShort": {"GlobalStandard": "glbl"},
        },
        "regions": {"eastus2": {"primary": True}},
        "catalog": [],
    }

    def test_no_target_context_is_explicit_greenfield_addition_mode(self) -> None:
        inventory, warnings = AVAILABILITY.existing_deployment_inventory(
            self.MODELS,
            resource_group=None,
            environment_name=None,
        )
        self.assertEqual(inventory, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("greenfield/addition mode", warnings[0])

    def test_inventory_reads_only_the_exact_target_account(self) -> None:
        deployment_name = "gpt-4.1-mini-slurmfactory-eastus2-glbl"

        def fake_az(*args: str) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("group", "exists"):
                payload = "true"
            elif args[:3] == ("cognitiveservices", "account", "list"):
                payload = (
                    '[{"name":"mf-aiforia-prod-eastus2-suffix",'
                    '"kind":"AIServices","location":"eastus2"},'
                    '{"name":"unrelated","kind":"AIServices","location":"eastus2"}]'
                )
            elif args[:4] == (
                "cognitiveservices",
                "account",
                "deployment",
                "list",
            ):
                self.assertIn("mf-aiforia-prod-eastus2-suffix", args)
                payload = json.dumps(
                    [
                        {
                            "name": deployment_name,
                            "sku": {"name": "GlobalStandard", "capacity": 50},
                            "properties": {
                                "model": {
                                    "name": "gpt-4.1-mini",
                                    "format": "OpenAI",
                                    "version": "2025-04-14",
                                },
                                "versionUpgradeOption": "NoAutoUpgrade",
                                "provisioningState": "Succeeded",
                            },
                        }
                    ]
                )
            else:
                self.fail(f"unexpected Azure CLI call: {args}")
            return subprocess.CompletedProcess(["az", *args], 0, payload, "")

        with patch.object(AVAILABILITY, "_az", side_effect=fake_az):
            inventory, warnings = AVAILABILITY.existing_deployment_inventory(
                self.MODELS,
                resource_group="rg-ai4ia-prod",
                environment_name="prod",
            )
        self.assertEqual(warnings, [])
        self.assertIn(("eastus2", deployment_name.casefold()), inventory)
        self.assertEqual(len(inventory), 1)


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
        # Hosted-on-Azure partner counters add a suffix that is not part of the
        # deployment model id.
        (
            "claude-opus-4-8",
            "DataZoneStandard",
            "AIServices.DataZoneStandard.claude-opus-4-8.Azure",
        ),
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

    def test_capacity_exactly_at_the_limit_warns_but_does_not_block(self) -> None:
        """16 catalog deployments sit exactly at their cap, and they do deploy.

        Zero headroom is fragile rather than impossible: it succeeds on a clean
        run and fails on a retry that still holds the previous reservation,
        which is exactly what happened to the three MAI-Image models.
        """
        errors, warnings = AVAILABILITY.evaluate_quota(
            [{"name": "gpt-image-2", "sku": "GlobalStandard", "capacity": 2}], self._index(2)
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("entire 2 limit", warnings[0])

    def test_consumed_quota_warns_rather_than_blocking(self) -> None:
        """`currentValue` is not the number ARM enforces against.

        `OpenAI.GlobalStandard.text-embedding-3-large` reports 1000/1000 in a
        region with no such deployment at all, while the 120-capacity deployment
        in the *other* region succeeded against an identically saturated
        counter. Blocking on this reading would strand a standup on a model that
        demonstrably deploys, so it is reported and not enforced.
        """
        errors, warnings = AVAILABILITY.evaluate_quota(
            [{"name": "gpt-image-2", "sku": "GlobalStandard", "capacity": 1}],
            self._index(limit=2, current=2),
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("already used", warnings[0])

    def test_over_limit_still_blocks_even_with_quota_free(self) -> None:
        """`capacity > limit` is unarguable: no retry and no release can fix it."""
        errors, _ = AVAILABILITY.evaluate_quota(
            [{"name": "gpt-image-2", "sku": "GlobalStandard", "capacity": 3}],
            self._index(limit=2, current=0),
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("subscription limit is 2", errors[0])

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

    def test_reduced_quota_exact_existing_warns_and_passes(self) -> None:
        desired = {
            "deploymentName": "gpt-image-2-existing",
            "name": "gpt-image-2",
            "format": "OpenAI",
            "sku": "GlobalStandard",
            "version": "1",
            "capacity": 10,
            "versionUpgradeOption": "NoAutoUpgrade",
            "region": "eastus2",
        }
        existing = {
            ("eastus2", "gpt-image-2-existing"): {
                "accountName": "mf-example",
                "deploymentName": "gpt-image-2-existing",
                "region": "eastus2",
                "modelName": "gpt-image-2",
                "format": "OpenAI",
                "version": "1",
                "sku": "GlobalStandard",
                "capacity": 10,
                "versionUpgradeOption": "NoAutoUpgrade",
                "provisioningState": "Succeeded",
            }
        }
        errors, warnings = AVAILABILITY.evaluate_quota(
            [desired], self._index(2), existing
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("routine reconcile adds no capacity", warnings[0])

    def test_mixed_exact_existing_and_new_group_still_blocks_total_capacity(self) -> None:
        existing_item = {
            "deploymentName": "gpt-image-2-existing",
            "name": "gpt-image-2",
            "format": "OpenAI",
            "sku": "GlobalStandard",
            "version": "1",
            "capacity": 2,
            "versionUpgradeOption": "NoAutoUpgrade",
            "region": "eastus2",
        }
        new_item = dict(
            existing_item,
            deploymentName="gpt-image-2-new",
            capacity=2,
        )
        existing = {
            ("eastus2", "gpt-image-2-existing"): {
                "accountName": "mf-example",
                "deploymentName": "gpt-image-2-existing",
                "region": "eastus2",
                "modelName": "gpt-image-2",
                "format": "OpenAI",
                "version": "1",
                "sku": "GlobalStandard",
                "capacity": 2,
                "versionUpgradeOption": "NoAutoUpgrade",
                "provisioningState": "Succeeded",
            }
        }
        errors, _ = AVAILABILITY.evaluate_quota(
            [existing_item, new_item], self._index(3), existing
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("needs 4", errors[0])

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


class SharedQuotaTests(unittest.TestCase):
    """Model quota is subscription-wide, so per-region checks can all pass and still fail.

    Measured live in `sub-planetexpress-slurmfactory`:
    `AIServices.GlobalStandard.MAI-Image-2.5` reads `used=2 / limit=2` in
    **eastus2**, a region that does not offer MAI-Image at all -- the
    subscription's only deployment sits in westus. The usage API replicates one
    subscription-wide aggregate into every region's response.
    """

    @staticmethod
    def _index(counter: str, limit: float) -> dict:
        return AVAILABILITY.index_quota(
            [{"name": {"value": counter}, "limit": limit, "currentValue": 0}]
        )

    def test_two_regions_that_each_fit_can_still_overcommit(self) -> None:
        """The MAI-Image failure exactly: 2 <= 2 twice, but 4 > 2 in total."""
        by_region = {
            "westus": [{"name": "MAI-Image-2.5", "sku": "GlobalStandard", "capacity": 2}],
            "swedencentral": [{"name": "MAI-Image-2.5", "sku": "GlobalStandard", "capacity": 2}],
        }
        errors, _ = AVAILABILITY.evaluate_shared_quota(
            by_region, self._index("AIServices.GlobalStandard.MAI-Image-2.5", 2)
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("4 total across 2 regions", errors[0])

    def test_openai_overcommit_warns_instead_of_blocking(self) -> None:
        """gpt-image-1.5 holds a full 9-capacity deployment in *two* regions.

        18 units against a limit of 9, and both succeeded -- so OpenAI-published
        models are enforced per region. Blocking here would reject a shape that
        demonstrably works.
        """
        by_region = {
            "eastus2": [{"name": "gpt-image-1.5", "sku": "GlobalStandard", "capacity": 9}],
            "swedencentral": [{"name": "gpt-image-1.5", "sku": "GlobalStandard", "capacity": 9}],
        }
        errors, warnings = AVAILABILITY.evaluate_shared_quota(
            by_region, self._index("OpenAI.GlobalStandard.gpt-image-1.5", 9)
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("18 total across 2 regions", warnings[0])

    def test_single_region_models_are_left_to_the_per_region_check(self) -> None:
        """Otherwise every over-limit model would be reported twice."""
        by_region = {
            "westus": [{"name": "MAI-Image-2.5", "sku": "GlobalStandard", "capacity": 99}]
        }
        errors, warnings = AVAILABILITY.evaluate_shared_quota(
            by_region, self._index("AIServices.GlobalStandard.MAI-Image-2.5", 2)
        )
        self.assertEqual((errors, warnings), ([], []))

    def test_multi_region_within_the_shared_limit_is_silent(self) -> None:
        """Spanning regions is fine as long as the total fits."""
        by_region = {
            "eastus2": [{"name": "gpt-image-1-mini", "sku": "GlobalStandard", "capacity": 2}],
            "swedencentral": [{"name": "gpt-image-1-mini", "sku": "GlobalStandard", "capacity": 2}],
        }
        errors, warnings = AVAILABILITY.evaluate_shared_quota(
            by_region, self._index("OpenAI.GlobalStandard.gpt-image-1-mini", 4)
        )
        self.assertEqual((errors, warnings), ([], []))

    def test_catalog_has_no_subscription_wide_overcommit(self) -> None:
        """Credential-free guard: no non-OpenAI model may span regions past its cap.

        This is the regression that cost a deploy run. It needs no Azure access
        because the limits it would have to violate are the ones we hit, so the
        cheap invariant is simply that MAI-Image stays single-region.
        """
        import json

        models = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        for model in models["catalog"]:
            if not model["name"].startswith("MAI-Image"):
                continue
            regions = {d["region"] for d in model.get("deployments", [])}
            self.assertEqual(
                len(regions),
                1,
                f"{model['name']} spans {sorted(regions)}; MAI-Image quota is a "
                "subscription-wide 2, so a second region deterministically fails "
                "with InsufficientQuota. Request a quota increase first.",
            )

    def test_shared_overage_exact_existing_warns_and_passes(self) -> None:
        def desired(region: str) -> dict:
            return {
                "deploymentName": f"mai-{region}",
                "name": "MAI-Image-2.5",
                "format": "OpenAI",
                "sku": "GlobalStandard",
                "version": "1",
                "capacity": 2,
                "versionUpgradeOption": "NoAutoUpgrade",
                "region": region,
            }

        east = desired("eastus2")
        sweden = desired("swedencentral")
        existing = {}
        for item in (east, sweden):
            existing[(item["region"], item["deploymentName"])] = {
                "accountName": f"mf-{item['region']}",
                "deploymentName": item["deploymentName"],
                "region": item["region"],
                "modelName": item["name"],
                "format": item["format"],
                "version": item["version"],
                "sku": item["sku"],
                "capacity": item["capacity"],
                "versionUpgradeOption": item["versionUpgradeOption"],
                "provisioningState": "Succeeded",
            }
        errors, warnings = AVAILABILITY.evaluate_shared_quota(
            {"eastus2": [east], "swedencentral": [sweden]},
            self._index("AIServices.GlobalStandard.MAI-Image-2.5", 2),
            existing,
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("routine reconcile adds no shared capacity", warnings[0])

    def test_shared_mixed_existing_and_new_still_blocks(self) -> None:
        east = {
            "deploymentName": "mai-eastus2",
            "name": "MAI-Image-2.5",
            "format": "OpenAI",
            "sku": "GlobalStandard",
            "version": "1",
            "capacity": 2,
            "versionUpgradeOption": "NoAutoUpgrade",
            "region": "eastus2",
        }
        sweden = dict(east, deploymentName="mai-sweden", region="swedencentral")
        existing = {
            ("eastus2", "mai-eastus2"): {
                "accountName": "mf-eastus2",
                "deploymentName": "mai-eastus2",
                "region": "eastus2",
                "modelName": "MAI-Image-2.5",
                "format": "OpenAI",
                "version": "1",
                "sku": "GlobalStandard",
                "capacity": 2,
                "versionUpgradeOption": "NoAutoUpgrade",
                "provisioningState": "Succeeded",
            }
        }
        errors, _ = AVAILABILITY.evaluate_shared_quota(
            {"eastus2": [east], "swedencentral": [sweden]},
            self._index("AIServices.GlobalStandard.MAI-Image-2.5", 2),
            existing,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("4 total across 2 regions", errors[0])


class ModelPreflightCredentialTests(unittest.TestCase):
    def test_missing_azure_cli_credentials_fail_with_a_precise_remedy(self) -> None:
        result = subprocess.CompletedProcess(
            ["az", "account", "show"],
            1,
            stdout="",
            stderr="Please run az login to setup account.",
        )
        with patch.object(AVAILABILITY, "_az", return_value=result):
            with self.assertRaises(SystemExit) as raised:
                AVAILABILITY.active_subscription("target-subscription")
        message = str(raised.exception)
        self.assertIn("requires Azure CLI credentials", message)
        self.assertIn("az login", message)
        self.assertIn("az account set", message)

    def test_wrong_subscription_fails_before_model_queries(self) -> None:
        result = subprocess.CompletedProcess(
            ["az", "account", "show"],
            0,
            stdout='{"id":"wrong-subscription","name":"Wrong","tenantId":"tenant"}',
            stderr="",
        )
        with patch.object(AVAILABILITY, "_az", return_value=result):
            with self.assertRaises(SystemExit) as raised:
                AVAILABILITY.active_subscription("target-subscription")
        self.assertIn("but azd will provision target-subscription", str(raised.exception))

    def test_matching_subscription_is_accepted(self) -> None:
        result = subprocess.CompletedProcess(
            ["az", "account", "show"],
            0,
            stdout='{"id":"TARGET-SUBSCRIPTION","name":"Target","tenantId":"tenant"}',
            stderr="",
        )
        with patch.object(AVAILABILITY, "_az", return_value=result):
            account = AVAILABILITY.active_subscription("target-subscription")
        self.assertEqual(account["id"], "TARGET-SUBSCRIPTION")


class ModelPreflightLifecycleWiringTests(unittest.TestCase):
    def test_azd_runs_full_model_preflight_in_both_preprovision_hooks(self) -> None:
        azure_yaml = (ROOT / "azure.yaml").read_text(encoding="utf-8")
        preprovision = azure_yaml.split("  preprovision:", 1)[1].split(
            "  postprovision:", 1
        )[0]
        self.assertEqual(
            preprovision.count("scripts/check-model-availability.py"),
            2,
            "Windows and POSIX azd provisions must both check model availability/quota",
        )
        self.assertIn("    windows:", preprovision)
        self.assertIn("    posix:", preprovision)
        self.assertNotIn("--skip-quota", preprovision)
        self.assertIn("continueOnError: false", preprovision)

    def test_preflight_script_changes_trigger_the_provisioning_workflow(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('- "scripts/check-model-availability.py"', workflow)

    def test_main_passes_existing_inventory_into_lifecycle_evaluation(self) -> None:
        source = (ROOT / "scripts" / "check-model-availability.py").read_text(
            encoding="utf-8"
        )
        compact = " ".join(source.split())
        self.assertIn(
            "required, index, index_lifecycle(offered), existing_deployments",
            compact,
        )


class ProviderPreflightRunsBeforeProvisionTests(unittest.TestCase):
    """The preflight is only useful if the deploy runs it, and runs it FIRST.

    Being registered is not something the template can assert for itself: an
    unregistered provider fails ~10 minutes into `azd provision` with
    `MissingSubscriptionRegistration`, after real resources exist. For a long
    time this script was only ever a manual "step 0" in the runbook, which
    protects a greenfield standup but not the routine path where a new provider
    arrives with a feature — enabling durable workflows added
    `Microsoft.DurableTask` to the derived set and it was NotRegistered live,
    because a flag-gated module never submits its resource type while the flag is
    off, so ARM had no occasion to register it.

    Ordering is the part worth pinning. A step moved after `azd provision` still
    reads as present in review and in the workflow file while doing nothing for
    the deploy it was meant to protect — the same "configured but inert" shape
    that has bitten this repo repeatedly.
    """

    def _deploy_steps(self) -> list[dict]:
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        )
        return workflow["jobs"]["deploy"]["steps"]

    def test_deploy_runs_the_provider_preflight_before_provisioning(self) -> None:
        steps = self._deploy_steps()
        preflight = [
            i
            for i, step in enumerate(steps)
            if "check-resource-providers.py" in str(step.get("run", ""))
        ]
        provision = [
            i for i, step in enumerate(steps) if "azd provision" in str(step.get("run", ""))
        ]
        self.assertEqual(
            len(preflight),
            1,
            "deploy.yml must run scripts/check-resource-providers.py exactly once; "
            "without it an unregistered provider fails partway through a provision "
            "instead of in seconds with the namespace to register.",
        )
        self.assertTrue(provision, "deploy.yml no longer runs `azd provision`")
        self.assertLess(
            preflight[0],
            min(provision),
            "the resource-provider preflight must run BEFORE `azd provision`; after "
            "it, the deployment has already failed on the thing it checks.",
        )

    def test_the_preflight_registers_rather_than_only_reporting(self) -> None:
        """Report-only would fail the change window over a one-line fix.

        Registration is idempotent, subscription-scoped, non-destructive, and
        already granted by the deploy identity's Contributor role, so self-healing
        is strictly better here than stopping. `--register` also waits for the
        state to become Registered, which report-only cannot do.
        """
        runs = [
            str(step.get("run", ""))
            for step in self._deploy_steps()
            if "check-resource-providers.py" in str(step.get("run", ""))
        ]
        self.assertTrue(runs, "no resource-provider preflight step found in deploy.yml")
        self.assertIn(
            "--register",
            runs[0],
            "the deploy preflight should pass --register so a newly added provider "
            "self-heals instead of failing the run.",
        )


if __name__ == "__main__":
    unittest.main()
