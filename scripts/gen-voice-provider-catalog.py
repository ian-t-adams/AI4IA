#!/usr/bin/env python3
"""Generate voice-provider artifacts from infra/voice-providers.json.

The infra catalog is the source of truth for both providers:

- ``azure_openai`` stays deployment-catalog driven and references
  ``infra/models.json`` for realtime deployments.
- ``speech_voice_live`` exposes only the curated managed-model catalog and
  carries curated azure-standard built-in Speech voices plus safe capability
  defaults/options.

It also owns the ``photoAvatars`` block: the custom photo avatar home region
(cross-checked against ``infra/models.json``), the undocumented creation
api-version, the Limited Access feature name, the observed attribute values and
the preview host. From it the generator renders the exact-operation photo
avatar APIM policy (``infra/policies/photo-avatars.xml``).

Run from the repo root:  python scripts/gen-voice-provider-catalog.py
Verify-only (CI drift):  python scripts/gen-voice-provider-catalog.py --check
"""
from __future__ import annotations

import copy
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _generator import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "infra" / "voice-providers.json"
API_TARGET = (
    REPO_ROOT / "app" / "api" / "src" / "ai4ia_api" / "data" / "voice_provider_catalog.json"
)
WEB_TARGET = REPO_ROOT / "app" / "web" / "src" / "lib" / "data" / "voice_provider_catalog.ts"
POLICY_TARGET = REPO_ROOT / "infra" / "policies" / "speech-voice-live.xml"
PHOTO_AVATAR_POLICY_TARGET = REPO_ROOT / "infra" / "policies" / "photo-avatars.xml"
MODELS_SOURCE = REPO_ROOT / "infra" / "models.json"

