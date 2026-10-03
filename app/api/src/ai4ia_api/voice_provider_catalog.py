"""Typed loader for the packaged voice-provider catalog."""
from __future__ import annotations

import json
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Sequence, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    computed_field,
    model_serializer,
    model_validator,
)

AZURE_OPENAI_PROVIDER_ID = "azure_openai"
SPEECH_VOICE_LIVE_PROVIDER_ID = "speech_voice_live"
EXPECTED_PROVIDER_IDS = (AZURE_OPENAI_PROVIDER_ID, SPEECH_VOICE_LIVE_PROVIDER_ID)
EXPECTED_SPEECH_MANAGED_MODEL_IDS = (
    "gpt-realtime",
    "gpt-realtime-mini",
    "gpt-realtime-1.5",
    "gpt-realtime-2.1",
    "gpt-realtime-2.1-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-5-mini",
    "gpt-5.1",
    "gpt-5.2",
    "gpt-5.4",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
DEFAULT_VOICE_PROVIDER_ID = AZURE_OPENAI_PROVIDER_ID

_PACKAGED = Path(__file__).resolve().parent / "data" / "voice_provider_catalog.json"


class VoiceProviderTransport(str, Enum):
    websocket = "websocket"


class VoiceProviderVoices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    default: str
    options: list[str]


class SpeechVoiceProviderVoices(VoiceProviderVoices):
    # Public-preview voices (no SLA), labelled as such by the browser.
    previewOptions: list[str]

    @model_validator(mode="after")
    def validate_preview_options(self) -> "SpeechVoiceProviderVoices":
        if len(set(self.previewOptions)) != len(self.previewOptions):
            raise ValueError("Speech preview voices must be unique.")
        if not set(self.previewOptions) <= set(self.options):
            raise ValueError("Speech preview voices must be catalog voice options.")
        if self.default in self.previewOptions:
            raise ValueError("The default Speech voice must not be a preview voice.")
        return self


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


class SpeechEchoClientReference(BaseModel):
    """Opt-in, preview Live-Reference AEC: the client supplies the echo reference.

    A session that asks for it connects at ``apiVersion`` with the ``features``
    flag and sends ``channels``-channel interleaved PCM16: the microphone, then
    what the client plays. Every other session keeps its managed model's version.
    """

    model_config = ConfigDict(extra="forbid")

    preview: Literal[True]
    apiVersion: Literal["2026-07-15"]
    features: Literal["client_ec_reference:true"]
    channels: Literal[2]


class SpeechEchoCancellation(VoiceProviderSimpleOptions):
    # Absent means the client echo reference is unavailable: the relay refuses it.
    clientReference: SpeechEchoClientReference | None = None


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


class AzureOpenAIVoiceProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voices: VoiceProviderVoices
    inputTranscription: VoiceProviderInputTranscription
    turnDetection: VoiceProviderTurnDetection
    interruption: VoiceProviderInterruption
    customVoice: VoiceProviderCustomVoice


SpeechManagedModelProfile: TypeAlias = Literal["native_audio", "azure_speech_chain"]


class SpeechInputTranscriptionOption(BaseModel):
    """A reviewed alternative to a managed model's own transcription default."""

    model_config = ConfigDict(extra="forbid")

    model: Literal["mai-transcribe-2"]
    displayName: str
    preview: bool
    profiles: list[SpeechManagedModelProfile] = Field(min_length=1)


class SpeechVoiceProviderInputTranscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: list[SpeechInputTranscriptionOption]

    @model_validator(mode="after")
    def validate_unique_models(self) -> "SpeechVoiceProviderInputTranscription":
        models = [option.model for option in self.options]
        if len(set(models)) != len(models):
            raise ValueError("Speech transcription options must be unique.")
        return self


class SpeechVoiceProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voices: SpeechVoiceProviderVoices
    inputTranscription: SpeechVoiceProviderInputTranscription
    turnDetection: VoiceProviderTurnDetection
    noiseSuppression: VoiceProviderSimpleOptions
    echoCancellation: SpeechEchoCancellation
    locale: VoiceProviderSimpleOptions
    interruption: VoiceProviderInterruption
    customVoice: VoiceProviderCustomVoice


class AzureOpenAIVoiceProviderSessionDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice: str
    inputTranscription: str
    turnDetection: str
    interruptResponse: bool
    autoTruncate: bool


class SpeechVoiceProviderSessionDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice: str
    turnDetection: str
    locale: str
    noiseSuppression: str
    echoCancellation: str
    interruptResponse: bool
    autoTruncate: bool


class VoiceProviderModelCatalogFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    runtimeEnabled: Literal[True] = True


class VoiceProviderModelCatalogRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sourceJson: str
    filter: VoiceProviderModelCatalogFilter
    defaultModelId: str


class VoiceProviderOpenAIManagedTranscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai"]
    model: Literal["gpt-4o-transcribe"]


class VoiceProviderAzureSpeechManagedTranscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["azure_speech"]
    model: Literal["azure-speech"]


# The ReasoningEffort enum of the pinned 2026-04-10 Voice Live reference.
SpeechReasoningEffort: TypeAlias = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class _VoiceProviderManagedModelBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    displayName: str
    description: str
    # Named as in infra/models.json: whether the model honours ``temperature``.
    # False for the GPT-5.x reasoning models, so the relay omits any client value.
    samplingSupported: StrictBool
    # The server-owned ``reasoning_effort`` the relay sends on every
    # session.update. None sends nothing; a client value is never forwarded.
    reasoningEffort: SpeechReasoningEffort | None = None
    apiVersion: Literal["2026-04-10"]
    initialRegion: Literal["eastus2"]
    audioFormat: Literal["pcm16"]
    sampleRateHz: Literal[24000]

    @model_validator(mode="after")
    def validate_reasoning_effort(self) -> "_VoiceProviderManagedModelBase":
        if self.reasoningEffort is not None and self.samplingSupported:
            raise ValueError("Only a managed model without sampling takes a reasoning effort.")
        return self

    @model_serializer(mode="wrap")
    def omit_undeclared_effort(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # The generated catalog omits an undeclared effort; so does the public view.
        result = handler(self)
        if result.get("reasoningEffort") is None:
            result.pop("reasoningEffort", None)
        return result


class VoiceProviderNativeAudioManagedModel(_VoiceProviderManagedModelBase):
    id: Literal[
        "gpt-realtime",
        "gpt-realtime-mini",
        "gpt-realtime-1.5",
        "gpt-realtime-2.1",
        "gpt-realtime-2.1-mini",
    ]
    profile: Literal["native_audio"]
    inputTranscription: VoiceProviderOpenAIManagedTranscription


class VoiceProviderAzureSpeechChainManagedModel(_VoiceProviderManagedModelBase):
    id: Literal[
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-5-mini",
        "gpt-5.1",
        "gpt-5.2",
        "gpt-5.4",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    profile: Literal["azure_speech_chain"]
    inputTranscription: VoiceProviderAzureSpeechManagedTranscription


VoiceProviderManagedModel: TypeAlias = Annotated[
    VoiceProviderNativeAudioManagedModel | VoiceProviderAzureSpeechChainManagedModel,
    Field(discriminator="profile"),
]


class _VoiceProviderBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    displayName: str
    displayLabel: str
    description: str
    transport: VoiceProviderTransport
    endpointPath: str


class _VoiceProviderPublicBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    displayName: str
    displayLabel: str
    description: str
    transport: VoiceProviderTransport


class AzureOpenAIVoiceProviderPublic(_VoiceProviderPublicBase):
    id: Literal["azure_openai"]
    selectionMode: Literal["deployment_catalog"]
    sessionDefaults: AzureOpenAIVoiceProviderSessionDefaults
    capabilities: AzureOpenAIVoiceProviderCapabilities


class SpeechVoiceProviderPublic(_VoiceProviderPublicBase):
    id: Literal["speech_voice_live"]
    selectionMode: Literal["managed_model_catalog"]
    defaultManagedModelId: Literal["gpt-realtime"]
    managedModels: list[VoiceProviderManagedModel]
    sessionDefaults: SpeechVoiceProviderSessionDefaults
    capabilities: SpeechVoiceProviderCapabilities


VoiceProviderPublic: TypeAlias = Annotated[
    AzureOpenAIVoiceProviderPublic | SpeechVoiceProviderPublic,
    Field(discriminator="id"),
]


class AzureOpenAIVoiceProvider(_VoiceProviderBase):
    id: Literal["azure_openai"]
    selectionMode: Literal["deployment_catalog"]
    modelCatalogRef: VoiceProviderModelCatalogRef
    sessionDefaults: AzureOpenAIVoiceProviderSessionDefaults
    capabilities: AzureOpenAIVoiceProviderCapabilities

    def public_view(self) -> AzureOpenAIVoiceProviderPublic:
        return AzureOpenAIVoiceProviderPublic.model_validate(
            self.model_dump(exclude={"endpointPath", "modelCatalogRef"})
        )


class SpeechVoiceProvider(_VoiceProviderBase):
    id: Literal["speech_voice_live"]
    selectionMode: Literal["managed_model_catalog"]
    defaultManagedModelId: Literal["gpt-realtime"]
    managedModels: list[VoiceProviderManagedModel]
    sessionDefaults: SpeechVoiceProviderSessionDefaults
    capabilities: SpeechVoiceProviderCapabilities

    @model_validator(mode="after")
    def validate_managed_models(self) -> "SpeechVoiceProvider":
        model_ids = [model.id for model in self.managedModels]
        if tuple(model_ids) != EXPECTED_SPEECH_MANAGED_MODEL_IDS:
            raise ValueError(
                "Speech Voice Live managed models must match the governed catalog."
            )
        return self

    def get_managed_model(self, model_id: str) -> VoiceProviderManagedModel | None:
        return next((model for model in self.managedModels if model.id == model_id), None)

    def input_transcription_models(
        self, managed_model: VoiceProviderManagedModel
    ) -> tuple[str, ...]:
        """The transcription models a session on ``managed_model`` may use.

        The managed model's own default comes first; reviewed catalog options
        follow only when they support that model's profile.
        """
        return (
            managed_model.inputTranscription.model,
            *(
                option.model
                for option in self.capabilities.inputTranscription.options
                if managed_model.profile in option.profiles
            ),
        )

    def public_view(self) -> SpeechVoiceProviderPublic:
        return SpeechVoiceProviderPublic.model_validate(
            self.model_dump(exclude={"endpointPath"})
        )


VoiceProvider: TypeAlias = Annotated[
    AzureOpenAIVoiceProvider | SpeechVoiceProvider,
    Field(discriminator="id"),
]


class VoiceLiveRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaultProviderId: str
    enabledProviderIds: list[str]
    providers: list[VoiceProviderPublic]
    openaiRealtimeProtocol: Literal["preview", "ga"] = "preview"


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


def _load_raw(explicit_path: str | None) -> dict[str, object]:
    path = Path(explicit_path) if explicit_path else _PACKAGED
    if not path.exists():
        raise FileNotFoundError(f"No voice provider catalog found at {path}.")
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return {
            key: value
            for key, value in raw.items()
            if isinstance(key, str) and key in ("defaultProviderId", "providers")
        }
    raise ValueError("Voice provider catalog must be a JSON object.")


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
