"""Modality classification from content-type / filename (Phase 11A).

Chooses the broad media class for a library document so the right analyzer set
and (later) ingest pipeline are selected. MIME type wins; the filename extension
is a fallback for the many uploads that arrive as ``application/octet-stream``.
"""
from __future__ import annotations

from .models import Modality

# Extension -> modality fallback (used when the MIME type is missing/generic).
_EXT_MODALITY: dict[str, Modality] = {
    # documents
    "pdf": Modality.document,
    "doc": Modality.document,
    "docx": Modality.document,
    "ppt": Modality.document,
    "pptx": Modality.document,
    "xls": Modality.document,
    "xlsx": Modality.document,
    "rtf": Modality.document,
    "odt": Modality.document,
    # text
    "txt": Modality.text,
    "md": Modality.text,
    "markdown": Modality.text,
    "csv": Modality.text,
    "json": Modality.text,
    "html": Modality.text,
    "htm": Modality.text,
    # images
    "png": Modality.image,
    "jpg": Modality.image,
    "jpeg": Modality.image,
    "gif": Modality.image,
    "webp": Modality.image,
    "bmp": Modality.image,
    "tif": Modality.image,
    "tiff": Modality.image,
    "heic": Modality.image,
    # audio
    "mp3": Modality.audio,
    "wav": Modality.audio,
    "m4a": Modality.audio,
    "ogg": Modality.audio,
    "flac": Modality.audio,
    "aac": Modality.audio,
    # video
    "mp4": Modality.video,
    "mov": Modality.video,
    "webm": Modality.video,
    "mkv": Modality.video,
    "avi": Modality.video,
}

# A few document MIME types are not under a single top-level family.
_DOCUMENT_MIMES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/rtf",
        "application/vnd.oasis.opendocument.text",
    }
)

_TEXT_MIMES: frozenset[str] = frozenset(
    {"application/json", "application/xml", "application/csv"}
)


def _extension(filename: str) -> str:
    name = (filename or "").rsplit(".", 1)
    return name[1].lower() if len(name) == 2 else ""


def classify_modality(content_type: str | None, filename: str | None) -> Modality:
    """Best-effort media class for an upload. Never raises; defaults to
    :attr:`Modality.other` for genuinely unknown inputs."""
    mime = (content_type or "").split(";", 1)[0].strip().lower()

    if mime:
        if mime in _DOCUMENT_MIMES:
            return Modality.document
        if mime in _TEXT_MIMES or mime.startswith("text/"):
            return Modality.text
        if mime.startswith("image/"):
            return Modality.image
        if mime.startswith("audio/"):
            return Modality.audio
        if mime.startswith("video/"):
            return Modality.video

    ext = _extension(filename or "")
    if ext in _EXT_MODALITY:
        return _EXT_MODALITY[ext]
    return Modality.other
