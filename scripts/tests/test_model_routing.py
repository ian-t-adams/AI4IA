"""The router subset reaches ARM intent and fails closed on incomplete evidence."""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from scripts._model_routing import offered_routing_models, routing_configuration, routing_matches
from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
AVAILABILITY = load_script("routing_availability", ROOT / "scripts" / "check-model-availability.py")


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        self.router = next(row for row in self.catalog["catalog"] if row["name"] == "model-router")

    def test_three_tiers_reach_every_router_requirement_without_capacity_changes(self):
        expected = {
            "mode": "balanced",
            "models": [
                {"format": "OpenAI", "name": "gpt-6-astra", "version": "2026-09-03"},
                {"format": "OpenAI", "name": "gpt-6-sol", "version": "2026-09-22"},
                {"format": "OpenAI", "name": "gpt-6-luna", "version": "2026-09-22"},
            ],
        }
        self.assertEqual(routing_configuration(self.router, self.catalog), expected)
        requirements = AVAILABILITY.catalog_requirements(self.catalog, capacity_profile="maximum")
        rows = [row for region in requirements.values() for row in region if row["name"] == "model-router"]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["routing"] == expected for row in rows))
        self.assertEqual([row["capacity"] for row in rows], [950, 300, 50, 300])

    def test_invalid_or_unavailable_subset_never_becomes_unrestricted(self):
        for changed in (
            None, {}, {"mode": "balanced", "models": []},
            {"mode": "unknown", "models": self.router["routing"]["models"]},
            {"mode": "balanced", "models": [self.router["routing"]["models"][0]] * 2},
            {"mode": "balanced", "models": [{"format": "OpenAI", "name": "unknown", "version": "1"}]},
            {"mode": "balanced", "models": [{"format": "OpenAI", "name": "gpt-6-sol", "version": "wrong"}]},
        ):
            with self.subTest(routing=changed), self.assertRaises(ValueError):
                routing_configuration({**self.router, "routing": changed}, self.catalog)
        for field, value in (("runtimeEnabled", False), ("format", "Anthropic")):
            catalog = deepcopy(self.catalog)
            next(row for row in catalog["catalog"] if row["name"] == "gpt-6-sol")[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                routing_configuration(self.router, catalog)
        self.assertIsNotNone(routing_configuration(self.router, self.catalog))

    def test_missing_extra_or_wrong_model_readback_is_real_deployment_drift(self):
        row = AVAILABILITY.catalog_requirements(self.catalog)["eastus2"]
        desired = next(item for item in row if item["name"] == "model-router")
        actual = {
            "deploymentName": desired["deploymentName"], "modelName": desired["name"],
            "format": desired["format"], "version": desired["version"], "sku": desired["sku"],
            "capacity": desired["capacity"], "versionUpgradeOption": "NoAutoUpgrade",
            "provisioningState": "Succeeded", "routing": deepcopy(desired["routing"]),
        }
        key = ("eastus2", desired["deploymentName"])
        self.assertEqual(AVAILABILITY.existing_deployment_drift(desired, {key: actual}), [])
        for routing in (None, {}, {**desired["routing"], "models": desired["routing"]["models"][:2]}):
            with self.subTest(routing=routing):
                actual["routing"] = routing
                self.assertIn("routing mode or exact model subset differs",
                              AVAILABILITY.existing_deployment_drift(desired, {key: actual}))
        actual["routing"] = deepcopy(desired["routing"])
        actual["routing"]["models"].reverse()
        self.assertTrue(routing_matches(actual["routing"], desired["routing"]))
        self.assertEqual(AVAILABILITY.existing_deployment_drift(desired, {key: actual}), [])

    def test_provider_offerings_must_cover_the_exact_versions(self):
        routing = self.router["routing"]
        good = {"capabilities": {"models": json.dumps(routing["models"])}}
        self.assertTrue(offered_routing_models(good, routing))
        for payload in ("not-json", "{}", "[]", json.dumps(routing["models"][:2])):
            with self.subTest(payload=payload):
                self.assertFalse(offered_routing_models({"capabilities": {"models": payload}}, routing))
        self.assertFalse(offered_routing_models({}, routing))
        self.assertTrue(offered_routing_models(good, routing))
