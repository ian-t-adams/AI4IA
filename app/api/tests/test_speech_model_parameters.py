"""Speech Voice Live: the catalog owns each managed model's sampling and reasoning.

The GPT-5.x managed models are reasoning models without temperature, and
gpt-5.2, gpt-5.4 and the GPT-5.6 models run at a server-owned
``reasoning_effort`` of ``none``. The relay omits a client temperature where the
catalog says the model has no sampling, sends only the catalog's reasoning
effort, and records both content-free for the completion telemetry. Every other
session.update is byte for byte what it was before.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from ai4ia_api.agents.tools import redact
from ai4ia_api.model_traits import supports_sampling
from ai4ia_api.receipts import build_receipt
from ai4ia_api.routers import realtime as realtime_module
from ai4ia_api.routers.realtime import (
    DEV_SUBPROTOCOL,
    VOICE_RELAY_RECEIPT_NOTES,
    SpeechParameterEvidence,
    UpstreamMessage,
    normalize_speech_client_frame,
)
from ai4ia_api.voice_provider_catalog import (
    EXPECTED_SPEECH_MANAGED_MODEL_IDS,
    SpeechVoiceProvider,
    load_voice_provider_catalog,
)
from tests.test_realtime_api import (
    GENERIC_VOICE,
    OPTED_IN_ECHO,
    _attach_completion_capture,
    _completion_payloads,
    _origin,
    _speech_client,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "PRIVATE-PROVIDER-MESSAGE-SENTINEL"

# Spelled out rather than read from the catalog, so a catalog edit cannot agree
# with itself.
NO_SAMPLING = ("gpt-5-mini", "gpt-5.1", "gpt-5.2", "gpt-5.4", "gpt-5.6-terra", "gpt-5.6-luna")
REASONING_NONE = ("gpt-5.2", "gpt-5.4", "gpt-5.6-terra", "gpt-5.6-luna")

# What the relay sent before this change, captured from that normalizer. A model
# with sampling, or a session without a temperature, must still send exactly this.
GPT51_FRAME = (
    '{"type": "session.update", "session": {"voice": {"type": "azure-standard", '
    '"name": "en-US-Ava:DragonHDLatestNeural", "locale": "en-US"}, '
    '"input_audio_transcription": {"model": "azure-speech", "language": "en-US"}, '
    '"turn_detection": {"type": "azure_semantic_vad", "create_response": true, '
    '"interrupt_response": true, "auto_truncate": false}, "input_audio_format": "pcm16", '
    '"output_audio_format": "pcm16", "input_audio_sampling_rate": 24000, '
    '"modalities": ["text", "audio"], "input_audio_noise_reduction": '
    '{"type": "azure_deep_noise_suppression"}, "input_audio_echo_cancellation": '
    '{"type": "server_echo_cancellation"}}}'
)
REALTIME_TEMPERATURE_FRAME = (
    '{"type": "session.update", "session": {"voice": {"type": "azure-standard", '
    '"name": "en-US-Ava:DragonHDLatestNeural", "locale": "en-US"}, '
    '"input_audio_transcription": {"model": "gpt-4o-transcribe", "language": "en-US"}, '
    '"turn_detection": {"type": "azure_semantic_vad", "create_response": true, '
    '"interrupt_response": true, "auto_truncate": false}, "input_audio_format": "pcm16", '
    '"output_audio_format": "pcm16", "input_audio_sampling_rate": 24000, '
    '"modalities": ["text", "audio"], "temperature": 0.7, "input_audio_noise_reduction": '
    '{"type": "azure_deep_noise_suppression"}, "input_audio_echo_cancellation": '
    '{"type": "server_echo_cancellation"}}}'
)
GPT41_TEMPERATURE_FRAME = REALTIME_TEMPERATURE_FRAME.replace("gpt-4o-transcribe", "azure-speech")


def _provider() -> SpeechVoiceProvider:
    provider = load_voice_provider_catalog().get("speech_voice_live")
    assert isinstance(provider, SpeechVoiceProvider)
    return provider


def _session_update(
    model_id: str,
    session: dict[str, object],
    *,
    evidence: SpeechParameterEvidence | None = None,
    echo: bool = False,
) -> str:
    provider = _provider()
    managed = provider.get_managed_model(model_id)
    assert managed is not None
    reference = provider.capabilities.echoCancellation.clientReference if echo else None
    out = normalize_speech_client_frame(
        json.dumps({"type": "session.update", "session": session}), provider, managed,
        echo_reference=reference, evidence=evidence,
    )
    assert out is not None
    return out


# --------------------------------------------------------------------------- #
# The catalog contract.
# --------------------------------------------------------------------------- #


def test_catalog_marks_exactly_the_gpt5_models_without_sampling_and_their_effort():
    models = _provider().managedModels
    assert tuple(model.id for model in models if not model.samplingSupported) == NO_SAMPLING
    assert {model.id: model.reasoningEffort for model in models if model.reasoningEffort} == (
        dict.fromkeys(REASONING_NONE, "none")
    )


@pytest.mark.parametrize("model_id", EXPECTED_SPEECH_MANAGED_MODEL_IDS)
def test_catalog_sampling_flag_matches_the_http_model_traits(model_id):
    # One rule for both surfaces: HTTP chat strips sampling for the same models.
    managed = _provider().get_managed_model(model_id)
    assert managed is not None
    assert managed.samplingSupported is supports_sampling(model_id)


def test_every_reasoning_effort_is_one_the_model_was_probed_to_accept():
    models = json.loads((REPO_ROOT / "infra" / "models.json").read_text(encoding="utf-8"))
    probed = {row["name"]: row.get("reasoningEffort") for row in models["catalog"]}
    checked: list[str] = []
    for managed in _provider().managedModels:
        if managed.reasoningEffort is None or managed.id not in probed:
            continue
        assert managed.reasoningEffort in (probed[managed.id] or []), managed.id
        checked.append(managed.id)
    # Non-vacuous: every model with an effort has a probed row to check against.
    assert tuple(checked) == REASONING_NONE


@pytest.mark.parametrize(
    ("label", "change"),
    [
        ("missing sampling flag", lambda model: model.pop("samplingSupported")),
        ("integer sampling flag", lambda model: model.update(samplingSupported=0)),
        ("string sampling flag", lambda model: model.update(samplingSupported="false")),
        ("effort outside the enum", lambda model: model.update(reasoningEffort="max")),
        ("capitalized effort", lambda model: model.update(reasoningEffort="None")),
        ("empty effort", lambda model: model.update(reasoningEffort="")),
        ("effort on a model with sampling", lambda model: model.update(samplingSupported=True)),
    ],
)
def test_runtime_catalog_rejects_unreviewed_parameter_shapes(label, change):
    raw = _provider().model_dump()
    SpeechVoiceProvider.model_validate(raw)  # control: the packaged shape is accepted
    terra = next(model for model in raw["managedModels"] if model["id"] == "gpt-5.6-terra")
    change(terra)
    with pytest.raises(ValidationError):
        SpeechVoiceProvider.model_validate(raw)


def test_public_view_matches_the_generated_catalog_shape():
    public = _provider().public_view().model_dump()
    for model in public["managedModels"]:
        assert ("reasoningEffort" in model) is (model["id"] in REASONING_NONE)
        assert model["samplingSupported"] is (model["id"] not in NO_SAMPLING)
    packaged = json.loads(
        (REPO_ROOT / "app/api/src/ai4ia_api/data/voice_provider_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    assert public["managedModels"] == packaged["providers"][1]["managedModels"]


# --------------------------------------------------------------------------- #
# The relay's rewrite of each session.update.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("model_id", EXPECTED_SPEECH_MANAGED_MODEL_IDS)
def test_client_temperature_is_omitted_exactly_where_the_model_has_no_sampling(model_id):
    evidence = SpeechParameterEvidence()
    session = json.loads(_session_update(model_id, {"temperature": 0.5}, evidence=evidence))[
        "session"
    ]
    if model_id in NO_SAMPLING:
        assert "temperature" not in session
        assert evidence.temperature_omitted == 1
    else:
        assert session["temperature"] == 0.5
        assert evidence.temperature_omitted == 0


@pytest.mark.parametrize("requested", ["0.5", True, None, [0.5]])
def test_only_a_numeric_client_temperature_counts_as_omitted(requested):
    evidence = SpeechParameterEvidence()
    session = json.loads(
        _session_update("gpt-5.6-terra", {"temperature": requested}, evidence=evidence)
    )["session"]
    assert "temperature" not in session and evidence.temperature_omitted == 0
    # Control: the same session with a number counts once per session.update.
    _session_update("gpt-5.6-terra", {"temperature": 0.5}, evidence=evidence)
    _session_update("gpt-5.6-terra", {"temperature": 1}, evidence=evidence)
    assert evidence.temperature_omitted == 2


@pytest.mark.parametrize("model_id", EXPECTED_SPEECH_MANAGED_MODEL_IDS)
@pytest.mark.parametrize("requested", [None, "high", "none", "minimal", {"effort": "low"}])
def test_reasoning_effort_is_only_ever_the_catalogs(model_id, requested):
    session_in: dict[str, object] = {}
    if requested is not None:
        session_in = {"reasoning_effort": requested, "reasoning": {"effort": "high"}}
    evidence = SpeechParameterEvidence()
    session = json.loads(_session_update(model_id, session_in, evidence=evidence))["session"]
    assert "reasoning" not in session
    if model_id in REASONING_NONE:
        assert session["reasoning_effort"] == "none"
        assert evidence.reasoning_effort == "none"
    else:
        assert "reasoning_effort" not in session
        assert evidence.reasoning_effort is None


@pytest.mark.parametrize("model_id", ["gpt-5.6-terra", "gpt-5.1", "gpt-realtime"])
def test_response_create_carries_no_sampling_or_reasoning_override(model_id):
    provider = _provider()
    frame = json.dumps({"type": "response.create", "response": {
        "temperature": 1.0, "reasoning_effort": "high", "reasoning": {"effort": "high"},
    }})
    assert normalize_speech_client_frame(
        frame, provider, provider.get_managed_model(model_id),
    ) == '{"type": "response.create"}'


def test_frames_for_models_with_sampling_or_without_a_temperature_are_unchanged():
    assert _session_update("gpt-5.1", {}) == GPT51_FRAME
    assert _session_update("gpt-5.1", {"temperature": 0.5}) == GPT51_FRAME
    assert _session_update("gpt-realtime", {"temperature": 0.7}) == REALTIME_TEMPERATURE_FRAME
    assert _session_update("gpt-4.1", {"temperature": 0.7}) == GPT41_TEMPERATURE_FRAME
    # A reasoning model differs from that frame only by the relay-owned effort.
    terra = json.loads(_session_update("gpt-5.6-terra", {"temperature": 0.5}))
    assert terra["session"].pop("reasoning_effort") == "none"
    assert json.dumps(terra) == GPT51_FRAME


def test_echo_reference_composes_with_the_model_parameters():
    requested = {"temperature": 0.5, "reasoning_effort": "high", "parallel_tool_calls": True}
    evidence = SpeechParameterEvidence()
    opted = json.loads(
        _session_update("gpt-5.6-terra", requested, evidence=evidence, echo=True)
    )["session"]
    assert opted["reasoning_effort"] == "none"
    assert "temperature" not in opted
    assert opted["parallel_tool_calls"] is False
    assert opted["input_audio_echo_cancellation"] == OPTED_IN_ECHO
    assert (evidence.reasoning_effort, evidence.temperature_omitted) == ("none", 1)
    # Control: without the reference, only the reference's own fields differ.
    plain = json.loads(_session_update("gpt-5.6-terra", requested))["session"]
    del opted["parallel_tool_calls"]
    opted["input_audio_echo_cancellation"] = {"type": "server_echo_cancellation"}
    assert opted == plain


@pytest.mark.parametrize(
    ("model_id", "effort", "temperature"),
    [
        ("gpt-5.6-terra", "none", None),
        ("gpt-5.1", None, None),
        ("gpt-realtime", None, 0.5),
        ("gpt-4.1", None, 0.5),
    ],
)
def test_live_session_update_composes_parameters_with_echo_tools_and_persona(
    monkeypatch, model_id, effort, temperature,
):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        realtime_module, "emit_custom_event", lambda name, attrs: events.append((name, attrs)),
    )
    c = _speech_client(realtime_tools_enabled=True)
    try:
        connector = c.app.state.realtime_connector
        with c.websocket_connect(
            f"/api/voice/live?provider=speech_voice_live&model={model_id}&echoRef=client&tools=1",
            subprotocols=[DEV_SUBPROTOCOL, "paramsuser"], headers=_origin(),
        ) as ws:
            ws.send_text(json.dumps({"type": "session.update", "session": {
                "temperature": 0.5, "reasoning_effort": "high", "parallel_tool_calls": True,
                "tools": [{"type": "function", "name": "untrusted"}],
            }}))
            ws.receive_text()
        session = json.loads(connector.upstream.sent_text[0])["session"]
        assert session.get("reasoning_effort") == effort
        assert session.get("temperature") == temperature
        # The echo reference, the tool bridge and the persona all still apply.
        assert session["parallel_tool_calls"] is False
        assert session["input_audio_echo_cancellation"] == OPTED_IN_ECHO
        assert session["tools"] and all(tool["name"] != "untrusted" for tool in session["tools"])
        assert session["tool_choice"] == "auto"
        assert session["instructions"] == GENERIC_VOICE
        # The echo-reference rewrite records the same evidence as any Speech session.
        live = [attrs for name, attrs in events if name == "voice_live_completion"]
        assert len(live) == 1 and live[0]["echoReference"] == "client"
        assert live[0].get("reasoningEffort") == effort
        assert live[0].get("clientTemperatureOmitted") == (1 if temperature is None else None)
    finally:
        c.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# Completion telemetry: why the first reply failed, and what the relay sent.
# --------------------------------------------------------------------------- #


class _ReplyUpstream:
    """Answers the first session.update with scripted provider frames, then closes."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._answered = False
        self._queue: asyncio.Queue[UpstreamMessage] = asyncio.Queue()

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)
        if '"session.update"' in data and not self._answered:
            self._answered = True
            for frame in self.frames:
                await self._queue.put(UpstreamMessage("text", text=frame))
            await self._queue.put(UpstreamMessage("close", close_code=1000))

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def receive(self) -> UpstreamMessage:
        return await self._queue.get()

    async def close(self) -> None:
        return None


