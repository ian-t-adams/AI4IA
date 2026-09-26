from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from xml.etree import ElementTree

import jsonschema

from scripts.tests._loader import load_script

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "scripts" / "gen-voice-provider-catalog.py"
SOURCE = REPO_ROOT / "infra" / "voice-providers.json"
SCHEMA = REPO_ROOT / "infra" / "voice-providers.schema.json"
POLICY = REPO_ROOT / "infra" / "policies" / "speech-voice-live.xml"
PHOTO_AVATAR_POLICY = REPO_ROOT / "infra" / "policies" / "photo-avatars.xml"

EXPECTED_MODELS = (
    ("gpt-realtime", "native_audio", "openai", "gpt-4o-transcribe"),
    ("gpt-realtime-mini", "native_audio", "openai", "gpt-4o-transcribe"),
    ("gpt-4.1", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-4.1-mini", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5-mini", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5.1", "azure_speech_chain", "azure_speech", "azure-speech"),
)


class VoiceProviderCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = json.loads(SOURCE.read_text(encoding="utf-8"))
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.gen = load_script("gen_voice_provider_catalog", GEN)

    @property
    def speech(self) -> dict:
        return self.raw["providers"][1]

    def assert_schema_rejects(self, mutated: dict) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(mutated, self.schema)

    def assert_generator_rejects(self, mutated: dict) -> None:
        with self.assertRaises(SystemExit):
            self.gen.build_catalog(mutated)

    def test_schema_accepts_the_authoritative_catalog(self) -> None:
        jsonschema.validate(self.raw, self.schema)

    def test_generator_projects_the_expected_provider_contracts(self) -> None:
        catalog = self.gen.build_catalog(self.raw)
        self.assertEqual(catalog["defaultProviderId"], "azure_openai")
        self.assertEqual(
            [provider["id"] for provider in catalog["providers"]],
            ["azure_openai", "speech_voice_live"],
        )

        azure_openai = catalog["providers"][0]
        self.assertEqual(azure_openai["selectionMode"], "deployment_catalog")
        self.assertEqual(
            azure_openai["modelCatalogRef"]["sourceJson"], "infra/models.json"
        )
        self.assertEqual(
            azure_openai["modelCatalogRef"]["defaultModelId"], "gpt-realtime"
        )
        self.assertEqual(
            azure_openai["capabilities"]["voices"]["options"],
            [
                "alloy",
                "ash",
                "ballad",
                "coral",
                "echo",
                "sage",
                "shimmer",
                "verse",
                "marin",
                "cedar",
            ],
        )
        self.assertFalse(azure_openai["capabilities"]["customVoice"]["enabled"])

        speech = catalog["providers"][1]
        self.assertEqual(speech["selectionMode"], "managed_model_catalog")
        self.assertEqual(speech["defaultManagedModelId"], "gpt-realtime")
        self.assertNotIn("managedModel", speech)
        self.assertNotIn("inputTranscription", speech["sessionDefaults"])
        self.assertNotIn("inputTranscription", speech["capabilities"])
        self.assertEqual(
            tuple(
                (
                    model["id"],
                    model["profile"],
                    model["inputTranscription"]["provider"],
                    model["inputTranscription"]["model"],
                )
                for model in speech["managedModels"]
            ),
            EXPECTED_MODELS,
        )
        for model in speech["managedModels"]:
            self.assertEqual(model["apiVersion"], "2026-04-10")
            self.assertEqual(model["initialRegion"], "eastus2")
            self.assertEqual(model["audioFormat"], "pcm16")
            self.assertEqual(model["sampleRateHz"], 24000)
            self.assertTrue(model["displayName"])
            self.assertTrue(model["description"])
        self.assertEqual(
            speech["capabilities"]["voices"]["options"][0],
            "en-US-Ava:DragonHDLatestNeural",
        )
        self.assertFalse(speech["capabilities"]["customVoice"]["allowPersonalVoice"])

    def test_schema_rejects_singular_model_and_custom_selectors(self) -> None:
        mutations = {}
        singular = copy.deepcopy(self.raw)
        singular["providers"][1]["managedModel"] = singular["providers"][1][
            "managedModels"
        ][0]
        mutations["singular managedModel"] = singular

        for field, value in (
            ("deploymentName", "custom-deployment"),
            ("endpointId", "custom-endpoint"),
            ("customEndpoint", "https://example.invalid"),
            ("agentId", "agent"),
            ("projectId", "project"),
            ("bringYourOwnModel", True),
        ):
            mutated = copy.deepcopy(self.raw)
            mutated["providers"][1]["managedModels"][0][field] = value
            mutations[field] = mutated

        for label, mutated in mutations.items():
            with self.subTest(label=label):
                self.assert_schema_rejects(mutated)
                self.assert_generator_rejects(mutated)

    def test_schema_and_generator_reject_invalid_model_contracts(self) -> None:
        mutations = {}

        invalid_pair = copy.deepcopy(self.raw)
        invalid_pair["providers"][1]["managedModels"][0]["inputTranscription"] = {
            "provider": "azure_speech",
            "model": "azure-speech",
        }
        mutations["profile transcription pair"] = invalid_pair

        invalid_id = copy.deepcopy(self.raw)
        invalid_id["providers"][1]["managedModels"][0]["id"] = "arbitrary-model"
        mutations["arbitrary id"] = invalid_id

        invalid_order = copy.deepcopy(self.raw)
        models = invalid_order["providers"][1]["managedModels"]
        models[0], models[1] = models[1], models[0]
        mutations["order"] = invalid_order

        duplicate = copy.deepcopy(self.raw)
        duplicate["providers"][1]["managedModels"][1] = copy.deepcopy(
            duplicate["providers"][1]["managedModels"][0]
        )
        mutations["duplicate"] = duplicate

        preview = copy.deepcopy(self.raw)
        preview["providers"][1]["managedModels"][0][
            "apiVersion"
        ] = "2026-04-10-preview"
        mutations["preview api version"] = preview

        missing_default = copy.deepcopy(self.raw)
        missing_default["providers"][1][
            "defaultManagedModelId"
        ] = "arbitrary-model"
        mutations["default membership"] = missing_default

        for label, mutated in mutations.items():
            with self.subTest(label=label):
                self.assert_schema_rejects(mutated)
                self.assert_generator_rejects(mutated)

    def test_generator_rejects_custom_voice(self) -> None:
        mutated = copy.deepcopy(self.raw)
        mutated["providers"][1]["capabilities"]["customVoice"]["enabled"] = True
        self.assert_schema_rejects(mutated)
        self.assert_generator_rejects(mutated)

    def test_generated_policy_is_current_and_catalog_driven(self) -> None:
        catalog = self.gen.build_catalog(self.raw)
        expected = self.gen.render_speech_voice_live_policy(catalog)
        actual = POLICY.read_text(encoding="utf-8")
        self.assertEqual(actual, expected)

        root = ElementTree.fromstring(actual)
        inbound = root.find("./inbound")
        assert inbound is not None
        when = inbound.find("./choose/when")
        assert when is not None
        condition = when.attrib["condition"]
        quoted_models = tuple(
            re.findall(r'"([^"]+)"\.Equals\(model, StringComparison\.Ordinal\)', condition)
        )
        self.assertEqual(quoted_models, tuple(model[0] for model in EXPECTED_MODELS))
        self.assertIn("String.IsNullOrWhiteSpace", condition)
        self.assertEqual(
            when.find("./return-response/set-status").attrib["code"],
            "400",
        )
        self.assertIsNone(when.find("./return-response/set-body"))

        query = {
            element.attrib["name"]: element
            for element in inbound.findall("./set-query-parameter")
        }
        model_override = (query["model"].findtext("value") or "").strip()
        self.assertIn("String.IsNullOrWhiteSpace", model_override)
        self.assertIn('? "gpt-realtime" :', model_override)
        self.assertEqual(query["api-version"].findtext("value"), "2026-04-10")
        for name in (
            "deployment",
            "subscription-key",
            "api-key",
            "agent_id",
            "project_id",
        ):
            self.assertEqual(query[name].attrib["exists-action"], "delete")

        stripped_headers = {
            header.attrib["name"]
            for header in inbound.findall("./set-header")
            if header.attrib.get("exists-action") == "delete"
        }
        self.assertEqual(
            stripped_headers,
            {
                "Ocp-Apim-Subscription-Key",
                "api-key",
                "Authorization",
                "X-AI4IA-App-Id",
                "X-AI4IA-User-Id",
                "X-UserProfile",
            },
        )
        self.assertEqual(
            inbound.find("./set-backend-service").attrib["base-url"],
            "{{speech-voice-live-wss-endpoint}}/voice-live/realtime",
        )
        self.assertEqual(
            inbound.find("./authentication-managed-identity").attrib["resource"],
            "{{speech-voice-live-mi-audience}}",
        )

    # --- photoAvatars -----------------------------------------------------

    @property
    def avatars(self) -> dict:
        return self.raw["photoAvatars"]

    def _mutated_avatars(self, change) -> dict:
        mutated = copy.deepcopy(self.raw)
        change(mutated["photoAvatars"])
        return mutated

    def test_photo_avatar_block_is_projected_unchanged_and_bound_to_models_regions(self) -> None:
        catalog = self.gen.build_catalog(self.raw)
        self.assertEqual(catalog["photoAvatars"], self.avatars)
        models = json.loads((REPO_ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
        region = models["regions"][self.avatars["homeRegion"]]
        self.assertEqual(region["dataZone"], self.avatars["homeDataZone"])
        self.assertEqual(self.avatars["requiredFeature"], "CustomAvatar")
        self.assertEqual(self.avatars["apiVersion"], "2023-12-01-preview")
        self.assertEqual(
            self.avatars["attributes"]["style"], ["Realistic", "DigitalIllustration", "Stylized3D"],
        )
        packaged = json.loads(
            (REPO_ROOT / "app/api/src/ai4ia_api/data/voice_provider_catalog.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(packaged["photoAvatars"], self.avatars)

    def test_schema_and_generator_reject_invalid_photo_avatar_blocks(self) -> None:
        mutations = {
            "unknown key": lambda b: b.update({"endpoint": "https://example.invalid"}),
            "account override": lambda b: b.update({"accountName": "someone-else"}),
            "api version shape": lambda b: b.update({"apiVersion": "latest"}),
            "empty feature": lambda b: b.update({"requiredFeature": ""}),
            "project suffix": lambda b: b.update({"projectSuffix": "_Other"}),
            "prompt bound zero": lambda b: b.update({"promptMaxChars": 0}),
            "prompt bound huge": lambda b: b.update({"promptMaxChars": 5000}),
            "prompt bound float": lambda b: b.update({"promptMaxChars": 10.5}),
            "duplicate enum": lambda b: b["attributes"]["gender"].append("Male"),
            "enum token": lambda b: b["attributes"]["age"].append("Young Adult"),
            "extra attribute": lambda b: b["attributes"].update({"hair": ["Long"]}),
            "missing attribute": lambda b: b["attributes"].pop("style"),
            "lookalike host": lambda b: b["preview"].update(
                {"host": "stttssvcproduse2.blob.core.windows.net.example.com"}
            ),
            "non-blob host": lambda b: b["preview"].update({"host": "example.com"}),
            "oversize preview": lambda b: b["preview"].update({"maxBytes": 64 * 1024 * 1024}),
            "html preview": lambda b: b["preview"]["contentTypes"].append("text/html"),
            "missing preview": lambda b: b.pop("preview"),
            "missing live meter": lambda b: b.pop("liveBillingModelId"),
            "live meter shape": lambda b: b.update({"liveBillingModelId": "Live Meter"}),
        }
        for label, change in mutations.items():
            with self.subTest(label=label):
                mutated = self._mutated_avatars(change)
                self.assert_schema_rejects(mutated)
                self.assert_generator_rejects(mutated)
        missing = copy.deepcopy(self.raw)
        del missing["photoAvatars"]
        self.assert_schema_rejects(missing)
        self.assert_generator_rejects(missing)

    def test_generator_binds_home_region_and_zone_to_the_model_catalog(self) -> None:
        # Shape-valid values the schema cannot judge: only models.json can.
        for label, change in {
            "region outside the catalog": lambda b: b.update({"homeRegion": "westus2"}),
            "zone mismatch": lambda b: b.update({"homeDataZone": "EU"}),
        }.items():
            with self.subTest(label=label):
                mutated = self._mutated_avatars(change)
                jsonschema.validate(mutated, self.schema)
                self.assert_generator_rejects(mutated)
        # Control: a real catalog region with its own zone is accepted.
        moved = self._mutated_avatars(
            lambda b: b.update({"homeRegion": "swedencentral", "homeDataZone": "EU"})
        )
        self.gen.build_catalog(moved)

    def test_generator_requires_a_live_meter_distinct_from_creation(self) -> None:
        # Shape-valid for the schema, but a per-avatar meter cannot price live seconds.
        shared = self._mutated_avatars(
            lambda b: b.update({"liveBillingModelId": b["billingModelId"]})
        )
        jsonschema.validate(shared, self.schema)
        self.assert_generator_rejects(shared)
        # Control: a distinct, shape-valid live meter is accepted.
        distinct = self._mutated_avatars(
            lambda b: b.update({"liveBillingModelId": "photo-avatar-realtime-other"})
        )
        self.gen.build_catalog(distinct)

    def test_generated_photo_avatar_policy_is_current_and_catalog_driven(self) -> None:
        catalog = self.gen.build_catalog(self.raw)
        rendered = self.gen.render_photo_avatar_policy(catalog)
        actual = PHOTO_AVATAR_POLICY.read_text(encoding="utf-8")
        self.assertEqual(actual, rendered)
        root = ElementTree.fromstring(actual)
        validation = root.find("./inbound/set-variable").attrib["value"]
        for name, values in self.avatars["attributes"].items():
            listed = ", ".join(f'"{value}"' for value in values)
            self.assertIn(f'item.Name == "{name}" ? new string[] {{ {listed} }}', validation)
        self.assertIn(f"text.Length > {self.avatars['promptMaxChars']}", validation)
        templates = {element.attrib["template"] for element in root.iter("rewrite-uri")}
        self.assertTrue(all(template.endswith("?api-version=2023-12-01-preview") for template in templates))
        self.assertEqual(
            root.find("./inbound/set-backend-service").attrib["base-url"],
            "{{foundry-eastus2-endpoint}}",
        )
        # Catalog-driven, not a static file: each catalog value moves the policy.
        changed = self._mutated_avatars(
            lambda b: (
                b["attributes"]["style"].append("Watercolor"),
                b.update({"promptMaxChars": 900, "apiVersion": "2027-01-01-preview"}),
                b.update({"homeRegion": "swedencentral", "homeDataZone": "EU"}),
            )
        )
        moved = self.gen.render_photo_avatar_policy(self.gen.build_catalog(changed))
        self.assertIn("&quot;Watercolor&quot;", moved)
        self.assertIn("text.Length &gt; 900", moved)
        self.assertIn("api-version=2027-01-01-preview", moved)
        self.assertIn("{{foundry-swedencentral-endpoint}}", moved)
        self.assertNotIn("Watercolor", actual)


if __name__ == "__main__":
    unittest.main()
