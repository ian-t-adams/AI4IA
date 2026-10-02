"""Speech Voice Live MAI voices and selectable input transcription.

The catalog may offer reviewed alternatives to a managed model's own
transcription default and public-preview MAI voices. The relay honors a
browser's pick only when the catalog offers it for that session's managed
model; anything else keeps the governed default.
"""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from ai4ia_api.routers.realtime import normalize_speech_client_frame
from ai4ia_api.voice_provider_catalog import (
    EXPECTED_SPEECH_MANAGED_MODEL_IDS,
    SpeechVoiceProvider,
    load_voice_provider_catalog,
)

# Spelled out rather than read from the catalog, so a catalog typo cannot
# agree with itself.
MAI_VOICES = (
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
)
MAI_TRANSCRIPTION = "mai-transcribe-2"
DEFAULT_VOICE = "en-US-Ava:DragonHDLatestNeural"


def _provider() -> SpeechVoiceProvider:
    provider = load_voice_provider_catalog().get("speech_voice_live")
    assert isinstance(provider, SpeechVoiceProvider)
    return provider


def _session(
    provider: SpeechVoiceProvider, model_id: str, requested: dict[str, object]
) -> dict[str, object]:
    managed = provider.get_managed_model(model_id)
    assert managed is not None
    frame = json.dumps({"type": "session.update", "session": requested})
    out = normalize_speech_client_frame(frame, provider, managed)
    assert out is not None
    return json.loads(out)["session"]


def test_packaged_catalog_marks_exactly_the_mai_voices_preview_and_keeps_defaults():
    provider = _provider()
    voices = provider.capabilities.voices
    assert voices.default == DEFAULT_VOICE
    assert provider.sessionDefaults.voice == DEFAULT_VOICE
    assert tuple(voices.previewOptions) == MAI_VOICES
    assert set(MAI_VOICES) <= set(voices.options)
    assert [option.model_dump() for option in provider.capabilities.inputTranscription.options] == [
        {
            "model": MAI_TRANSCRIPTION,
            "displayName": "MAI Transcribe 2",
            "preview": True,
            "profiles": ["native_audio", "azure_speech_chain"],
        }
    ]
    defaults = {
        managed.id: managed.inputTranscription.model for managed in provider.managedModels
    }
    assert defaults == {
        "gpt-realtime": "gpt-4o-transcribe",
        "gpt-realtime-mini": "gpt-4o-transcribe",
        "gpt-4.1": "azure-speech",
        "gpt-4.1-mini": "azure-speech",
        "gpt-5-mini": "azure-speech",
        "gpt-5.1": "azure-speech",
    }


def _mutated(change) -> dict:
    raw = _provider().model_dump()
    change(raw)
    return raw


def _set_option(field: str, value: object):
    def change(raw: dict) -> None:
        raw["capabilities"]["inputTranscription"]["options"][0][field] = value

    return change


@pytest.mark.parametrize(
    ("label", "change"),
    [
        (
            "default voice marked preview",
            lambda raw: raw["capabilities"]["voices"]["previewOptions"].append(DEFAULT_VOICE),
        ),
        (
            "preview voice outside options",
            lambda raw: raw["capabilities"]["voices"]["previewOptions"].append(
                "en-GB-Emily:MAI-Voice-2.1-Flash"
            ),
        ),
        (
            "duplicate preview voice",
            lambda raw: raw["capabilities"]["voices"]["previewOptions"].append(MAI_VOICES[0]),
        ),
        ("floating alias", _set_option("model", "mai-transcribe")),
        ("streaming product id", _set_option("model", "MAI-Transcribe-2-Streaming")),
        ("unknown profile", _set_option("profiles", ["agent"])),
        ("empty profiles", _set_option("profiles", [])),
        ("endpoint field", _set_option("endpoint", "https://attacker.example")),
        (
            "duplicate option",
            lambda raw: raw["capabilities"]["inputTranscription"]["options"].append(
                copy.deepcopy(raw["capabilities"]["inputTranscription"]["options"][0])
            ),
        ),
        ("missing capability", lambda raw: raw["capabilities"].pop("inputTranscription")),
        ("missing preview list", lambda raw: raw["capabilities"]["voices"].pop("previewOptions")),
    ],
)
def test_runtime_catalog_rejects_unreviewed_voice_and_transcription_shapes(label, change):
    SpeechVoiceProvider.model_validate(_mutated(lambda raw: None))
    with pytest.raises(ValidationError):
        SpeechVoiceProvider.model_validate(_mutated(change))


