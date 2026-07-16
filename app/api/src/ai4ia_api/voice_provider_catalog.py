"""Typed loader for the packaged voice-provider catalog."""
from __future__ import annotations

import json
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, computed_field

AZURE_OPENAI_PROVIDER_ID = "azure_openai"
SPEECH_VOICE_LIVE_PROVIDER_ID = "speech_voice_live"
EXPECTED_PROVIDER_IDS = (AZURE_OPENAI_PROVIDER_ID, SPEECH_VOICE_LIVE_PROVIDER_ID)
DEFAULT_VOICE_PROVIDER_ID = AZURE_OPENAI_PROVIDER_ID

_PACKAGED = Path(__file__).resolve().parent / "data" / "voice_provider_catalog.json"


class VoiceProviderTransport(str, Enum):
    websocket = "websocket"


class VoiceProviderSelectionMode(str, Enum):
    deployment_catalog = "deployment_catalog"
    fixed_managed_model = "fixed_managed_model"


class VoiceProviderVoices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    default: str
    options: list[str]


class VoiceProviderInputTranscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    default: str
    options: list[str]


class VoiceProviderTurnDetection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str
    options: list[str]


class VoiceProviderSimpleOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str
    options: list[str]


class VoiceProviderInterruption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interruptResponse: bool
    autoTruncate: bool


class VoiceProviderCustomVoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    allowEndpointIds: bool
    allowLexicons: bool
    allowPersonalVoice: bool


class VoiceProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voices: VoiceProviderVoices
    inputTranscription: VoiceProviderInputTranscription
    turnDetection: VoiceProviderTurnDetection
    noiseSuppression: VoiceProviderSimpleOptions | None = None
    echoCancellation: VoiceProviderSimpleOptions | None = None
    locale: VoiceProviderSimpleOptions | None = None
    interruption: VoiceProviderInterruption
    customVoice: VoiceProviderCustomVoice


class VoiceProviderSessionDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice: str
    inputTranscription: str
    turnDetection: str
    locale: str | None = None
    noiseSuppression: str | None = None
    echoCancellation: str | None = None
    interruptResponse: bool
    autoTruncate: bool


class VoiceProviderModelCatalogFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str


class VoiceProviderModelCatalogRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sourceJson: str
    filter: VoiceProviderModelCatalogFilter
    defaultModelId: str


class VoiceProviderManagedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modelId: str
    apiVersion: str
    initialRegion: str
    audioFormat: str
    sampleRateHz: int


class VoiceProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    displayName: str
    displayLabel: str
    description: str
    transport: VoiceProviderTransport
    selectionMode: VoiceProviderSelectionMode
    endpointPath: str
    modelCatalogRef: VoiceProviderModelCatalogRef | None = None
    managedModel: VoiceProviderManagedModel | None = None
    sessionDefaults: VoiceProviderSessionDefaults
    capabilities: VoiceProviderCapabilities

    def public_view(self) -> "VoiceProviderPublic":
        data = self.model_dump(exclude={"endpointPath", "modelCatalogRef"})
        return VoiceProviderPublic.model_validate(data)


class VoiceProviderPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    displayName: str
    displayLabel: str
    description: str
    transport: VoiceProviderTransport
    selectionMode: VoiceProviderSelectionMode
    managedModel: VoiceProviderManagedModel | None = None
    sessionDefaults: VoiceProviderSessionDefaults
    capabilities: VoiceProviderCapabilities


class VoiceLiveRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaultProviderId: str
    enabledProviderIds: list[str]
    providers: list[VoiceProviderPublic]


class VoiceProviderCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaultProviderId: str
    providers: list[VoiceProvider]

    @computed_field
    @property
    def providerIds(self) -> list[str]:
        return [provider.id for provider in self.providers]

    def get(self, provider_id: str) -> VoiceProvider | None:
        return next((provider for provider in self.providers if provider.id == provider_id), None)

    def allowed_provider_ids(self, allowlist: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        allowed: list[str] = []
        for provider_id in allowlist:
            if provider_id in seen:
                continue
            provider = self.get(provider_id)
            if provider is None:
                continue
            seen.add(provider_id)
            allowed.append(provider_id)
        return allowed

    def public_providers(self, provider_ids: Sequence[str]) -> list[VoiceProviderPublic]:
        out: list[VoiceProviderPublic] = []
        for provider_id in provider_ids:
            provider = self.get(provider_id)
            if provider is not None:
                out.append(provider.public_view())
        return out


def _load_raw(explicit_path: str | None) -> dict[str, Any]:
    path = Path(explicit_path) if explicit_path else _PACKAGED
    if not path.exists():
        raise FileNotFoundError(f"No voice provider catalog found at {path}.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return {k: raw[k] for k in ("defaultProviderId", "providers") if k in raw}
    return raw


def _validate_catalog(catalog: VoiceProviderCatalog) -> VoiceProviderCatalog:
    ids = tuple(catalog.providerIds)
    if catalog.defaultProviderId != DEFAULT_VOICE_PROVIDER_ID:
        raise ValueError(
            "Voice provider catalog defaultProviderId must remain azure_openai."
        )
    if ids != EXPECTED_PROVIDER_IDS:
        raise ValueError(
            "Voice provider catalog must contain exactly azure_openai and speech_voice_live."
        )
    return catalog


@lru_cache
def load_voice_provider_catalog(explicit_path: str | None = None) -> VoiceProviderCatalog:
    raw = _load_raw(explicit_path)
    return _validate_catalog(VoiceProviderCatalog.model_validate(raw))
