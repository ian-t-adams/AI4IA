from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "scripts" / "gen-voice-provider-catalog.py"
SOURCE = REPO_ROOT / "infra" / "voice-providers.json"
SCHEMA = REPO_ROOT / "infra" / "voice-providers.schema.json"


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

    def test_schema_accepts_the_authoritative_catalog(self) -> None:
        jsonschema.validate(self.raw, self.schema)

    def test_generator_projects_the_expected_provider_contracts(self) -> None:
        catalog = self.gen.build_catalog(self.raw)
        self.assertEqual(catalog["defaultProviderId"], "azure_openai")
        self.assertEqual([p["id"] for p in catalog["providers"]], ["azure_openai", "speech_voice_live"])

        azure_openai = catalog["providers"][0]
        self.assertEqual(azure_openai["selectionMode"], "deployment_catalog")
        self.assertEqual(azure_openai["modelCatalogRef"]["sourceJson"], "infra/models.json")
        self.assertEqual(azure_openai["modelCatalogRef"]["defaultModelId"], "gpt-realtime")
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
        self.assertEqual(speech["selectionMode"], "fixed_managed_model")
        self.assertEqual(speech["managedModel"]["modelId"], "gpt-realtime")
        self.assertEqual(speech["managedModel"]["apiVersion"], "2026-04-10")
        self.assertEqual(speech["managedModel"]["initialRegion"], "eastus2")
        self.assertEqual(speech["managedModel"]["sampleRateHz"], 24000)
        self.assertEqual(
            speech["capabilities"]["voices"]["options"][0],
            "en-US-Ava:DragonHDLatestNeural",
        )
        self.assertEqual(
            speech["capabilities"]["turnDetection"]["options"],
            ["azure_semantic_vad", "azure_semantic_vad_multilingual"],
        )
        self.assertEqual(
            speech["capabilities"]["noiseSuppression"]["options"],
            ["azure_deep_noise_suppression"],
        )
        self.assertEqual(
            speech["capabilities"]["echoCancellation"]["options"],
            ["server_echo_cancellation"],
        )
        self.assertEqual(
            speech["capabilities"]["inputTranscription"],
            {
                "provider": "openai",
                "default": "gpt-4o-transcribe",
                "options": ["gpt-4o-transcribe"],
            },
        )
        self.assertEqual(speech["capabilities"]["locale"]["options"], ["en-US"])
        self.assertFalse(speech["capabilities"]["customVoice"]["allowPersonalVoice"])

    def test_generator_rejects_custom_voice_and_endpoint_leaks(self) -> None:
        mutated = copy.deepcopy(self.raw)
        mutated["providers"][1]["capabilities"]["customVoice"]["enabled"] = True
        with self.assertRaises(SystemExit):
            self.gen.build_catalog(mutated)

    def test_generator_rejects_unsupported_managed_model_transcription_pair(self) -> None:
        mutated = copy.deepcopy(self.raw)
        speech = mutated["providers"][1]
        speech["sessionDefaults"]["inputTranscription"] = "azure-speech"
        speech["capabilities"]["inputTranscription"] = {
            "provider": "azure-speech",
            "default": "azure-speech",
            "options": ["azure-speech"],
        }
        with self.assertRaises(SystemExit):
            self.gen.build_catalog(mutated)

        mutated = copy.deepcopy(self.raw)
        speech = mutated["providers"][1]
        speech["managedModel"]["apiVersion"] = "2025-10-01"
        with self.assertRaises(SystemExit):
            self.gen.build_catalog(mutated)

        mutated = copy.deepcopy(self.raw)
        mutated["providers"][1]["managedModel"]["endpointId"] = "custom-endpoint"
        with self.assertRaises(SystemExit):
            self.gen.build_catalog(mutated)


if __name__ == "__main__":
    unittest.main()