@pytest.mark.parametrize("model_id", EXPECTED_SPEECH_MANAGED_MODEL_IDS)
def test_selected_mai_transcription_reaches_every_managed_model(model_id):
    provider = _provider()
    session = _session(
        provider,
        model_id,
        {"input_audio_transcription": {"model": MAI_TRANSCRIPTION, "language": "xx-XX"}},
    )
    assert session["input_audio_transcription"] == {
        "model": MAI_TRANSCRIPTION,
        "language": "en-US",
    }


@pytest.mark.parametrize("model_id", EXPECTED_SPEECH_MANAGED_MODEL_IDS)
@pytest.mark.parametrize(
    "requested",
    [
        "mai-transcribe",
        "MAI-Transcribe-2",
        "MAI-Transcribe-2-Streaming",
        "whisper-1",
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe",
        "azure-speech",
        7,
        None,
    ],
)
def test_unoffered_transcription_keeps_the_managed_default(model_id, requested):
    provider = _provider()
    managed = provider.get_managed_model(model_id)
    assert managed is not None
    session = _session(
        provider, model_id, {"input_audio_transcription": {"model": requested}}
    )
    # A model's own default is the only other accepted value; another
    # profile's default is never a selectable option.
    assert session["input_audio_transcription"] == {
        "model": managed.inputTranscription.model,
        "language": "en-US",
    }


def test_transcription_option_is_limited_to_the_profiles_it_lists():
    raw = _mutated(
        lambda data: data["capabilities"]["inputTranscription"]["options"][0].__setitem__(
            "profiles", ["azure_speech_chain"]
        )
    )
    chain_only = SpeechVoiceProvider.model_validate(raw)
    native = chain_only.get_managed_model("gpt-realtime")
    chain = chain_only.get_managed_model("gpt-4.1")
    assert native is not None and chain is not None
    assert chain_only.input_transcription_models(native) == ("gpt-4o-transcribe",)
    assert chain_only.input_transcription_models(chain) == ("azure-speech", MAI_TRANSCRIPTION)

    requested = {"input_audio_transcription": {"model": MAI_TRANSCRIPTION}}
    assert _session(chain_only, "gpt-realtime", requested)["input_audio_transcription"] == {
        "model": "gpt-4o-transcribe",
        "language": "en-US",
    }
    assert _session(chain_only, "gpt-4.1", requested)["input_audio_transcription"] == {
        "model": MAI_TRANSCRIPTION,
        "language": "en-US",
    }


@pytest.mark.parametrize("voice", MAI_VOICES)
def test_catalog_mai_voices_pass_through_in_both_voice_forms(voice):
    provider = _provider()
    for requested in (voice, {"type": "azure-standard", "name": voice}):
        session = _session(provider, "gpt-4.1", {"voice": requested})
        assert session["voice"] == {"type": "azure-standard", "name": voice, "locale": "en-US"}


@pytest.mark.parametrize(
    "voice",
    [
        "en-US-Harper:MAI-Voice-2-Flash",
        "en-US-Harper:MAI-Voice-2.1-flash",
        "en-GB-Emily:MAI-Voice-2.1-Flash",
        "MAI-Voice-2.1-Flash",
        {"type": "azure-custom", "name": "en-US-Harper:MAI-Voice-2.1-Flash"},
    ],
)
def test_unreviewed_mai_voices_keep_the_default_voice(voice):
    session = _session(_provider(), "gpt-realtime", {"voice": voice})
    assert session["voice"] == {"type": "azure-standard", "name": DEFAULT_VOICE, "locale": "en-US"}