class _ReplyConnector:
    def __init__(self, frames: list[str]) -> None:
        self.upstream = _ReplyUpstream(frames)
        self.connects: list[dict] = []

    @asynccontextmanager
    async def connect(self, *, url: str, headers: dict[str, str], timeout: float):
        self.connects.append({"url": url, "headers": headers, "timeout": timeout})
        yield self.upstream


def _failed(error: dict[str, object]) -> str:
    return json.dumps({"type": "response.done", "response": {
        "id": "resp_secret_id", "status": "failed", "output": [],
        "status_details": {"type": "failed", "error": error},
    }})


FIRST_FAILURE = {
    "type": "invalid_request_error", "code": "unsupported_value",
    "param": "session.temperature", "message": SECRET,
}
LATER_FAILURE = {"type": "server_error", "code": "later_failure_code", "message": SECRET}


@pytest.mark.parametrize(
    ("model_id", "parameters"),
    [
        ("gpt-5.6-terra", {"reasoningEffort": "none", "clientTemperatureOmitted": 1}),
        ("gpt-realtime", {}),
    ],
)
def test_completion_records_the_first_failure_and_the_relays_own_parameters(
    monkeypatch, caplog, model_id, parameters,
):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        realtime_module, "emit_custom_event", lambda name, attrs: events.append((name, attrs)),
    )
    c = _speech_client()
    caplog.set_level("INFO", logger="ai4ia_api.routers.realtime")
    capture = _attach_completion_capture(caplog)
    try:
        frames = [_failed(FIRST_FAILURE), _failed(LATER_FAILURE)]
        connector = _ReplyConnector(frames)
        c.app.state.realtime_connector = connector
        with c.websocket_connect(
            f"/api/voice/live?provider=speech_voice_live&model={model_id}",
            subprotocols=[DEV_SUBPROTOCOL, "failureuser"], headers=_origin(),
        ) as ws:
            ws.send_text(json.dumps({"type": "session.update", "session": {"temperature": 0.5}}))
            for _ in frames:
                ws.receive_text()
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

        # The temperature reached Azure only for the model with sampling.
        sent = json.loads(connector.upstream.sent_text[0])["session"]
        assert ("temperature" in sent) is (model_id == "gpt-realtime")

        first = {
            "responseFailedType": "invalid_request_error",
            "responseFailedCode": "unsupported_value",
            "responseFailedParam": "session.temperature",
        }
        live = [attrs for name, attrs in events if name == "voice_live_completion"]
        assert len(live) == 1
        event = live[0]
        assert (event["flowVersion"], event["responseFailed"]) == (2, 2)
        assert {key: event[key] for key in first} == first
        assert {
            key: event[key]
            for key in ("reasoningEffort", "clientTemperatureOmitted")
            if key in event
        } == parameters

        payloads = _completion_payloads(caplog)
        assert len(payloads) == 1
        assert payloads[0]["stats"]["responseFailure"] == {
            "type": "invalid_request_error", "code": "unsupported_value",
            "param": "session.temperature",
        }
        assert {
            key: payloads[0][key]
            for key in ("reasoningEffort", "clientTemperatureOmitted")
            if key in payloads[0]
        } == parameters

        encoded = json.dumps(payloads) + json.dumps(live, default=str)
        for forbidden in (SECRET, "resp_secret_id", "later_failure_code", "failureuser"):
            assert forbidden not in encoded
    finally:
        if capture is not None:
            capture.removeHandler(caplog.handler)
        c.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# Receipt notes: the redactor masks any 32+ character token, so none may be one.
# --------------------------------------------------------------------------- #

KNOWN_RECEIPT_NOTES = (
    "voice_usage_not_recorded",
    "voice_model_params_not_recorded",
    "avatar_media_not_recorded",
    "voice_not_started",
    "voice_delivery_guidance_v1",
    "echo_reference_client",
)


def test_every_voice_relay_receipt_note_survives_the_redactor():
    for note in VOICE_RELAY_RECEIPT_NOTES:
        assert redact(note) == note, note
    notes = list(VOICE_RELAY_RECEIPT_NOTES)
    assert build_receipt(notes=notes).notes == notes
    assert set(KNOWN_RECEIPT_NOTES) <= set(VOICE_RELAY_RECEIPT_NOTES)
    # Control: the parameters note's former 35-character name was masked whole.
    assert redact("voice_model_parameters_not_recorded") == "***REDACTED***"
    assert build_receipt(notes=["voice_model_parameters_not_recorded"]).notes == [
        "***REDACTED***"
    ]
