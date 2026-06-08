"""Voice endpoints contract + governance (speech + transcription).

Covers happy paths, input/voice/format validation, catalog/category rejection,
entitlement gating (disabled + admin rate limit), upstream-error sanitization,
and the governance guarantee that successful voice requests are metered into the
usage ledger (so rolling rate/budget windows account for voice traffic).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.main import create_app
from tests.conftest import make_settings

ADMIN = {"X-Dev-User": "alice"}
FAKE_AUDIO = b"ID3\x04\x00\x00\x00\x00fake-mp3-bytes"


class FakeVoiceGateway:
    """Stand-in gateway for voice tests: returns canned audio/text or raises."""

    def __init__(self) -> None:
        self.speech_error: ModelGatewayError | None = None
        self.transcribe_error: ModelGatewayError | None = None
        self.audio: bytes = FAKE_AUDIO
        self.text: str = "hello from the transcript"
        self.speech_calls: list[dict] = []
        self.transcribe_calls: list[dict] = []

    async def synthesize_speech(
        self, *, deployment, text, voice, response_format, extra=None, correlation_id=None
    ):
        self.speech_calls.append(
            {"deployment": deployment, "text": text, "voice": voice, "format": response_format}
        )
        if self.speech_error is not None:
            raise self.speech_error
        return self.audio

    async def transcribe(
        self, *, deployment, audio, filename, content_type, language=None,
        response_format="json", correlation_id=None,
    ):
        self.transcribe_calls.append(
            {"deployment": deployment, "filename": filename, "content_type": content_type,
             "language": language, "bytes": len(audio)}
        )
        if self.transcribe_error is not None:
            raise self.transcribe_error
        return {"text": self.text}


def _client(**settings_overrides) -> TestClient:
    app = create_app(make_settings(admin_subjects="alice", **settings_overrides))
    c = TestClient(app)
    c.__enter__()
    c.app.state.gateway = FakeVoiceGateway()
    return c


@pytest.fixture
def client():
    c = _client()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _internal_id(client, headers) -> str:
    return client.get("/api/entitlement", headers=headers).json()["userId"]


def _audio_upload(content_type="audio/webm", name="clip.webm", data=b"\x00\x01\x02pcm"):
    return {"file": (name, data, content_type)}


# ---- speech (TTS) happy path ----


def test_speech_success_returns_audio(client):
    r = client.post(
        "/api/voice/speech",
        json={"input": "hello world", "model": "tts-hd", "voice": "nova", "format": "mp3"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == FAKE_AUDIO
    assert r.headers.get("X-Model") == "tts-hd"
    call = client.app.state.gateway.speech_calls[-1]
    assert call["voice"] == "nova"
    assert call["format"] == "mp3"


def test_speech_default_model_and_voice(client):
    r = client.post(
        "/api/voice/speech",
        json={"input": "hi"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    # Default voice is alloy; default model is the first tts catalog model.
    assert client.app.state.gateway.speech_calls[-1]["voice"] == "alloy"


def test_speech_wav_format_media_type(client):
    r = client.post(
        "/api/voice/speech",
        json={"input": "hi", "model": "tts-hd", "format": "wav"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"


# ---- speech validation ----


def test_speech_empty_input_rejected(client):
    r = client.post(
        "/api/voice/speech",
        json={"input": "   ", "model": "tts-hd"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 422


def test_speech_bad_voice_rejected(client):
    r = client.post(
        "/api/voice/speech",
        json={"input": "x", "model": "tts-hd", "voice": "darth-vader"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 422


def test_speech_bad_format_rejected(client):
    r = client.post(
        "/api/voice/speech",
        json={"input": "x", "model": "tts-hd", "format": "midi"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 422


def test_speech_non_tts_model_rejected(client):
    r = client.post(
        "/api/voice/speech",
        json={"input": "x", "model": "gpt-5.2"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 400
    assert "not a speech model" in r.json()["detail"]


def test_speech_unknown_model_rejected(client):
    r = client.post(
        "/api/voice/speech",
        json={"input": "x", "model": "no-such-model"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 400


# ---- transcription (STT) happy path ----


def test_transcription_success(client):
    r = client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(),
        data={"model": "whisper"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "hello from the transcript"
    assert body["model"] == "whisper"
    assert body["deployment"].startswith("whisper")


def test_transcription_default_model(client):
    r = client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(),
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "whisper"


def test_transcription_passes_language(client):
    client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(),
        data={"model": "whisper", "language": "en"},
        headers={"X-Dev-User": "ian"},
    )
    assert client.app.state.gateway.transcribe_calls[-1]["language"] == "en"


def test_transcription_empty_file_rejected(client):
    r = client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(data=b""),
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 422


def test_transcription_bad_content_type_rejected(client):
    r = client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(content_type="application/pdf", name="x.pdf"),
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 422


def test_transcription_accepts_mediarecorder_mime_with_codecs(client):
    # Browsers send e.g. "audio/webm;codecs=opus"; the base type must be matched.
    r = client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(content_type="audio/webm;codecs=opus"),
        data={"model": "whisper"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 200, r.text
    # The gateway receives the normalized base content type, not the codecs suffix.
    assert client.app.state.gateway.transcribe_calls[-1]["content_type"] == "audio/webm"


def test_transcription_non_stt_model_rejected(client):
    r = client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(),
        data={"model": "gpt-5.2"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 400
    assert "not a transcription model" in r.json()["detail"]


# ---- entitlement gating ----


def test_speech_disabled_user_forbidden(client):
    headers = {"X-Dev-User": "banned"}
    uid = _internal_id(client, headers)
    client.put(f"/api/admin/entitlements/{uid}", json={"disabled": True}, headers=ADMIN)
    r = client.post(
        "/api/voice/speech",
        json={"input": "x", "model": "tts-hd"},
        headers=headers,
    )
    assert r.status_code == 403


def test_transcription_rate_limited_429(client):
    headers = {"X-Dev-User": "capped"}
    uid = _internal_id(client, headers)
    client.put(
        f"/api/admin/entitlements/{uid}", json={"requestsPerMinute": 0}, headers=ADMIN
    )
    r = client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(),
        data={"model": "whisper"},
        headers=headers,
    )
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"


# ---- upstream error sanitization ----


def test_speech_upstream_500_mapped_to_generic_502(client):
    client.app.state.gateway.speech_error = ModelGatewayError(500, "internal stack trace leak")
    r = client.post(
        "/api/voice/speech",
        json={"input": "x", "model": "tts-hd"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 502
    assert "stack trace" not in r.json()["detail"]


def test_transcription_upstream_400_surfaced(client):
    client.app.state.gateway.transcribe_error = ModelGatewayError(400, "invalid_audio: bad codec")
    r = client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(),
        data={"model": "whisper"},
        headers={"X-Dev-User": "ian"},
    )
    assert r.status_code == 400
    assert "invalid_audio" in r.json()["detail"]


# ---- governance: voice requests are metered ----


def test_speech_is_metered(client):
    headers = {"X-Dev-User": "meterme"}
    client.post(
        "/api/voice/speech",
        json={"input": "x", "model": "tts-hd"},
        headers=headers,
    )
    summary = client.get("/api/usage", headers=headers).json()
    assert summary["totalRequests"] >= 1


def test_transcription_is_metered(client):
    headers = {"X-Dev-User": "meterme2"}
    client.post(
        "/api/voice/transcriptions",
        files=_audio_upload(),
        data={"model": "whisper"},
        headers=headers,
    )
    summary = client.get("/api/usage", headers=headers).json()
    assert summary["totalRequests"] >= 1
