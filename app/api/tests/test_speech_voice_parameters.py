"""Speech Voice Live: more catalog voices, a speaking rate and an HD voice temperature.

The relay still rebuilds the session voice from the catalog. A numeric client
``rate`` is clamped to ``capabilities.speakingRate`` and sent as a decimal
string; a numeric voice ``temperature`` is clamped to
``capabilities.hdVoiceTemperature`` and sent only for a Dragon HD voice
(``voices.hdOptions``). That is not the model's sampling temperature. With
neither set, every frame is byte for byte what the relay sent before, as
captured from its normalizer at b2ec3ef7 in ``speech_session_frames.json``.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai4ia_api.routers.realtime import (
    DEV_SUBPROTOCOL,
    SpeechParameterEvidence,
    normalize_speech_client_frame,
)
from ai4ia_api.voice_provider_catalog import (
    EXPECTED_SPEECH_MANAGED_MODEL_IDS,
    SpeechVoiceProvider,
    load_voice_provider_catalog,
)
from tests.test_realtime_api import GENERIC_VOICE, OPTED_IN_ECHO, _origin, _speech_client

CAPTURED = json.loads(
    Path(__file__).with_name("speech_session_frames.json").read_text(encoding="utf-8")
)

# Spelled out rather than read from the catalog, so a catalog edit cannot agree
# with itself. Names as Microsoft Learn lists them (reviewed 2026-10-03).
DEFAULT_VOICE = "en-US-Ava:DragonHDLatestNeural"
HD_VOICES = (
    DEFAULT_VOICE,
    "en-US-Adam:DragonHDLatestNeural",
    "en-US-Alloy:DragonHDLatestNeural",
    "en-US-Andrew:DragonHDLatestNeural",
    "en-US-Andrew2:DragonHDLatestNeural",
    "en-US-Aria:DragonHDLatestNeural",
    "en-US-Brian:DragonHDLatestNeural",
    "en-US-Davis:DragonHDLatestNeural",
    "en-US-Emma:DragonHDLatestNeural",
    "en-US-Emma2:DragonHDLatestNeural",
    "en-US-Jenny:DragonHDLatestNeural",
    "en-US-Nova:DragonHDLatestNeural",
    "en-US-Phoebe:DragonHDLatestNeural",
    "en-US-Serena:DragonHDLatestNeural",
    "en-US-Steffan:DragonHDLatestNeural",
)
MULTILINGUAL_VOICES = (
    "en-US-AdamMultilingualNeural",
    "en-US-AlloyTurboMultilingualNeural",
    "en-US-AmandaMultilingualNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-AvaMultilingualNeural",
    "en-US-BrandonMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-ChristopherMultilingualNeural",
    "en-US-CoraMultilingualNeural",
    "en-US-DavisMultilingualNeural",
    "en-US-DerekMultilingualNeural",
    "en-US-DustinMultilingualNeural",
    "en-US-EchoTurboMultilingualNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-EvelynMultilingualNeural",
    "en-US-FableTurboMultilingualNeural",
    "en-US-JennyMultilingualNeural",
    "en-US-LewisMultilingualNeural",
    "en-US-LolaMultilingualNeural",
    "en-US-NancyMultilingualNeural",
    "en-US-NovaTurboMultilingualNeural",
    "en-US-OnyxTurboMultilingualNeural",
    "en-US-PhoebeMultilingualNeural",
    "en-US-RyanMultilingualNeural",
    "en-US-SamuelMultilingualNeural",
    "en-US-SerenaMultilingualNeural",
    "en-US-ShimmerTurboMultilingualNeural",
    "en-US-SteffanMultilingualNeural",
)
NEURAL_VOICES = ("en-US-AvaNeural", "en-US-AndrewNeural")
MAI_VOICES = tuple(
    f"en-US-{name}:{model}"
    for model in ("MAI-Voice-2.1-Flash", "MAI-Voice-2.1")
    for name in ("Ethan", "Grant", "Harper", "Iris", "Jasper", "Olivia", "Sage")
)
ALL_VOICES = (*HD_VOICES, *MULTILINGUAL_VOICES, *NEURAL_VOICES, *MAI_VOICES)
# What the catalog offered before this change, in its order then.
PREVIOUS_VOICES = (
    DEFAULT_VOICE,
    "en-US-AvaNeural",
    "en-US-AndrewNeural",
    "en-US-Brian:DragonHDLatestNeural",
    "en-US-Emma:DragonHDLatestNeural",
    "en-US-Jenny:DragonHDLatestNeural",
    *MAI_VOICES,
)
ADDED_VOICES = tuple(voice for voice in ALL_VOICES if voice not in PREVIOUS_VOICES)
# Learn names a voice Voice Live in eastus2 has no reason to accept (preview, a
# region it is not offered in, absent from the HD list, another family, or the
# wrong case), plus a near miss.
UNLISTED_VOICES = (
    "en-US-Bree:DragonHDLatestNeural",
    "en-US-Jane:DragonHDLatestNeural",
    "en-US-Andrew3:DragonHDLatestNeural",
    "en-US-Ava3:DragonHDLatestNeural",
    "en-us-MultiTalker-Ava-Andrew:DragonHDLatestNeural",
    "en-US-AshTurboMultilingualNeural",
    "en-US-Andrew:DragonHDOmniLatestNeural",
    "en-US-Tiana:DragonHDFlashLatestNeural",
    "en-US-AlloyMultilingualNeural",
    "en-us-Adam:DragonHDLatestNeural",
    "en-US-GuyNeural",
    "en-US-AvaMultilingualNeuralHD",
)
PLAIN_VOICE = {"type": "azure-standard", "name": DEFAULT_VOICE, "locale": "en-US"}


def _provider() -> SpeechVoiceProvider:
    provider = load_voice_provider_catalog().get("speech_voice_live")
    assert isinstance(provider, SpeechVoiceProvider)
    return provider


def _session(
    model_id: str,
    session: dict[str, Any],
    *,
    provider: SpeechVoiceProvider | None = None,
    echo: bool = False,
    evidence: SpeechParameterEvidence | None = None,
) -> str:
    provider = provider or _provider()
    managed = provider.get_managed_model(model_id)
    assert managed is not None
    reference = provider.capabilities.echoCancellation.clientReference if echo else None
    out = normalize_speech_client_frame(
        json.dumps({"type": "session.update", "session": session}), provider, managed,
        echo_reference=reference, evidence=evidence,
    )
    assert out is not None
    return out


def _voice(voice: object, model_id: str = "gpt-4.1", **kwargs: Any) -> dict[str, Any]:
    return json.loads(_session(model_id, {"voice": voice}, **kwargs))["session"]["voice"]


# --------------------------------------------------------------------------- #
# The catalog contract.
# --------------------------------------------------------------------------- #


def test_catalog_offers_the_reviewed_voices_by_family_and_keeps_every_earlier_one():
    voices = _provider().capabilities.voices
    assert tuple(voices.options) == ALL_VOICES
    assert voices.default == DEFAULT_VOICE == voices.options[0]
    assert tuple(voices.hdOptions) == HD_VOICES
    assert tuple(voices.previewOptions) == MAI_VOICES
    assert set(PREVIOUS_VOICES) <= set(voices.options)
    assert len(ADDED_VOICES) == 39 and len(ALL_VOICES) == len(set(ALL_VOICES)) == 59
    assert not set(UNLISTED_VOICES) & set(voices.options)


def test_catalog_voice_parameter_ranges_are_the_documented_ones():
    capabilities = _provider().capabilities
    assert (capabilities.speakingRate.min, capabilities.speakingRate.max) == (0.5, 1.5)
    assert (capabilities.hdVoiceTemperature.min, capabilities.hdVoiceTemperature.max) == (
        0.0, 1.0,
    )


def test_live_config_advertises_the_voices_and_voice_parameter_ranges():
    c = _speech_client()
    try:
        response = c.get("/api/voice/live/config")
        assert response.status_code == 200
        capabilities = response.json()["providers"][1]["capabilities"]
        assert tuple(capabilities["voices"]["options"]) == ALL_VOICES
        assert tuple(capabilities["voices"]["hdOptions"]) == HD_VOICES
        assert capabilities["speakingRate"] == {"min": 0.5, "max": 1.5}
        assert capabilities["hdVoiceTemperature"] == {"min": 0.0, "max": 1.0}
    finally:
        c.__exit__(None, None, None)


def _mutated(change) -> dict[str, Any]:
    raw = _provider().model_dump()
    change(raw["capabilities"])
    return raw


@pytest.mark.parametrize(
    ("label", "change"),
    [
        ("rate wider than documented", lambda caps: caps["speakingRate"].update(max=2.0)),
        ("rate slower than documented", lambda caps: caps["speakingRate"].update(min=0.25)),
        ("temperature above documented", lambda caps: caps["hdVoiceTemperature"].update(max=2)),
        ("negative temperature", lambda caps: caps["hdVoiceTemperature"].update(min=-0.5)),
        ("inverted range", lambda caps: caps["speakingRate"].update(min=1.2, max=0.8)),
        ("empty range", lambda caps: caps["speakingRate"].update(min=1.0, max=1.0)),
        ("boolean bound", lambda caps: caps["hdVoiceTemperature"].update(max=True)),
        ("string bound", lambda caps: caps["speakingRate"].update(max="1.5")),
        ("NaN bound", lambda caps: caps["speakingRate"].update(min=math.nan)),
        ("extra range field", lambda caps: caps["speakingRate"].update(step=0.1)),
        ("missing rate", lambda caps: caps.pop("speakingRate")),
        ("missing voice temperature", lambda caps: caps.pop("hdVoiceTemperature")),
        (
            "HD voice outside options",
            lambda caps: caps["voices"]["hdOptions"].append("en-US-Bree:DragonHDLatestNeural"),
        ),
        ("duplicate HD voice", lambda caps: caps["voices"]["hdOptions"].append(DEFAULT_VOICE)),
        ("missing HD list", lambda caps: caps["voices"].pop("hdOptions")),
    ],
)
def test_runtime_catalog_rejects_unreviewed_voice_parameter_shapes(label, change):
    SpeechVoiceProvider.model_validate(_mutated(lambda caps: None))  # control
    with pytest.raises(ValidationError):
        SpeechVoiceProvider.model_validate(_mutated(change))


def test_runtime_catalog_may_narrow_a_range_and_the_relay_clamps_to_it():
    narrowed = SpeechVoiceProvider.model_validate(
        _mutated(lambda caps: caps["speakingRate"].update(min=0.8, max=1.2))
    )
    voice = {"type": "azure-standard", "name": DEFAULT_VOICE}
    assert _voice({**voice, "rate": 1.5}, provider=narrowed)["rate"] == "1.2"
    assert _voice({**voice, "rate": 0.5}, provider=narrowed)["rate"] == "0.8"
    # Control: the packaged catalog allows both.
    assert _voice({**voice, "rate": 1.5})["rate"] == "1.5"
    assert _voice({**voice, "rate": 0.5})["rate"] == "0.5"


# --------------------------------------------------------------------------- #
# Unset: the frames the relay sent before, byte for byte.
# --------------------------------------------------------------------------- #


def _browser_session(model_id: str) -> dict[str, Any]:
    """What the browser sent by default before this change."""
    managed = _provider().get_managed_model(model_id)
    assert managed is not None
    return {
        "voice": dict(PLAIN_VOICE),
        "input_audio_transcription": {
            "model": managed.inputTranscription.model, "language": "en-US",
        },
        "turn_detection": {
            "type": "azure_semantic_vad", "interrupt_response": True, "auto_truncate": False,
        },
        "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
        "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
    }


# The inputs the frames were captured with; none sets a usable rate, and the only
# numeric voice temperature is on a voice that is not a Dragon HD voice.
CAPTURED_INPUTS = {
    "empty": lambda model_id: {},
    "browser": _browser_session,
    "dropped_voice_fields": lambda model_id: {
        "voice": {
            "type": "azure-standard", "name": "en-US-AndrewNeural", "locale": "en-US",
            "rate": "1.2", "temperature": True, "endpoint_id": "custom", "style": "cheerful",
        },
        "temperature": 0.7,
    },
    "non_hd_voice_temperature": lambda model_id: {
        "voice": {"type": "azure-standard", "name": "en-US-AndrewNeural", "temperature": 0.5},
    },
}


def test_captured_frames_cover_every_managed_model_and_input():
    assert tuple(CAPTURED["models"]) == EXPECTED_SPEECH_MANAGED_MODEL_IDS
    for inputs in CAPTURED["models"].values():
        assert tuple(inputs) == tuple(CAPTURED_INPUTS)
        for pair in inputs.values():
            assert set(pair) == {"plain", "echo"}
            assert all(name in CAPTURED["frames"] for name in pair.values())


@pytest.mark.parametrize("echo", [False, True], ids=["plain", "echo"])
@pytest.mark.parametrize("input_name", tuple(CAPTURED_INPUTS))
@pytest.mark.parametrize("model_id", EXPECTED_SPEECH_MANAGED_MODEL_IDS)
def test_unset_voice_parameters_keep_the_captured_frame(model_id, input_name, echo):
    expected = CAPTURED["frames"][CAPTURED["models"][model_id][input_name][
        "echo" if echo else "plain"
    ]]
    session = CAPTURED_INPUTS[input_name](model_id)
    assert _session(model_id, session, echo=echo) == expected


@pytest.mark.parametrize("model_id", EXPECTED_SPEECH_MANAGED_MODEL_IDS)
def test_set_voice_parameters_change_only_the_voice(model_id):
    # Control for the captured frames: the same browser session with both set
    # differs from its captured frame in the voice alone.
    for echo, kind in ((False, "plain"), (True, "echo")):
        session = _browser_session(model_id)
        session["voice"].update(rate=1.2, temperature=0.4)
        changed = json.loads(_session(model_id, session, echo=echo))
        captured = json.loads(CAPTURED["frames"][CAPTURED["models"][model_id]["browser"][kind]])
        assert changed["session"].pop("voice") == {**PLAIN_VOICE, "temperature": 0.4, "rate": "1.2"}
        assert captured["session"].pop("voice") == PLAIN_VOICE
        assert changed == captured


# --------------------------------------------------------------------------- #
# Speaking rate.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("requested", "sent"),
    [
        (1.2, "1.2"),
        (1, "1.0"),
        (0.5, "0.5"),
        (1.5, "1.5"),
        (0.75, "0.75"),
        (1.234, "1.23"),
        (0.1, "0.5"),
        (-1, "0.5"),
        (9, "1.5"),
        (1e308, "1.5"),
    ],
)
def test_rate_is_clamped_to_the_catalog_and_sent_as_a_decimal_string(requested, sent):
    voice = _voice({"type": "azure-standard", "name": DEFAULT_VOICE, "rate": requested})
    assert voice == {**PLAIN_VOICE, "rate": sent}


@pytest.mark.parametrize(
    "requested", ["1.2", True, False, None, [1.2], {"value": 1.2}, math.nan, math.inf, 10**400],
)
def test_only_a_finite_numeric_rate_is_sent(requested):
    voice = {"type": "azure-standard", "name": DEFAULT_VOICE, "rate": requested}
    assert _voice(voice) == PLAIN_VOICE
    # Control: the same voice with a number carries the rate.
    assert _voice({**voice, "rate": 1.1}) == {**PLAIN_VOICE, "rate": "1.1"}


@pytest.mark.parametrize("name", ALL_VOICES)
def test_rate_applies_to_every_catalog_voice(name):
    voice = _voice({"type": "azure-standard", "name": name, "rate": 0.9})
    assert voice == {"type": "azure-standard", "name": name, "locale": "en-US", "rate": "0.9"}


def test_a_string_voice_carries_no_rate():
    assert _voice("en-US-AvaMultilingualNeural") == {
        "type": "azure-standard", "name": "en-US-AvaMultilingualNeural", "locale": "en-US",
    }


# --------------------------------------------------------------------------- #
# HD voice temperature.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ALL_VOICES)
def test_voice_temperature_reaches_only_a_dragon_hd_voice(name):
    voice = _voice({"type": "azure-standard", "name": name, "temperature": 0.6})
    expected = {"type": "azure-standard", "name": name, "locale": "en-US"}
    if name in HD_VOICES:
        expected["temperature"] = 0.6
    assert voice == expected


@pytest.mark.parametrize(
    ("requested", "sent"),
    [(0.8, 0.8), (0, 0.0), (1, 1.0), (0.05, 0.05), (-0.5, 0.0), (3, 1.0), (1e308, 1.0)],
)
def test_voice_temperature_is_clamped_to_the_catalog(requested, sent):
    voice = _voice({"type": "azure-standard", "name": DEFAULT_VOICE, "temperature": requested})
    assert voice == {**PLAIN_VOICE, "temperature": sent}


@pytest.mark.parametrize("requested", ["0.8", True, None, [0.8], math.nan, -math.inf, 10**400])
def test_only_a_finite_numeric_voice_temperature_is_sent(requested):
    voice = {"type": "azure-standard", "name": DEFAULT_VOICE, "temperature": requested}
    assert _voice(voice) == PLAIN_VOICE
    # Control: the same HD voice with a number carries the temperature.
    assert _voice({**voice, "temperature": 0.3}) == {**PLAIN_VOICE, "temperature": 0.3}


@pytest.mark.parametrize("model_id", ["gpt-5.6-terra", "gpt-5.1", "gpt-realtime", "gpt-4.1"])
def test_voice_temperature_is_separate_from_the_model_temperature(model_id):
    evidence = SpeechParameterEvidence()
    session = json.loads(_session(model_id, {
        "temperature": 0.7,
        "voice": {"type": "azure-standard", "name": DEFAULT_VOICE, "temperature": 0.2},
    }, evidence=evidence))["session"]
    # The HD voice keeps its own temperature whether or not the model has sampling.
    assert session["voice"]["temperature"] == 0.2
    sampling = _provider().get_managed_model(model_id)
    assert sampling is not None
    if sampling.samplingSupported:
        assert session["temperature"] == 0.7 and evidence.temperature_omitted == 0
    else:
        assert "temperature" not in session and evidence.temperature_omitted == 1


# --------------------------------------------------------------------------- #
# Voice selection.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ADDED_VOICES)
def test_every_added_voice_is_accepted_in_both_voice_forms(name):
    expected = {"type": "azure-standard", "name": name, "locale": "en-US"}
    assert _voice(name) == expected
    assert _voice({"type": "azure-standard", "name": name}) == expected
    assert _voice({"name": f"  {name} "}) == expected


@pytest.mark.parametrize("name", UNLISTED_VOICES)
def test_an_unlisted_voice_still_falls_back_to_the_default(name):
    assert _voice(name) == PLAIN_VOICE
    assert _voice({"type": "azure-standard", "name": name}) == PLAIN_VOICE
    # The parameters apply to the voice actually sent: the HD default.
    assert _voice({"type": "azure-standard", "name": name, "rate": 1.3, "temperature": 0.9}) == {
        **PLAIN_VOICE, "temperature": 0.9, "rate": "1.3",
    }


def test_a_custom_voice_type_keeps_the_default_voice():
    voice = {"type": "azure-custom", "name": "en-US-Andrew:DragonHDLatestNeural", "rate": 1.3}
    assert _voice(voice) == {**PLAIN_VOICE, "rate": "1.3"}


def test_response_create_carries_no_voice_parameters():
    provider = _provider()
    frame = json.dumps({"type": "response.create", "response": {
        "voice": {"type": "azure-standard", "name": DEFAULT_VOICE, "rate": "1.4", "temperature": 1},
    }})
    assert normalize_speech_client_frame(
        frame, provider, provider.get_managed_model("gpt-4.1"),
    ) == '{"type": "response.create"}'


# --------------------------------------------------------------------------- #
# The live relay: voice parameters with the echo reference, tools and persona.
# --------------------------------------------------------------------------- #


def _live_session(model_id: str, voice: dict[str, Any]) -> dict[str, Any]:
    c = _speech_client(realtime_tools_enabled=True)
    try:
        connector = c.app.state.realtime_connector
        with c.websocket_connect(
            f"/api/voice/live?provider=speech_voice_live&model={model_id}&echoRef=client&tools=1",
            subprotocols=[DEV_SUBPROTOCOL, "voiceparamsuser"], headers=_origin(),
        ) as ws:
            ws.send_text(json.dumps({"type": "session.update", "session": {
                "voice": voice, "temperature": 0.5, "parallel_tool_calls": True,
                "tools": [{"type": "function", "name": "untrusted"}],
            }}))
            ws.receive_text()
        return json.loads(connector.upstream.sent_text[0])["session"]
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("model_id", "name", "temperature"),
    [
        ("gpt-5.6-terra", "en-US-Andrew2:DragonHDLatestNeural", 0.4),
        ("gpt-realtime", "en-US-EvelynMultilingualNeural", None),
        ("gpt-4.1", "en-US-Harper:MAI-Voice-2.1-Flash", None),
    ],
)
def test_live_session_update_carries_voice_parameters_with_echo_tools_and_persona(
    model_id, name, temperature,
):
    voice = {"type": "azure-standard", "name": name, "rate": 1.25, "temperature": 0.4}
    session = _live_session(model_id, voice)
    expected = {"type": "azure-standard", "name": name, "locale": "en-US", "rate": "1.25"}
    if temperature is not None:
        expected = {**expected, "temperature": temperature}
    assert session["voice"] == expected
    # The echo reference, the tool bridge and the persona all still apply.
    assert session["parallel_tool_calls"] is False
    assert session["input_audio_echo_cancellation"] == OPTED_IN_ECHO
    assert session["tools"] and all(tool["name"] != "untrusted" for tool in session["tools"])
    assert session["tool_choice"] == "auto"
    assert session["instructions"] == GENERIC_VOICE
    # Control: without the parameters only the voice differs.
    plain = _live_session(model_id, {"type": "azure-standard", "name": name})
    assert plain.pop("voice") == {"type": "azure-standard", "name": name, "locale": "en-US"}
    session.pop("voice")
    assert session == plain
