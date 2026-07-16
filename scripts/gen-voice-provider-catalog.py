#!/usr/bin/env python3
"""Generate the packaged voice-provider catalog from infra/voice-providers.json.

The infra catalog is the source of truth for both providers:

- ``azure_openai`` stays deployment-catalog driven and references
  ``infra/models.json`` for realtime deployments.
- ``speech_voice_live`` is fixed to the managed Voice Live model/API version and
  carries curated azure-standard built-in Speech voices plus safe capability
  defaults/options.

Run from the repo root:  python scripts/gen-voice-provider-catalog.py
Verify-only (CI drift):  python scripts/gen-voice-provider-catalog.py --check
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "infra" / "voice-providers.json"
API_TARGET = (
    REPO_ROOT / "app" / "api" / "src" / "ai4ia_api" / "data" / "voice_provider_catalog.json"
)
WEB_TARGET = REPO_ROOT / "app" / "web" / "src" / "lib" / "data" / "voice_provider_catalog.ts"

EXPECTED_PROVIDER_IDS = ("azure_openai", "speech_voice_live")
EXPECTED_DEFAULT_PROVIDER_ID = "azure_openai"
AZURE_OPENAI_VOICES = (
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
)
SPEECH_VOICES = (
    "en-US-Ava:DragonHDLatestNeural",
    "en-US-AvaNeural",
    "en-US-AndrewNeural",
    "en-US-Brian:DragonHDLatestNeural",
    "en-US-Emma:DragonHDLatestNeural",
    "en-US-Jenny:DragonHDLatestNeural",
)
SPEECH_TRANSCRIPTION_COMPATIBILITY = {
    ("gpt-realtime", "2026-04-10"): ("gpt-4o-transcribe",),
}


def _require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def _exact_keys(
    errors: list[str], obj: Any, *, allowed: tuple[str, ...], label: str
) -> None:
    if not isinstance(obj, dict):
        errors.append(f"{label} must be an object")
        return
    unexpected = sorted(set(obj) - set(allowed))
    if unexpected:
        errors.append(f"{label} has unexpected field(s): {', '.join(unexpected)}")


def _provider_map(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers = raw.get("providers", [])
    return {entry.get("id", ""): entry for entry in providers if isinstance(entry, dict)}


def _validate_voice_membership(
    errors: list[str], provider: dict[str, Any], *, expected: tuple[str, ...]
) -> None:
    capabilities = provider.get("capabilities", {})
    voices = capabilities.get("voices", {})
    options = tuple(voices.get("options", []))
    _require(
        errors,
        options == expected,
        f"{provider.get('id', '<unknown>')}: voices.options must be {expected!r} (got {options!r})",
    )
    _require(
        errors,
        voices.get("default") == expected[0],
        f"{provider.get('id', '<unknown>')}: voices.default must be {expected[0]!r}",
    )
    for voice in expected:
        _require(
            errors,
            voice in options,
            f"{provider.get('id', '<unknown>')}: voice {voice!r} is missing from the curated list",
        )


def _validate_common_provider(
    errors: list[str], provider: dict[str, Any], *, expected_display_label: str
) -> None:
    _exact_keys(
        errors,
        provider,
        allowed=(
            "id",
            "displayName",
            "displayLabel",
            "description",
            "transport",
            "selectionMode",
            "endpointPath",
            "modelCatalogRef",
            "managedModel",
            "sessionDefaults",
            "capabilities",
        ),
        label=f"{provider.get('id', '<unknown>')}",
    )
    _require(errors, provider.get("transport") == "websocket", f"{provider.get('id')}: transport must be websocket")
    _require(
        errors,
        provider.get("displayLabel") == expected_display_label,
        f"{provider.get('id')}: displayLabel must be {expected_display_label!r}",
    )
    custom_voice = provider.get("capabilities", {}).get("customVoice", {})
    for field in ("enabled", "allowEndpointIds", "allowLexicons", "allowPersonalVoice"):
        _require(
            errors,
            custom_voice.get(field) is False,
            f"{provider.get('id')}: customVoice.{field} must remain false",
        )


def _validate_azure_openai(errors: list[str], provider: dict[str, Any]) -> None:
    _require(
        errors,
        provider.get("selectionMode") == "deployment_catalog",
        "azure_openai: selectionMode must be deployment_catalog",
    )
    _require(
        errors,
        provider.get("endpointPath") == "/openai/realtime",
        "azure_openai: endpointPath must be /openai/realtime",
    )
    model_ref = provider.get("modelCatalogRef", {})
    _exact_keys(
        errors,
        model_ref,
        allowed=("sourceJson", "filter", "defaultModelId"),
        label="azure_openai.modelCatalogRef",
    )
    _exact_keys(
        errors,
        model_ref.get("filter", {}),
        allowed=("category",),
        label="azure_openai.modelCatalogRef.filter",
    )
    _require(
        errors,
        model_ref.get("sourceJson") == "infra/models.json",
        "azure_openai: modelCatalogRef.sourceJson must reference infra/models.json",
    )
    _require(
        errors,
        model_ref.get("filter", {}).get("category") == "realtime",
        "azure_openai: modelCatalogRef.filter.category must be realtime",
    )
    _require(
        errors,
        model_ref.get("defaultModelId") == "gpt-realtime",
        "azure_openai: modelCatalogRef.defaultModelId must be gpt-realtime",
    )
    defaults = provider.get("sessionDefaults", {})
    _exact_keys(
        errors,
        defaults,
        allowed=("voice", "inputTranscription", "turnDetection", "interruptResponse", "autoTruncate"),
        label="azure_openai.sessionDefaults",
    )
    _require(errors, defaults.get("voice") == "alloy", "azure_openai: default voice must be alloy")
    _require(
        errors,
        defaults.get("inputTranscription") == "whisper-1",
        "azure_openai: default inputTranscription must be whisper-1",
    )
    _require(
        errors,
        defaults.get("turnDetection") == "server_vad",
        "azure_openai: default turnDetection must be server_vad",
    )
    _require(
        errors,
        defaults.get("interruptResponse") is True,
        "azure_openai: default interruptResponse must be true",
    )
    _require(
        errors,
        defaults.get("autoTruncate") is False,
        "azure_openai: default autoTruncate must be false",
    )
    _validate_voice_membership(errors, provider, expected=AZURE_OPENAI_VOICES)
    capabilities = provider.get("capabilities", {})
    _exact_keys(
        errors,
        capabilities,
        allowed=(
            "voices",
            "inputTranscription",
            "turnDetection",
            "interruption",
            "customVoice",
        ),
        label="azure_openai.capabilities",
    )
    _exact_keys(
        errors,
        capabilities.get("voices", {}),
        allowed=("kind", "default", "options"),
        label="azure_openai.capabilities.voices",
    )
    input_transcription = capabilities.get("inputTranscription", {})
    _exact_keys(
        errors,
        input_transcription,
        allowed=("provider", "default", "options"),
        label="azure_openai.capabilities.inputTranscription",
    )
    _require(
        errors,
        input_transcription.get("provider") == "openai",
        "azure_openai: inputTranscription.provider must be openai",
    )
    _require(
        errors,
        tuple(input_transcription.get("options", [])) == ("whisper-1",),
        "azure_openai: inputTranscription.options must contain only whisper-1",
    )
    turn_detection = capabilities.get("turnDetection", {})
    _exact_keys(
        errors,
        turn_detection,
        allowed=("default", "options"),
        label="azure_openai.capabilities.turnDetection",
    )
    _exact_keys(
        errors,
        capabilities.get("interruption", {}),
        allowed=("interruptResponse", "autoTruncate"),
        label="azure_openai.capabilities.interruption",
    )
    _exact_keys(
        errors,
        capabilities.get("customVoice", {}),
        allowed=("enabled", "allowEndpointIds", "allowLexicons", "allowPersonalVoice"),
        label="azure_openai.capabilities.customVoice",
    )
    _require(
        errors,
        tuple(turn_detection.get("options", [])) == ("server_vad", "semantic_vad"),
        "azure_openai: turnDetection.options must be server_vad + semantic_vad",
    )


def _validate_speech_voice_live(errors: list[str], provider: dict[str, Any]) -> None:
    _require(
        errors,
        provider.get("selectionMode") == "fixed_managed_model",
        "speech_voice_live: selectionMode must be fixed_managed_model",
    )
    _require(
        errors,
        provider.get("endpointPath") == "/voice-live/realtime",
        "speech_voice_live: endpointPath must be /voice-live/realtime",
    )
    managed = provider.get("managedModel", {})
    _exact_keys(
        errors,
        managed,
        allowed=("modelId", "apiVersion", "initialRegion", "audioFormat", "sampleRateHz"),
        label="speech_voice_live.managedModel",
    )
    _require(errors, managed.get("modelId") == "gpt-realtime", "speech_voice_live: modelId must be gpt-realtime")
    _require(
        errors,
        managed.get("apiVersion") == "2026-04-10",
        "speech_voice_live: apiVersion must be 2026-04-10",
    )
    _require(
        errors,
        managed.get("initialRegion") == "eastus2",
        "speech_voice_live: initialRegion must be eastus2",
    )
    _require(
        errors,
        managed.get("audioFormat") == "pcm16",
        "speech_voice_live: audioFormat must be pcm16",
    )
    _require(
        errors,
        managed.get("sampleRateHz") == 24000,
        "speech_voice_live: sampleRateHz must be 24000",
    )
    defaults = provider.get("sessionDefaults", {})
    _exact_keys(
        errors,
        defaults,
        allowed=(
            "voice",
            "inputTranscription",
            "turnDetection",
            "locale",
            "noiseSuppression",
            "echoCancellation",
            "interruptResponse",
            "autoTruncate",
        ),
        label="speech_voice_live.sessionDefaults",
    )
    _require(
        errors,
        defaults.get("voice") == "en-US-Ava:DragonHDLatestNeural",
        "speech_voice_live: default voice must be en-US-Ava:DragonHDLatestNeural",
    )
    compatible_transcriptions = SPEECH_TRANSCRIPTION_COMPATIBILITY.get(
        (managed.get("modelId"), managed.get("apiVersion")),
        (),
    )
    _require(
        errors,
        defaults.get("inputTranscription") in compatible_transcriptions,
        "speech_voice_live: default inputTranscription is incompatible with "
        f"{managed.get('modelId')} at {managed.get('apiVersion')}; expected one of "
        f"{compatible_transcriptions}",
    )
    _require(
        errors,
        defaults.get("turnDetection") == "azure_semantic_vad",
        "speech_voice_live: default turnDetection must be azure_semantic_vad",
    )
    _require(errors, defaults.get("locale") == "en-US", "speech_voice_live: default locale must be en-US")
    _require(
        errors,
        defaults.get("noiseSuppression") == "azure_deep_noise_suppression",
        "speech_voice_live: default noiseSuppression must be azure_deep_noise_suppression",
    )
    _require(
        errors,
        defaults.get("echoCancellation") == "server_echo_cancellation",
        "speech_voice_live: default echoCancellation must be server_echo_cancellation",
    )
    _require(
        errors,
        defaults.get("interruptResponse") is True,
        "speech_voice_live: default interruptResponse must be true",
    )
    _require(
        errors,
        defaults.get("autoTruncate") is False,
        "speech_voice_live: default autoTruncate must be false",
    )
    _validate_voice_membership(errors, provider, expected=SPEECH_VOICES)
    capabilities = provider.get("capabilities", {})
    _exact_keys(
        errors,
        capabilities,
        allowed=(
            "voices",
            "inputTranscription",
            "turnDetection",
            "noiseSuppression",
            "echoCancellation",
            "interruption",
            "locale",
            "customVoice",
        ),
        label="speech_voice_live.capabilities",
    )
    _exact_keys(
        errors,
        capabilities.get("voices", {}),
        allowed=("kind", "default", "options"),
        label="speech_voice_live.capabilities.voices",
    )
    input_transcription = capabilities.get("inputTranscription", {})
    _exact_keys(
        errors,
        input_transcription,
        allowed=("provider", "default", "options"),
        label="speech_voice_live.capabilities.inputTranscription",
    )
    _require(
        errors,
        input_transcription.get("provider") == "openai",
        "speech_voice_live: inputTranscription.provider must be openai",
    )
    _require(
        errors,
        tuple(input_transcription.get("options", [])) == compatible_transcriptions,
        "speech_voice_live: inputTranscription.options must exactly match the "
        f"compatibility matrix for {managed.get('modelId')} at "
        f"{managed.get('apiVersion')}",
    )
    turn_detection = capabilities.get("turnDetection", {})
    _exact_keys(
        errors,
        turn_detection,
        allowed=("default", "options"),
        label="speech_voice_live.capabilities.turnDetection",
    )
    _require(
        errors,
        tuple(turn_detection.get("options", [])) == (
            "azure_semantic_vad",
            "azure_semantic_vad_multilingual",
        ),
        "speech_voice_live: turnDetection.options must be azure_semantic_vad + azure_semantic_vad_multilingual",
    )
    noise = capabilities.get("noiseSuppression", {})
    _exact_keys(
        errors,
        noise,
        allowed=("default", "options"),
        label="speech_voice_live.capabilities.noiseSuppression",
    )
    _require(
        errors,
        tuple(noise.get("options", [])) == ("azure_deep_noise_suppression",),
        "speech_voice_live: noiseSuppression.options must contain only azure_deep_noise_suppression",
    )
    echo = capabilities.get("echoCancellation", {})
    _exact_keys(
        errors,
        echo,
        allowed=("default", "options"),
        label="speech_voice_live.capabilities.echoCancellation",
    )
    _require(
        errors,
        tuple(echo.get("options", [])) == ("server_echo_cancellation",),
        "speech_voice_live: echoCancellation.options must contain only server_echo_cancellation",
    )
    locale = capabilities.get("locale", {})
    _exact_keys(
        errors,
        locale,
        allowed=("default", "options"),
        label="speech_voice_live.capabilities.locale",
    )
    _require(
        errors,
        locale.get("default") == "en-US",
        "speech_voice_live: locale.default must be en-US",
    )
    _require(
        errors,
        tuple(locale.get("options", [])) == ("en-US",),
        "speech_voice_live: locale.options must contain only en-US",
    )
    _exact_keys(
        errors,
        capabilities.get("interruption", {}),
        allowed=("interruptResponse", "autoTruncate"),
        label="speech_voice_live.capabilities.interruption",
    )
    _exact_keys(
        errors,
        capabilities.get("customVoice", {}),
        allowed=("enabled", "allowEndpointIds", "allowLexicons", "allowPersonalVoice"),
        label="speech_voice_live.capabilities.customVoice",
    )


def build_catalog(raw: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    _require(errors, raw.get("defaultProviderId") == EXPECTED_DEFAULT_PROVIDER_ID, "defaultProviderId must be azure_openai")
    providers = raw.get("providers", [])
    _require(errors, isinstance(providers, list), "providers must be an array")
    _require(errors, len(providers) == 2, "providers must contain exactly two entries")

    provider_ids = tuple(entry.get("id", "") for entry in providers if isinstance(entry, dict))
    _require(
        errors,
        provider_ids == EXPECTED_PROVIDER_IDS,
        f"provider ids must be {EXPECTED_PROVIDER_IDS!r} in that order (got {provider_ids!r})",
    )

    by_id = _provider_map(raw)
    _require(errors, set(by_id) == set(EXPECTED_PROVIDER_IDS), f"providers must include exactly {EXPECTED_PROVIDER_IDS!r}")

    for provider_id in EXPECTED_PROVIDER_IDS:
        provider = by_id.get(provider_id, {})
        expected_display_name = {
            "azure_openai": "Azure OpenAI Realtime",
            "speech_voice_live": "Azure Speech Voice Live",
        }[provider_id]
        expected_display_label = {
            "azure_openai": "Azure OpenAI",
            "speech_voice_live": "Azure Speech",
        }[provider_id]
        _require(errors, provider.get("displayName") == expected_display_name, f"{provider_id}: displayName must be {expected_display_name!r}")
        _validate_common_provider(errors, provider, expected_display_label=expected_display_label)

    _validate_azure_openai(errors, by_id.get("azure_openai", {}))
    _validate_speech_voice_live(errors, by_id.get("speech_voice_live", {}))

    if errors:
        raise SystemExit(
            "FAIL: {} issue(s) in {}:\n  - {}".format(
                len(errors),
                SOURCE.name,
                "\n  - ".join(errors),
            )
        )

    catalog = copy.deepcopy(raw)
    catalog["_comment"] = "GENERATED by scripts/gen-voice-provider-catalog.py from infra/voice-providers.json. Do not edit by hand."
    return catalog


def render_ts(catalog: dict[str, Any], provider_ids: tuple[str, ...]) -> str:
    rendered = json.dumps(catalog, indent=2, ensure_ascii=False)
    provider_ids_rendered = ",\n  ".join(f'"{pid}"' for pid in provider_ids)
    return (
        "/* GENERATED by scripts/gen-voice-provider-catalog.py from infra/voice-providers.json. Do not edit by hand. */\n"
        "\n"
        f"export const voiceProviderCatalog = {rendered} as const;\n"
        "\n"
        "export type VoiceProviderCatalog = typeof voiceProviderCatalog;\n"
        "export type VoiceProvider = VoiceProviderCatalog[\"providers\"][number];\n"
        "export type VoiceProviderId = VoiceProvider[\"id\"];\n"
        "export const DEFAULT_VOICE_PROVIDER_ID = voiceProviderCatalog.defaultProviderId;\n"
        "export const VOICE_PROVIDER_IDS = [\n"
        f"  {provider_ids_rendered}\n"
        "] as const;\n"
    )


def _write_if_needed(path: Path, content: str, *, check: bool) -> None:
    if check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != content:
            raise SystemExit(f"{path.name} is stale. Run: python scripts/gen-voice-provider-catalog.py")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the generated catalogs are stale (no write).",
    )
    args = parser.parse_args()

    raw = json.loads(SOURCE.read_text(encoding="utf-8"))
    catalog = build_catalog(raw)

    api_json = json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
    # ``providers_ids`` is a tiny helper for TS rendering, never written to disk.
    web_ts = render_ts(catalog, tuple(provider["id"] for provider in catalog["providers"])) + "\n"

    if args.check:
        _write_if_needed(API_TARGET, api_json, check=True)
        _write_if_needed(WEB_TARGET, web_ts, check=True)
        print("voice_provider_catalog outputs are up to date.")
        return 0

    _write_if_needed(API_TARGET, api_json, check=False)
    _write_if_needed(WEB_TARGET, web_ts, check=False)
    print(
        f"Wrote {API_TARGET.relative_to(REPO_ROOT)} and {WEB_TARGET.relative_to(REPO_ROOT)} "
        f"({len(catalog['providers'])} providers)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
