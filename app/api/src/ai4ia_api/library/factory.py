"""Selects and constructs the document-library repository.

Returns ``None`` when the feature is disabled so the app constructs nothing and
the router refuses (404) — the default-OFF, zero-regression posture. When
enabled, the store mirrors the session store's durability (in-memory vs Cosmos).
"""
from __future__ import annotations

from ..config import Settings, SessionStoreKind
from .memory_repo import InMemoryDocumentLibraryRepository
from .repository import DocumentLibraryRepository


def build_document_library(settings: Settings) -> DocumentLibraryRepository | None:
    if not settings.document_understanding_enabled:
        return None
    if settings.session_store == SessionStoreKind.cosmos:
        if not settings.cosmos_endpoint:
            raise RuntimeError(
                "AI4IA_COSMOS_ENDPOINT is required for the cosmos document library."
            )
        from .cosmos_repo import CosmosDocumentLibraryRepository

        return CosmosDocumentLibraryRepository(
            settings.cosmos_endpoint, settings.cosmos_database
        )
    return InMemoryDocumentLibraryRepository()
