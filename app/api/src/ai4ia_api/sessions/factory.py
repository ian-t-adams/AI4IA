"""Selects and constructs the configured session repository."""
from __future__ import annotations

from ..config import Settings, SessionStoreKind
from ..documents.ephemeral_store import inline_attachment_storage_id
from .memory_repo import InMemorySessionRepository
from .repository import SessionRepository


def build_session_repository(settings: Settings) -> SessionRepository:
    if settings.session_store == SessionStoreKind.memory:
        return InMemorySessionRepository(
            deletion_enabled=settings.session_deletion_enabled,
            attachment_storage_required=settings.inline_document_compute_enabled,
            attachment_storage_id=inline_attachment_storage_id(settings),
        )
    if settings.session_store == SessionStoreKind.cosmos:
        if not settings.cosmos_endpoint:
            raise RuntimeError("AI4IA_COSMOS_ENDPOINT is required for the cosmos store.")
        from .cosmos_repo import CosmosSessionRepository

        return CosmosSessionRepository(
            settings.cosmos_endpoint, settings.cosmos_database,
            deletion_enabled=settings.session_deletion_enabled,
            deletion_rollout_id=settings.session_deletion_rollout_id,
            attachment_storage_required=settings.inline_document_compute_enabled,
            attachment_storage_id=inline_attachment_storage_id(settings),
        )
    raise RuntimeError(f"Unsupported session store: {settings.session_store}")