# Photo avatar gateway contract shared with infra/modules/gateway.bicep and
# app/api/src/ai4ia_api/photo_avatars/provider.py (parity is test-enforced).
PHOTO_AVATAR_API_PATH = "ai4ia-photo-avatars-v1"
PHOTO_AVATAR_SUBSCRIPTION_PLACEHOLDER = "__AI4IA_PHOTO_AVATAR_SUBSCRIPTION_ID__"
# Stricter than the provider's own id pattern: APIM accepts only ids AI4IA
# issues, so an avatar created in the portal (or by anyone else) in the same
# account can never be read, replaced or deleted through this route.
PHOTO_AVATAR_ID_PATTERN = "ai4ia-[0-9a-f]{20}"
PHOTO_AVATAR_BODY_MAX_BYTES = 16384
PHOTO_AVATAR_FORWARD_TIMEOUT_SECONDS = 30
PHOTO_AVATAR_OPERATIONS = (
    ("photo-avatar-features", "GET", "/features"),
    ("photo-avatar-project-read", "GET", "/project"),
    ("photo-avatar-project-create", "PUT", "/project"),
    ("photo-avatar-create", "PUT", "/photoavatars"),
    ("photo-avatar-read", "GET", "/photoavatars"),
    ("photo-avatar-delete", "DELETE", "/photoavatars"),
)
PHOTO_AVATAR_ATTRIBUTE_NAMES = ("gender", "age", "ethnicity", "style")
PHOTO_AVATAR_STRIPPED_HEADERS = (
    "Ocp-Apim-Subscription-Key",
    "api-key",
    "Authorization",
    "S7P-KEY",
    "S7PTTL",
    "S7PTimeout",
    "x-S7PPriority",
    "X-AI4IA-App-Id",
    "X-AI4IA-User-Id",
    "X-UserProfile",
    "x-LLMModel",
    "x-PolicyCycleCounter",
    "x-LifetimePolicyCycleCounter",
)

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
SPEECH_DEFAULT_MANAGED_MODEL_ID = "gpt-realtime"
SPEECH_API_VERSION = "2026-04-10"
SPEECH_INITIAL_REGION = "eastus2"
SPEECH_AUDIO_FORMAT = "pcm16"
SPEECH_SAMPLE_RATE_HZ = 24000
SPEECH_MANAGED_MODEL_SPECS = (
    (
        "gpt-realtime",
        "GPT Realtime",
        "Native-audio realtime model with GPT-4o transcription.",
        "native_audio",
        "openai",
        "gpt-4o-transcribe",
    ),
    (
        "gpt-realtime-mini",
        "GPT Realtime Mini",
        "Lower-cost native-audio realtime model with GPT-4o transcription.",
        "native_audio",
        "openai",
        "gpt-4o-transcribe",
    ),
    (
        "gpt-4.1",
        "GPT-4.1",
        "GPT-4.1 response model paired with the Azure Speech chain.",
        "azure_speech_chain",
        "azure_speech",
        "azure-speech",
    ),
    (
        "gpt-4.1-mini",
        "GPT-4.1 Mini",
        "GPT-4.1 Mini response model paired with the Azure Speech chain.",
        "azure_speech_chain",
        "azure_speech",
        "azure-speech",
    ),
    (
        "gpt-5-mini",
        "GPT-5 Mini",
        "GPT-5 Mini response model paired with the Azure Speech chain.",
        "azure_speech_chain",
        "azure_speech",
        "azure-speech",
    ),
    (
        "gpt-5.1",
        "GPT-5.1",
        "GPT-5.1 response model paired with the Azure Speech chain.",
        "azure_speech_chain",
        "azure_speech",
        "azure-speech",
    ),
)
SPEECH_MANAGED_MODEL_IDS = tuple(spec[0] for spec in SPEECH_MANAGED_MODEL_SPECS)


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
    provider_specific_fields = {
        "azure_openai": ("modelCatalogRef",),
        "speech_voice_live": ("defaultManagedModelId", "managedModels"),
    }.get(provider.get("id"), ())
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
            "sessionDefaults",
            "capabilities",
            *provider_specific_fields,
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
        provider.get("selectionMode") == "managed_model_catalog",
        "speech_voice_live: selectionMode must be managed_model_catalog",
    )
    _require(
        errors,
        provider.get("endpointPath") == "/voice-live/realtime",
        "speech_voice_live: endpointPath must be /voice-live/realtime",
    )
    _require(
        errors,
        provider.get("defaultManagedModelId") == SPEECH_DEFAULT_MANAGED_MODEL_ID,
        f"speech_voice_live: defaultManagedModelId must be {SPEECH_DEFAULT_MANAGED_MODEL_ID}",
    )
    raw_managed_models = provider.get("managedModels", [])
    _require(
        errors,
        isinstance(raw_managed_models, list),
        "speech_voice_live: managedModels must be an array",
    )
    managed_models = raw_managed_models if isinstance(raw_managed_models, list) else []
    managed_model_ids = tuple(
        model.get("id", "") for model in managed_models if isinstance(model, dict)
    )
    _require(
        errors,
        managed_model_ids == SPEECH_MANAGED_MODEL_IDS,
        "speech_voice_live: managed model ids must be "
        f"{SPEECH_MANAGED_MODEL_IDS!r} in that order (got {managed_model_ids!r})",
    )
    _require(
        errors,
        len(managed_model_ids) == len(set(managed_model_ids)),
        "speech_voice_live: managed model ids must be unique",
    )
    _require(
        errors,
        provider.get("defaultManagedModelId") in managed_model_ids,
        "speech_voice_live: defaultManagedModelId must identify a managedModels entry",
    )
    model_fields = (
        "id",
        "displayName",
        "description",
        "profile",
        "inputTranscription",
        "apiVersion",
        "initialRegion",
        "audioFormat",
        "sampleRateHz",
    )
    for index, spec in enumerate(SPEECH_MANAGED_MODEL_SPECS):
        if index >= len(managed_models) or not isinstance(managed_models[index], dict):
            errors.append(f"speech_voice_live.managedModels[{index}] must be an object")
            continue
        model = managed_models[index]
        (
            expected_id,
            expected_display_name,
            expected_description,
            expected_profile,
            expected_transcription_provider,
            expected_transcription_model,
        ) = spec
        label = f"speech_voice_live.managedModels[{index}]"
        _exact_keys(errors, model, allowed=model_fields, label=label)
        for field, expected in (
            ("id", expected_id),
            ("displayName", expected_display_name),
            ("description", expected_description),
            ("profile", expected_profile),
            ("apiVersion", SPEECH_API_VERSION),
            ("initialRegion", SPEECH_INITIAL_REGION),
            ("audioFormat", SPEECH_AUDIO_FORMAT),
            ("sampleRateHz", SPEECH_SAMPLE_RATE_HZ),
        ):
            _require(
                errors,
                model.get(field) == expected,
                f"{label}.{field} must be {expected!r}",
            )
        raw_transcription = model.get("inputTranscription", {})
        _exact_keys(
            errors,
            raw_transcription,
            allowed=("provider", "model"),
            label=f"{label}.inputTranscription",
        )
        transcription = raw_transcription if isinstance(raw_transcription, dict) else {}
        _require(
            errors,
            transcription.get("provider") == expected_transcription_provider,
            f"{label}.inputTranscription.provider must be "
            f"{expected_transcription_provider!r}",
        )
        _require(
            errors,
            transcription.get("model") == expected_transcription_model,
            f"{label}.inputTranscription.model must be "
            f"{expected_transcription_model!r}",
        )

    defaults = provider.get("sessionDefaults", {})
    _exact_keys(
        errors,
        defaults,
        allowed=(
            "voice",
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


def _validate_photo_avatars(
    errors: list[str], block: Any, regions: dict[str, Any]
) -> None:
    """Strictly validate the photo avatar block against models.json regions.

    Provider-shaped values (api-version, feature name, attribute enums, preview
    host) are observations of an undocumented surface: they stay reviewable
    catalog data, and the shapes below keep a typo from becoming a live route.
    """
    label = "photoAvatars"
    _exact_keys(
        errors,
        block,
        allowed=(
            "displayName",
            "description",
            "homeRegion",
            "homeDataZone",
            "apiVersion",
            "requiredFeature",
            "projectSuffix",
            "baseModel",
            "billingModelId",
            "liveBillingModelId",
            "promptMaxChars",
            "attributes",
            "preview",
        ),
        label=label,
    )
    if not isinstance(block, dict):
        return

    def text_matches(field: str, pattern: str) -> None:
        value = block.get(field)
        _require(
            errors,
            isinstance(value, str) and re.fullmatch(pattern, value) is not None,
            f"{label}.{field} must match {pattern}",
        )

    for field in ("displayName", "description"):
        value = block.get(field)
        _require(
            errors,
            isinstance(value, str) and 0 < len(value.strip()) <= 400,
            f"{label}.{field} must be a nonempty string",
        )
    text_matches("homeRegion", r"[a-z][a-z0-9]{1,30}")
    home = block.get("homeRegion")
    _require(
        errors,
        isinstance(home, str) and home in regions,
        f"{label}.homeRegion must be a region in infra/models.json (got {home!r})",
    )
    expected_zone = (regions.get(home) or {}).get("dataZone") if isinstance(home, str) else None
    _require(
        errors,
        block.get("homeDataZone") in {"US", "EU"} and block.get("homeDataZone") == expected_zone,
        f"{label}.homeDataZone must equal the models.json dataZone of {home!r} ({expected_zone!r})",
    )
    text_matches("apiVersion", r"[0-9]{4}-[0-9]{2}-[0-9]{2}(-preview)?")
    text_matches("requiredFeature", r"[A-Za-z][A-Za-z0-9]{1,63}")
    _require(
        errors,
        block.get("projectSuffix") == "_PhotoAvatar",
        f"{label}.projectSuffix must be _PhotoAvatar",
    )
    text_matches("baseModel", r"[a-z][a-z0-9.-]{1,31}")
    text_matches("billingModelId", r"[a-z][a-z0-9-]{1,62}")
    text_matches("liveBillingModelId", r"[a-z][a-z0-9-]{1,62}")
    _require(
        errors,
        block.get("liveBillingModelId") != block.get("billingModelId"),
        f"{label}.liveBillingModelId must name a meter distinct from billingModelId",
    )
    prompt_max = block.get("promptMaxChars")
    _require(
        errors,
        type(prompt_max) is int and 1 <= prompt_max <= 4000,
        f"{label}.promptMaxChars must be an integer from 1 to 4000",
    )

    attributes = block.get("attributes", {})
    _exact_keys(
        errors, attributes, allowed=PHOTO_AVATAR_ATTRIBUTE_NAMES, label=f"{label}.attributes"
    )
    for name in PHOTO_AVATAR_ATTRIBUTE_NAMES:
        values = attributes.get(name) if isinstance(attributes, dict) else None
        _require(
            errors,
            isinstance(values, list)
            and 1 <= len(values) <= 16
            and len(set(values)) == len(values)
            and all(
                isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,39}", value)
                for value in values
            ),
            f"{label}.attributes.{name} must be 1-16 unique identifier values",
        )

    preview = block.get("preview", {})
    _exact_keys(
        errors,
        preview,
        allowed=("host", "maxBytes", "maxDimension", "contentTypes"),
        label=f"{label}.preview",
    )
    if isinstance(preview, dict):
        host = preview.get("host")
        _require(
            errors,
            isinstance(host, str)
            and re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", host) is not None,
            f"{label}.preview.host must be one exact Azure Blob host",
        )
        max_bytes = preview.get("maxBytes")
        _require(
            errors,
            type(max_bytes) is int and 1024 <= max_bytes <= 16 * 1024 * 1024,
            f"{label}.preview.maxBytes must be an integer from 1024 to 16777216",
        )
        max_dimension = preview.get("maxDimension")
        _require(
            errors,
            type(max_dimension) is int and 64 <= max_dimension <= 8192,
            f"{label}.preview.maxDimension must be an integer from 64 to 8192",
        )
        content_types = preview.get("contentTypes")
        _require(
            errors,
            isinstance(content_types, list)
            and bool(content_types)
            and len(set(content_types)) == len(content_types)
            and set(content_types) <= {"application/octet-stream", "image/png"},
            f"{label}.preview.contentTypes must be a subset of application/octet-stream and image/png",
        )


def _load_regions(models: dict[str, Any] | None) -> dict[str, Any]:
    source = models if models is not None else json.loads(MODELS_SOURCE.read_text(encoding="utf-8"))
    regions = source.get("regions", {})
    return regions if isinstance(regions, dict) else {}


def build_catalog(raw: dict[str, Any], models: dict[str, Any] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    _exact_keys(
        errors,
        raw,
        allowed=("$schema", "_comment", "defaultProviderId", "providers", "photoAvatars"),
        label="catalog",
    )
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
    _require(errors, "photoAvatars" in raw, "photoAvatars block is required")
    if "photoAvatars" in raw:
        _validate_photo_avatars(errors, raw.get("photoAvatars"), _load_regions(models))

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


def render_ts(catalog: dict[str, Any]) -> str:
    rendered = json.dumps(catalog, indent=2, ensure_ascii=False)
    return (
        "/* GENERATED by scripts/gen-voice-provider-catalog.py from infra/voice-providers.json. Do not edit by hand. */\n"
        "\n"
        f"export const voiceProviderCatalog = {rendered} as const;\n"
        "\n"
        "export type VoiceProviderCatalog = typeof voiceProviderCatalog;\n"
        "export type VoiceProvider = VoiceProviderCatalog[\"providers\"][number];\n"
        "export type VoiceProviderId = VoiceProvider[\"id\"];\n"
        "export const DEFAULT_VOICE_PROVIDER_ID = voiceProviderCatalog.defaultProviderId;\n"
    )


def render_speech_voice_live_policy(catalog: dict[str, Any]) -> str:
    speech = next(
        provider
        for provider in catalog["providers"]
        if provider["id"] == "speech_voice_live"
    )
    model_ids = tuple(model["id"] for model in speech["managedModels"])
    default_model_id = speech["defaultManagedModelId"]
    api_version = speech["managedModels"][0]["apiVersion"]
    allowed_expression = " ||\n              ".join(
        f'"{model_id}".Equals(model, StringComparison.Ordinal)'
        for model_id in model_ids
    )
    reject_expression = (
        "@{\n"
        '            string model = context.Request.Url.Query.GetValueOrDefault("model", "");\n'
        "            return !String.IsNullOrWhiteSpace(model) &&\n"
        "              !(\n"
        f"              {allowed_expression}\n"
        "              );\n"
        "          }"
    )
    model_expression = (
        "@(String.IsNullOrWhiteSpace("
        'context.Request.Url.Query.GetValueOrDefault("model", "")) '
        f'? "{default_model_id}" : '
        'context.Request.Url.Query.GetValueOrDefault("model", ""))'
    )
    return (
        "<policies>\n"
        "  <inbound>\n"
        "    <base />\n"
        "    <!-- GENERATED by scripts/gen-voice-provider-catalog.py from the managed-model catalog. -->\n"
        "    <choose>\n"
        f'      <when condition="{html.escape(reject_expression, quote=True)}">\n'
        "        <return-response>\n"
        '          <set-status code="400" reason="Voice Live model is not in the AI4IA catalog" />\n'
        "        </return-response>\n"
        "      </when>\n"
        "    </choose>\n"
        '    <set-query-parameter name="model" exists-action="override">\n'
        f"      <value>{html.escape(model_expression, quote=False)}</value>\n"
        "    </set-query-parameter>\n"
        '    <set-query-parameter name="api-version" exists-action="override">\n'
        f"      <value>{api_version}</value>\n"
        "    </set-query-parameter>\n"
        '    <set-query-parameter name="deployment" exists-action="delete" />\n'
        '    <set-query-parameter name="subscription-key" exists-action="delete" />\n'
        '    <set-query-parameter name="api-key" exists-action="delete" />\n'
        '    <set-query-parameter name="agent_id" exists-action="delete" />\n'
        '    <set-query-parameter name="project_id" exists-action="delete" />\n'
        '    <set-backend-service base-url="{{speech-voice-live-wss-endpoint}}/voice-live/realtime" />\n'
        '    <set-header name="x-correlation-id" exists-action="override">\n'
        "      <value>@(context.RequestId.ToString())</value>\n"
        "    </set-header>\n"
        '    <set-header name="Ocp-Apim-Subscription-Key" exists-action="delete" />\n'
        '    <set-header name="api-key" exists-action="delete" />\n'
        '    <set-header name="Authorization" exists-action="delete" />\n'
        '    <set-header name="X-AI4IA-App-Id" exists-action="delete" />\n'
        '    <set-header name="X-AI4IA-User-Id" exists-action="delete" />\n'
        '    <set-header name="X-UserProfile" exists-action="delete" />\n'
        '    <authentication-managed-identity resource="{{speech-voice-live-mi-audience}}" />\n'
        "  </inbound>\n"
        "  <backend>\n"
        "    <base />\n"
        "  </backend>\n"
        "  <outbound>\n"
        "    <base />\n"
        "  </outbound>\n"
        "  <on-error>\n"
        "    <base />\n"
        '    <set-header name="x-correlation-id" exists-action="override">\n'
        "      <value>@(context.RequestId.ToString())</value>\n"
        "    </set-header>\n"
        "    <return-response>\n"
        '      <set-status code="502" reason="Speech Voice Live handshake failed" />\n'
        "    </return-response>\n"
        "  </on-error>\n"
        "</policies>\n"
    )


def _csharp_strings(values: list[str]) -> str:
    return "new string[] { " + ", ".join(f'"{value}"' for value in values) + " }"


def render_photo_avatar_policy(catalog: dict[str, Any]) -> str:
    """Render the exact-operation photo avatar API policy from the catalog.

    One inbound validation expression admits only the six AI4IA operations on
    the API-scoped subscription, AI4IA-issued avatar ids, no caller query and,
    for create, a bounded JSON object of catalog-enumerated properties. APIM
    re-serializes that validated object, owns the avatar project body and the
    provider path/api-version, strips caller and proxy headers before managed
    identity, and forwards exactly once: no retry element exists.
    """
    block = catalog["photoAvatars"]
    attributes = block["attributes"]
    api_version = block["apiVersion"]
    suffix = block["projectSuffix"]
    operations = "\n".join(
        f'        operation == "{name}" ? "{method} {route}" :'
        for name, method, route in PHOTO_AVATAR_OPERATIONS
    )
    allowed = "\n".join(
        f'            item.Name == "{name}" ? {_csharp_strings(attributes[name])} :'
        for name in PHOTO_AVATAR_ATTRIBUTE_NAMES
    )
    validation = (
        "@{\n"
        f'    if (context.Api.Path != "{PHOTO_AVATAR_API_PATH}" ||\n'
        "        context.Subscription == null ||\n"
        f'        context.Subscription.Id != "{PHOTO_AVATAR_SUBSCRIPTION_PLACEHOLDER}") {{ return ""; }}\n'
        "    string operation = context.Operation.Id;\n"
        "    string expected =\n"
        f"{operations}\n"
        '        "";\n'
        "    string query = context.Request.OriginalUrl.QueryString;\n"
        '    if (expected.Length == 0 || (!string.IsNullOrEmpty(query) && query != "?")) { return ""; }\n'
        "    var match = System.Text.RegularExpressions.Regex.Match(context.Request.OriginalUrl.Path,\n"
        f'        @"\\A/{PHOTO_AVATAR_API_PATH}/(features|project|photoavatars/({PHOTO_AVATAR_ID_PATTERN}))\\z");\n'
        '    if (!match.Success) { return ""; }\n'
        '    string route = match.Groups[2].Success ? "/photoavatars" : "/" + match.Groups[1].Value;\n'
        '    if (context.Request.Method + " " + route != expected) { return ""; }\n'
        '    if (match.Groups[2].Success && (!context.Request.MatchedParameters.ContainsKey("avatarId") ||\n'
        '        context.Request.MatchedParameters["avatarId"] != match.Groups[2].Value)) { return ""; }\n'
        "    byte[] bytes = context.Request.Body == null\n"
        "        ? new byte[0] : context.Request.Body.As<byte[]>(preserveContent: true);\n"
        '    if (operation != "photo-avatar-create") { return bytes.Length == 0 ? operation : ""; }\n'
        f'    if (bytes.Length == 0 || bytes.Length > {PHOTO_AVATAR_BODY_MAX_BYTES}) {{ return ""; }}\n'
        "    JObject body;\n"
        "    try { body = JObject.Parse(System.Text.Encoding.UTF8.GetString(bytes)); }\n"
        '    catch (Exception) { return ""; }\n'
        '    JObject properties = body["properties"] as JObject;\n'
        '    if (body.Properties().Count() != 1 || properties == null) { return ""; }\n'
        '    JToken prompt = properties["prompt"];\n'
        '    if (prompt == null || prompt.Type != JTokenType.String) { return ""; }\n'
        "    string text = prompt.Value<string>();\n"
        f'    if (text.Trim().Length == 0 || text.Length > {block["promptMaxChars"]}) {{ return ""; }}\n'
        "    foreach (JProperty item in properties.Properties()) {\n"
        '        if (item.Name == "prompt") { continue; }\n'
        "        string[] allowed =\n"
        f"{allowed}\n"
        "            null;\n"
        "        if (allowed == null || item.Value.Type != JTokenType.String ||\n"
        '            !allowed.Contains(item.Value.Value<string>())) { return ""; }\n'
        "    }\n"
        "    return operation;\n"
        "}"
    )
    canonical = (
        "@{\n"
        "    JObject body = JObject.Parse(System.Text.Encoding.UTF8.GetString(\n"
        "        context.Request.Body.As<byte[]>(preserveContent: true)));\n"
        '    JObject properties = (JObject)body["properties"];\n'
        "    JObject canonical = new JObject();\n"
        f'    foreach (string name in {_csharp_strings(["prompt", *PHOTO_AVATAR_ATTRIBUTE_NAMES])}) {{\n'
        "        JToken value = properties[name];\n"
        "        if (value != null) { canonical.Add(new JProperty(name, value.Value<string>())); }\n"
        "    }\n"
        '    return new JObject(new JProperty("properties", canonical)).ToString(\n'
        "        Newtonsoft.Json.Formatting.None);\n"
        "}"
    )
    correlation = (
        "@{\n"
        '      var supplied = context.Request.Headers.GetValueOrDefault("x-correlation-id", "");\n'
        "      return !String.IsNullOrWhiteSpace(supplied) && supplied.Length <= 128\n"
        "        ? supplied\n"
        "        : context.RequestId.ToString();\n"
        "    }"
    )

    def selected(name: str) -> str:
        return html.escape(
            f'@((string)context.Variables["photoAvatarOperation"] == "{name}")', quote=True
        )

    project_uri = f"/CustomAvatar/projects/{{{{photo-avatar-project}}}}{suffix}?api-version={api_version}"
    avatar_uri = (
        f"/CustomAvatar/projects/{{{{photo-avatar-project}}}}{suffix}/photoavatars/{{avatarId}}"
        f"?api-version={api_version}"
    )
    json_header = (
        '        <set-header name="Content-Type" exists-action="override">\n'
        "          <value>application/json</value>\n"
        "        </set-header>\n"
    )
    strips = "".join(
        f'    <set-header name="{name}" exists-action="delete" />\n'
        for name in PHOTO_AVATAR_STRIPPED_HEADERS
    )
    empty_check = html.escape(
        '@(string.IsNullOrEmpty((string)context.Variables["photoAvatarOperation"]))', quote=True
    )
    correlation_value = html.escape(
        '@((string)context.Variables["ai4ia-correlation-id"])', quote=False
    )
    return (
        "<policies>\n"
        "  <inbound>\n"
        "    <!-- GENERATED by scripts/gen-voice-provider-catalog.py from the photoAvatars catalog.\n"
        "         Exact operations and no base inheritance. gateway.bicep binds the subscription id\n"
        "         to the API-scoped proxy subscription; no caller supplies it. -->\n"
        f'    <set-variable name="photoAvatarOperation" value="{html.escape(validation, quote=True)}" />\n'
        "    <choose>\n"
        f'      <when condition="{empty_check}">\n'
        "        <return-response>\n"
        '          <set-status code="400" reason="Invalid photo avatar request" />\n'
        '          <set-header name="Content-Type" exists-action="override">\n'
        "            <value>application/json</value>\n"
        "          </set-header>\n"
        '          <set-body>{"error":{"code":"invalid_photo_avatar_request"}}</set-body>\n'
        "        </return-response>\n"
        "      </when>\n"
        "    </choose>\n"
        "    <choose>\n"
        f'      <when condition="{selected("photo-avatar-features")}">\n'
        f'        <rewrite-uri template="/customavatar/features/?api-version={api_version}" copy-unmatched-params="false" />\n'
        "      </when>\n"
        f'      <when condition="{selected("photo-avatar-project-read")}">\n'
        f'        <rewrite-uri template="{project_uri}" copy-unmatched-params="false" />\n'
        "      </when>\n"
        f'      <when condition="{selected("photo-avatar-project-create")}">\n'
        f'        <rewrite-uri template="{project_uri}" copy-unmatched-params="false" />\n'
        f"{json_header}"
        "        <set-body>{\"kind\":\"PhotoAvatar\",\"foundryProjectName\":\"{{photo-avatar-project}}\"}</set-body>\n"
        "      </when>\n"
        f'      <when condition="{selected("photo-avatar-create")}">\n'
        f'        <rewrite-uri template="{avatar_uri}" copy-unmatched-params="false" />\n'
        f"{json_header}"
        f"        <set-body>{html.escape(canonical, quote=False)}</set-body>\n"
        "      </when>\n"
        "      <otherwise>\n"
        f'        <rewrite-uri template="{avatar_uri}" copy-unmatched-params="false" />\n'
        "      </otherwise>\n"
        "    </choose>\n"
        f'    <set-backend-service base-url="{{{{foundry-{block["homeRegion"]}-endpoint}}}}" />\n'
        f'    <set-variable name="ai4ia-correlation-id" value="{html.escape(correlation, quote=True)}" />\n'
        '    <set-header name="x-correlation-id" exists-action="override">\n'
        f"      <value>{correlation_value}</value>\n"
        "    </set-header>\n"
        '    <set-header name="x-apim-request-id" exists-action="override">\n'
        "      <value>@(context.RequestId.ToString())</value>\n"
        "    </set-header>\n"
        f"{strips}"
        '    <set-query-parameter name="subscription-key" exists-action="delete" />\n'
        '    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />\n'
        "  </inbound>\n"
        "  <backend>\n"
        f'    <forward-request timeout="{PHOTO_AVATAR_FORWARD_TIMEOUT_SECONDS}" http-version="1" '
        'follow-redirects="false" buffer-request-body="true" buffer-response="true" '
        'fail-on-error-status-code="false" />\n'
        "  </backend>\n"
        "  <outbound>\n"
        '    <set-header name="S7PREQUEUE" exists-action="delete" />\n'
        '    <set-header name="x-correlation-id" exists-action="override">\n'
        f"      <value>{correlation_value}</value>\n"
        "    </set-header>\n"
        "  </outbound>\n"
        "  <on-error>\n"
        "    <return-response>\n"
        '      <set-status code="502" reason="Photo avatar gateway error" />\n'
        '      <set-header name="Content-Type" exists-action="override">\n'
        "        <value>application/json</value>\n"
        "      </set-header>\n"
        '      <set-body>{"error":{"code":"photo_avatar_gateway_error"}}</set-body>\n'
        "    </return-response>\n"
        "  </on-error>\n"
        "</policies>\n"
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
    parser = build_parser(__doc__)
    args = parser.parse_args()

    raw = json.loads(SOURCE.read_text(encoding="utf-8"))
    catalog = build_catalog(raw)

    api_json = json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
    web_ts = render_ts(catalog) + "\n"
    policy_xml = render_speech_voice_live_policy(catalog)
    photo_avatar_policy_xml = render_photo_avatar_policy(catalog)

    if args.check:
        _write_if_needed(API_TARGET, api_json, check=True)
        _write_if_needed(WEB_TARGET, web_ts, check=True)
        _write_if_needed(POLICY_TARGET, policy_xml, check=True)
        _write_if_needed(PHOTO_AVATAR_POLICY_TARGET, photo_avatar_policy_xml, check=True)
        print("voice provider catalog and policy outputs are up to date.")
        return 0

    _write_if_needed(API_TARGET, api_json, check=False)
    _write_if_needed(WEB_TARGET, web_ts, check=False)
    _write_if_needed(POLICY_TARGET, policy_xml, check=False)
    _write_if_needed(PHOTO_AVATAR_POLICY_TARGET, photo_avatar_policy_xml, check=False)
    print(
        f"Wrote {API_TARGET.relative_to(REPO_ROOT)}, {WEB_TARGET.relative_to(REPO_ROOT)}, "
        f"{POLICY_TARGET.relative_to(REPO_ROOT)}, "
        f"and {PHOTO_AVATAR_POLICY_TARGET.relative_to(REPO_ROOT)} "
        f"({len(catalog['providers'])} providers)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
