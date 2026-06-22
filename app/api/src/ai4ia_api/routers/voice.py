"""Voice endpoints: speech-to-text and text-to-speech.

Both ride the same model gateway as chat and share its governance: the caller is
authenticated, the entitlement gate runs (a disabled user is blocked; an admin
rate/budget limit applies), and every successful request is metered into the
per-user usage ledger so voice traffic counts toward rolling rate windows.

- ``POST /api/voice/transcriptions`` — multipart audio upload -> ``{text}``
  (whisper). Audio bytes are size-capped and never logged.
- ``POST /api/voice/speech`` — JSON ``{input, voice, format}`` -> audio bytes
  (gpt-4o-mini-tts / tts-hd). Input length, voice and format are closed allowlists.

Hardening mirrors the image endpoint: only audio-capable catalog models are
accepted, inputs are hard-capped, and upstream gateway errors are sanitized
(user-actionable 400s surface a trimmed detail; everything else is generic).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..catalog import ModelCatalog
from ..entitlements.service import EntitlementService
from ..gateway.client import ModelGatewayClient, ModelGatewayError
from ..logging_setup import get_correlation_id
from ..usage.models import TokenUsage
from ..usage.service import UsageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])

# Categories whose models serve the audio REST endpoints. Only whisper
# (category "transcription") serves /audio/transcriptions, and only the tts
# family (gpt-4o-mini-tts, tts-hd; category "tts") serves /audio/speech.
# gpt-audio (category "audio") is a chat-completions audio model and is NOT a
# REST speech/transcription model, so it is deliberately excluded here.
STT_CATEGORIES = {"transcription"}
TTS_CATEGORIES = {"tts"}

# --- Text-to-speech caps/allowlists ---
MAX_TTS_INPUT_CHARS = 4000
ALLOWED_VOICES = {
    "alloy", "echo", "fable", "onyx", "nova", "shimmer",
    "coral", "sage", "ash", "ballad", "verse", "marin", "cedar",
}
DEFAULT_VOICE = "alloy"
# response_format -> media type. mp3 is the broadly-supported default.
FORMAT_MEDIA_TYPE = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/opus",
    "flac": "audio/flac",
    "aac": "audio/aac",
}
DEFAULT_FORMAT = "mp3"
# Reject an upstream audio response larger than this (defense against an
# oversized/misbehaving provider response).
MAX_AUDIO_BYTES = 25_000_000  # ~25 MB

# --- Speech-to-text caps ---
MAX_UPLOAD_BYTES = 25_000_000  # 25 MB (provider limit)
MAX_LANGUAGE_LEN = 8  # ISO-639-1 plus a little slack


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=MAX_TTS_INPUT_CHARS)
    model: str | None = None
    voice: str | None = None
    format: str | None = None
    region: str | None = None
    dataZone: str | None = None


class TranscriptionResponse(BaseModel):
    text: str
    model: str
    deployment: str


def _audio_usage() -> TokenUsage:
    """Voice models don't report token usage, so each request meters as one
    *unknown* call: it counts toward rate limits but adds no tokens (consistent
    with how the image path treats a missing usage object)."""
    return TokenUsage(known=False, complete=False, calls=1)


def _resolve_model(
    catalog: ModelCatalog, model_id: str | None, *, categories: set[str], kind: str
):
    if not model_id:
        first = next((m for m in catalog.models if m.category in categories), None)
        if first is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No {kind} models are available.",
            )
        model_id = first.id
    entry = catalog.get(model_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown model: {model_id}",
        )
    if entry.category not in categories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Model '{model_id}' is not a {kind} model.",
        )
    return model_id, entry


async def _gate(entitlements: EntitlementService, user_id: str) -> None:
    decision = await entitlements.check(user_id)
    if not decision.allowed:
        headers = (
            {"Retry-After": str(decision.retry_after_seconds)}
            if decision.retry_after_seconds is not None
            else None
        )
        raise HTTPException(status_code=decision.code, detail=decision.reason, headers=headers)


def _sanitize_upstream(exc: ModelGatewayError, *, model_id: str, correlation_id: str, what: str):
    logger.warning(
        "%s upstream error (status=%s, model=%s, correlation_id=%s)",
        what, exc.status_code, model_id, correlation_id,
    )
    if exc.status_code == 400:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_trim(exc.detail) or f"{what} was rejected.",
        )
    if exc.status_code in (401, 403):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{what} provider rejected the request.",
        )
    if exc.status_code == 429:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{what} provider is rate limited. Try again shortly.",
            headers={"Retry-After": "30"},
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"{what} failed.")


@router.post("/speech")
async def synthesize_speech(
    body: SpeechRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    catalog: ModelCatalog = request.app.state.catalog
    gateway: ModelGatewayClient = request.app.state.gateway
    entitlements: EntitlementService = request.app.state.entitlements
    metering: UsageService = request.app.state.usage

    text = body.input.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Input must not be empty.",
        )

    voice = (body.voice or DEFAULT_VOICE).lower()
    if voice not in ALLOWED_VOICES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported voice. Allowed: {', '.join(sorted(ALLOWED_VOICES))}.",
        )
    fmt = (body.format or DEFAULT_FORMAT).lower()
    if fmt not in FORMAT_MEDIA_TYPE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported format. Allowed: {', '.join(sorted(FORMAT_MEDIA_TYPE))}.",
        )

    model_id, _ = _resolve_model(catalog, body.model, categories=TTS_CATEGORIES, kind="speech")
    deployment = catalog.resolve_deployment(model_id, region=body.region, data_zone=body.dataZone)
    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown or unavailable model: {model_id}",
        )

    await _gate(entitlements, user.internal_user_id)

    correlation_id = get_correlation_id()
    try:
        audio = await gateway.synthesize_speech(
            deployment=deployment.deploymentName,
            text=text,
            voice=voice,
            response_format=fmt,
            correlation_id=correlation_id,
        )
    except ModelGatewayError as exc:
        raise _sanitize_upstream(
            exc, model_id=model_id, correlation_id=correlation_id, what="Speech synthesis"
        ) from exc

    if not audio:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Speech synthesis returned no audio.",
        )
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Synthesized audio was unexpectedly large.",
        )

    await metering.record_completion(
        user_id=user.internal_user_id,
        session_id="voice-speech",
        model_id=model_id,
        deployment=deployment,
        usage=_audio_usage(),
        status="complete",
        correlation_id=correlation_id,
    )

    return Response(
        content=audio,
        media_type=FORMAT_MEDIA_TYPE[fmt],
        headers={
            "Content-Disposition": f'inline; filename="speech.{fmt}"',
            "X-Model": model_id,
        },
    )


@router.post("/transcriptions", response_model=TranscriptionResponse)
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    model: str | None = Form(default=None),
    language: str | None = Form(default=None),
    region: str | None = Form(default=None),
    dataZone: str | None = Form(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
) -> TranscriptionResponse:
    catalog: ModelCatalog = request.app.state.catalog
    gateway: ModelGatewayClient = request.app.state.gateway
    entitlements: EntitlementService = request.app.state.entitlements
    metering: UsageService = request.app.state.usage

    # Validate cheap header-derived inputs BEFORE reading the upload body. Browser
    # MediaRecorder sends types like "audio/webm;codecs=opus", so match on the
    # base type only.
    raw_type = file.content_type or "application/octet-stream"
    content_type = raw_type.split(";", 1)[0].strip().lower()
    if not (content_type.startswith("audio/") or content_type in {"video/webm", "video/mp4"}):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported content type: {raw_type}.",
        )

    lang = (language or "").strip() or None
    if lang and len(lang) > MAX_LANGUAGE_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid language code.",
        )

    model_id, _ = _resolve_model(
        catalog, model, categories=STT_CATEGORIES, kind="transcription"
    )
    deployment = catalog.resolve_deployment(model_id, region=region, data_zone=dataZone)
    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown or unavailable model: {model_id}",
        )

    # Gate BEFORE reading the (potentially large) body so a disabled/rate-limited
    # caller can't force a full read.
    await _gate(entitlements, user.internal_user_id)

    # Read with a hard cap: one byte past the limit detects oversize without
    # pulling an unbounded amount into memory.
    audio = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(audio) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Audio file is too large (max 25 MB).",
        )
    if not audio:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Audio file is empty.",
        )

    correlation_id = get_correlation_id()
    try:
        result = await gateway.transcribe(
            deployment=deployment.deploymentName,
            audio=audio,
            filename=file.filename or "audio",
            content_type=content_type,
            language=lang,
            correlation_id=correlation_id,
        )
    except ModelGatewayError as exc:
        raise _sanitize_upstream(
            exc, model_id=model_id, correlation_id=correlation_id, what="Transcription"
        ) from exc

    text = (result or {}).get("text")
    if not isinstance(text, str):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Transcription returned no text.",
        )

    await metering.record_completion(
        user_id=user.internal_user_id,
        session_id="voice-transcription",
        model_id=model_id,
        deployment=deployment,
        usage=_audio_usage(),
        status="complete",
        correlation_id=correlation_id,
    )

    return TranscriptionResponse(text=text, model=model_id, deployment=deployment.deploymentName)


def _trim(detail: str | None, limit: int = 300) -> str:
    if not detail:
        return ""
    text = detail.strip()
    return text if len(text) <= limit else text[:limit] + "…"
