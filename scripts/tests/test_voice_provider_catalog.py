from __future__ import annotations

import copy
import importlib.util
import json
import re
import unittest
from pathlib import Path
from xml.etree import ElementTree

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "scripts" / "gen-voice-provider-catalog.py"
SOURCE = REPO_ROOT / "infra" / "voice-providers.json"
SCHEMA = REPO_ROOT / "infra" / "voice-providers.schema.json"
POLICY = REPO_ROOT / "infra" / "policies" / "speech-voice-live.xml"

EXPECTED_MODELS = (
    ("gpt-realtime", "native_audio", "openai", "gpt-4o-transcribe"),
    ("gpt-realtime-mini", "native_audio", "openai", "gpt-4o-transcribe"),
    ("gpt-4.1", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-4.1-mini", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5-mini", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5.1", "azure_speech_chain", "azure_speech", "azure-speech"),
)


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_voice_provider_catalog", GEN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VoiceProviderCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = json.loads(SOURCE.read_text(encoding="utf-8"))
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.gen = _load_gen()

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


if __name__ == "__main__":
    unittest.main()
