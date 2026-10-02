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
    ("gpt-realtime-1.5", "native_audio", "openai", "gpt-4o-transcribe"),
    ("gpt-realtime-2.1", "native_audio", "openai", "gpt-4o-transcribe"),
    ("gpt-realtime-2.1-mini", "native_audio", "openai", "gpt-4o-transcribe"),
    ("gpt-4.1", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-4.1-mini", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5-mini", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5.1", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5.2", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5.4", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5.6-terra", "azure_speech_chain", "azure_speech", "azure-speech"),
    ("gpt-5.6-luna", "azure_speech_chain", "azure_speech", "azure-speech"),
)
ADDED_MODEL_IDS = (
    "gpt-realtime-1.5",
    "gpt-realtime-2.1",
    "gpt-realtime-2.1-mini",
    "gpt-5.2",
    "gpt-5.4",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
# Names the catalog must not accept: GPT-6/6.1 are not Voice Live models,
# gpt-5.5 and gpt-5.4-mini/nano are bring-your-own-model only, azure-realtime
# needs its own voice type, and Data Zone variants are separately named models.
UNSUPPORTED_MODEL_IDS = (
    "gpt-6",
    "gpt-6.1",
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "azure-realtime",
    "gpt-realtime-2.1-datazone",
    "gpt-realtime-2",
)
SPEECH_GA_VOICES = [
    "en-US-Ava:DragonHDLatestNeural",
    "en-US-AvaNeural",
    "en-US-AndrewNeural",
    "en-US-Brian:DragonHDLatestNeural",
    "en-US-Emma:DragonHDLatestNeural",
    "en-US-Jenny:DragonHDLatestNeural",
]
# Spelled out (not derived) so a generator typo cannot agree with itself.
MAI_VOICES = [
    "en-US-Ethan:MAI-Voice-2.1-Flash",
    "en-US-Grant:MAI-Voice-2.1-Flash",
    "en-US-Harper:MAI-Voice-2.1-Flash",
    "en-US-Iris:MAI-Voice-2.1-Flash",
    "en-US-Jasper:MAI-Voice-2.1-Flash",
    "en-US-Olivia:MAI-Voice-2.1-Flash",
    "en-US-Sage:MAI-Voice-2.1-Flash",
    "en-US-Ethan:MAI-Voice-2.1",
    "en-US-Grant:MAI-Voice-2.1",
    "en-US-Harper:MAI-Voice-2.1",
    "en-US-Iris:MAI-Voice-2.1",
    "en-US-Jasper:MAI-Voice-2.1",
    "en-US-Olivia:MAI-Voice-2.1",
    "en-US-Sage:MAI-Voice-2.1",
]


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
        # The default stays per managed model; only alternatives are selectable.
        self.assertNotIn("inputTranscription", speech["sessionDefaults"])
        self.assertEqual(
            speech["capabilities"]["inputTranscription"],
            {
                "options": [
                    {
                        "model": "mai-transcribe-2",
                        "displayName": "MAI Transcribe 2",
                        "preview": True,
                        "profiles": ["native_audio", "azure_speech_chain"],
                    }
                ]
            },
        )
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
        self.assertEqual(
            speech["capabilities"]["voices"]["options"],
            [*SPEECH_GA_VOICES, *MAI_VOICES],
        )
        self.assertEqual(speech["capabilities"]["voices"]["previewOptions"], MAI_VOICES)
        self.assertEqual(
            speech["sessionDefaults"]["voice"],
            "en-US-Ava:DragonHDLatestNeural",
        )
        self.assertFalse(speech["capabilities"]["customVoice"]["allowPersonalVoice"])
        # The opt-in Live-Reference AEC contract; every managed model stays pinned.
        self.assertEqual(
            speech["capabilities"]["echoCancellation"],
            {
                "default": "server_echo_cancellation",
                "options": ["server_echo_cancellation"],
                "clientReference": {
                    "preview": True,
                    "apiVersion": "2026-07-15",
                    "features": "client_ec_reference:true",
                    "channels": 2,
                },
            },
        )
        self.assertEqual(speech["sessionDefaults"]["echoCancellation"], "server_echo_cancellation")

    def test_schema_and_generator_reject_unreviewed_echo_reference(self) -> None:
        def reference_mutation(change) -> dict:
            mutated = copy.deepcopy(self.raw)
            change(mutated["providers"][1]["capabilities"]["echoCancellation"]["clientReference"])
            return mutated

        def set_field(field: str, value: object):
            return lambda block: block.__setitem__(field, value)

        mutations = {
            "pinned version": reference_mutation(set_field("apiVersion", "2026-04-10")),
            "preview version": reference_mutation(set_field("apiVersion", "2026-06-01-preview")),
            "flag off": reference_mutation(set_field("features", "client_ec_reference:false")),
            "extra flag": reference_mutation(
                set_field("features", "client_ec_reference:true,other:true")
            ),
            "mono reference": reference_mutation(set_field("channels", 1)),
            "not marked preview": reference_mutation(set_field("preview", False)),
            "endpoint field": reference_mutation(
                set_field("endpoint", "wss://attacker.example")
            ),
            "missing flag": reference_mutation(lambda block: block.pop("features")),
        }
        missing = copy.deepcopy(self.raw)
        del missing["providers"][1]["capabilities"]["echoCancellation"]["clientReference"]
        mutations["missing block"] = missing

        for label, mutated in mutations.items():
            with self.subTest(label=label):
                self.assert_schema_rejects(mutated)
                self.assert_generator_rejects(mutated)

        # JSON Schema compares numbers by value, so only the generator's type
        # checks catch these; the accepted catalog is the control.
        self.gen.build_catalog(self.raw)
        for label, change in (
            ("float channels", set_field("channels", 2.0)),
            ("integer preview", set_field("preview", 1)),
        ):
            with self.subTest(label=label):
                self.assert_generator_rejects(reference_mutation(change))

    def test_schema_and_generator_reject_unreviewed_mai_voice_contracts(self) -> None:
        mutations = {}

        unmarked = copy.deepcopy(self.raw)
        unmarked["providers"][1]["capabilities"]["voices"]["previewOptions"].pop()
        mutations["MAI voice not marked preview"] = unmarked

        preview_default = copy.deepcopy(self.raw)
        preview_default["providers"][1]["capabilities"]["voices"]["previewOptions"].append(
            "en-US-Ava:DragonHDLatestNeural"
        )
        mutations["default voice marked preview"] = preview_default

        missing_preview = copy.deepcopy(self.raw)
        del missing_preview["providers"][1]["capabilities"]["voices"]["previewOptions"]
        mutations["missing previewOptions"] = missing_preview

        for voice in (
            "en-US-Harper:MAI-Voice-2-Flash",
            "en-GB-Emily:MAI-Voice-2.1-Flash",
            "MAI-Voice-2.1-Flash",
        ):
            unreviewed = copy.deepcopy(self.raw)
            voices = unreviewed["providers"][1]["capabilities"]["voices"]
            voices["options"].append(voice)
            voices["previewOptions"].append(voice)
            mutations[f"unreviewed voice {voice}"] = unreviewed

        default_mai = copy.deepcopy(self.raw)
        default_mai["providers"][1]["capabilities"]["voices"][
            "default"
        ] = "en-US-Harper:MAI-Voice-2.1-Flash"
        mutations["preview default voice"] = default_mai

        for label, mutated in mutations.items():
            with self.subTest(label=label):
                self.assert_schema_rejects(mutated)
                self.assert_generator_rejects(mutated)

    def test_schema_and_generator_reject_unreviewed_transcription_options(self) -> None:
        def option_mutation(change) -> dict:
            mutated = copy.deepcopy(self.raw)
            change(mutated["providers"][1]["capabilities"]["inputTranscription"]["options"])
            return mutated

        def set_field(field: str, value: object):
            return lambda options: options[0].__setitem__(field, value)

        mutations = {
            "floating alias": option_mutation(set_field("model", "mai-transcribe")),
            "streaming product id": option_mutation(
                set_field("model", "MAI-Transcribe-2-Streaming")
            ),
            "managed default repeated": option_mutation(set_field("model", "azure-speech")),
            "unknown profile": option_mutation(set_field("profiles", ["agent"])),
            "empty profiles": option_mutation(set_field("profiles", [])),
            "not marked preview": option_mutation(set_field("preview", False)),
            "endpoint field": option_mutation(
                set_field("endpoint", "https://attacker.example")
            ),
            "extra option": option_mutation(
                lambda options: options.append(
                    {
                        "model": "whisper-1",
                        "displayName": "Whisper",
                        "preview": False,
                        "profiles": ["native_audio"],
                    }
                )
            ),
            "duplicate option": option_mutation(
                lambda options: options.append(copy.deepcopy(options[0]))
            ),
            "no options": option_mutation(lambda options: options.clear()),
        }
        missing = copy.deepcopy(self.raw)
        del missing["providers"][1]["capabilities"]["inputTranscription"]
        mutations["missing capability"] = missing
        session_default = copy.deepcopy(self.raw)
        session_default["providers"][1]["sessionDefaults"][
            "inputTranscription"
        ] = "mai-transcribe-2"
        mutations["provider-wide default"] = session_default

        for label, mutated in mutations.items():
            with self.subTest(label=label):
                self.assert_schema_rejects(mutated)
                self.assert_generator_rejects(mutated)

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

    def _model_index(self, model_id: str) -> int:
        ids = [model["id"] for model in self.speech["managedModels"]]
        self.assertIn(model_id, ids)
        return ids.index(model_id)

    def test_schema_and_generator_reject_unsupported_ids_in_added_positions(self) -> None:
        for model_id in ADDED_MODEL_IDS:
            index = self._model_index(model_id)
            for unsupported in UNSUPPORTED_MODEL_IDS:
                with self.subTest(position=model_id, replacement=unsupported):
                    mutated = copy.deepcopy(self.raw)
                    mutated["providers"][1]["managedModels"][index]["id"] = unsupported
                    self.assert_schema_rejects(mutated)
                    self.assert_generator_rejects(mutated)

    def test_schema_and_generator_pin_each_added_model_contract(self) -> None:
        jsonschema.validate(self.raw, self.schema)
        self.gen.build_catalog(self.raw)
        transcription = {
            "native_audio": {"provider": "openai", "model": "gpt-4o-transcribe"},
            "azure_speech_chain": {"provider": "azure_speech", "model": "azure-speech"},
        }
        other_profile = {
            "native_audio": "azure_speech_chain",
            "azure_speech_chain": "native_audio",
        }
        for model_id in ADDED_MODEL_IDS:
            index = self._model_index(model_id)
            profile = self.speech["managedModels"][index]["profile"]
            swapped = other_profile[profile]
            changes = {
                "other profile's transcription": lambda m, s=swapped: m.update(
                    {"inputTranscription": copy.deepcopy(transcription[s])}
                ),
                "other profile": lambda m, s=swapped: m.update({"profile": s}),
                "other profile and its transcription": lambda m, s=swapped: m.update(
                    {"profile": s, "inputTranscription": copy.deepcopy(transcription[s])}
                ),
                "newer api version": lambda m: m.update({"apiVersion": "2026-07-15"}),
                "other region": lambda m: m.update({"initialRegion": "swedencentral"}),
                "missing transcription": lambda m: m.pop("inputTranscription"),
            }
            for label, change in changes.items():
                with self.subTest(model=model_id, label=label):
                    mutated = copy.deepcopy(self.raw)
                    change(mutated["providers"][1]["managedModels"][index])
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
        echo_pair = (
            'context.Request.Url.Query.GetValueOrDefault("features", "") == '
            '"client_ec_reference:true" && '
            'context.Request.Url.Query.GetValueOrDefault("api-version", "") == "2026-07-15"'
        )
        self.assertEqual(
            query["api-version"].findtext("value"),
            f'@(({echo_pair}) ? "2026-07-15" : "2026-04-10")',
        )
        self.assertNotIn("features", query)
        feature_branch = inbound.findall("./choose/when")[1]
        self.assertEqual(
            feature_branch.attrib["condition"],
            f'@(context.Request.Url.Query.ContainsKey("features") && !({echo_pair}))',
        )
        self.assertEqual(feature_branch.find("./return-response/set-status").attrib["code"], "400")
        self.assertIsNone(feature_branch.find("./return-response/set-body"))
        # Catalog-driven: the echo-reference pair comes from the catalog block.
        moved = copy.deepcopy(catalog)
        moved["providers"][1]["capabilities"]["echoCancellation"]["clientReference"].update(
            apiVersion="2099-01-01", features="other_flag:true",
        )
        rendered = self.gen.render_speech_voice_live_policy(moved)
        self.assertIn('== &quot;2099-01-01&quot;', rendered)
        self.assertIn('? "2099-01-01" : "2026-04-10"', rendered)
        self.assertIn("other_flag:true", rendered)
        self.assertNotIn("client_ec_reference", rendered)
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
        # Shape-valid values the schema cannot judge: only models.json can, and
        # each has its own message, so neither check hides behind the other.
        for label, change, message in (
            ("region outside the catalog", lambda b: b.update({"homeRegion": "westus2"}),
             "homeRegion must be a region in infra/models.json (got 'westus2')"),
            ("zone mismatch", lambda b: b.update({"homeDataZone": "EU"}),
             "homeDataZone must equal the models.json dataZone of 'eastus2' ('US')"),
        ):
            with self.subTest(label=label):
                mutated = self._mutated_avatars(change)
                jsonschema.validate(mutated, self.schema)
                with self.assertRaises(SystemExit) as refused:
                    self.gen.build_catalog(mutated)
                self.assertIn(message, str(refused.exception))
                self.assertEqual(str(refused.exception).count("photoAvatars.home"), 1)
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
