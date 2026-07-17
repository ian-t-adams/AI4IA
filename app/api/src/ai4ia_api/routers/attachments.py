"""Server-authoritative attachment capabilities for the active environment."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..auth.base import AuthenticatedUser
from ..auth.dependencies import get_current_user
from ..sessions.models import MAX_LIBRARY_DOCUMENTS_PER_SESSION
from .documents import MAX_DOCS_PER_SESSION, MAX_UPLOAD_BYTES

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

_TEXT_EXTENSIONS = [
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".xml",
    ".yaml", ".yml", ".html", ".htm", ".pdf", ".docx", ".pptx",
]
_LIBRARY_MEDIA_EXTENSIONS = [
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".webm", ".mp4", ".mov",
]


class AttachmentCapabilities(BaseModel):
    ingestPath: str
    maxBytes: int
    maxPerUserDocuments: int | None = None
    maxPerSessionDocuments: int
    extensions: list[str] = Field(default_factory=list)
    mimeTypes: list[str] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)


@router.get("/capabilities", response_model=AttachmentCapabilities)
async def attachment_capabilities(
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> AttachmentCapabilities:
    library_enabled = getattr(request.app.state, "document_library", None) is not None
    if library_enabled:
        settings = request.app.state.settings
        return AttachmentCapabilities(
            ingestPath="library",
            maxBytes=settings.document_max_upload_bytes,
            maxPerUserDocuments=settings.document_max_per_user,
            maxPerSessionDocuments=MAX_LIBRARY_DOCUMENTS_PER_SESSION,
            extensions=[*_TEXT_EXTENSIONS, *_LIBRARY_MEDIA_EXTENSIONS],
            mimeTypes=["text/*", "application/pdf", "image/*", "audio/*", "video/*"],
            modalities=["document", "text", "image", "audio", "video"],
        )
    return AttachmentCapabilities(
        ingestPath="session",
        maxBytes=MAX_UPLOAD_BYTES,
        maxPerSessionDocuments=MAX_DOCS_PER_SESSION,
        extensions=_TEXT_EXTENSIONS,
        mimeTypes=["text/*", "application/pdf"],
        modalities=["document", "text"],
    )
