"""Selects and constructs the configured session repository."""
from __future__ import annotations

from ..config import Settings, SessionStoreKind
from .memory_repo import InMemorySessionRepository
from .repository import SessionRepository


def build_session_repository(settings: Settings) -> SessionRepository:
    if settings.session_store == SessionStoreKind.memory:
        return InMemorySessionRepository()
    if settings.session_store == SessionStoreKind.cosmos:
        if not settings.cosmos_endpoint:
            raise RuntimeError("AI4IA_COSMOS_ENDPOINT is required for the cosmos store.")
        from .cosmos_repo import CosmosSessionRepository

        return CosmosSessionRepository(settings.cosmos_endpoint, settings.cosmos_database)
    raise RuntimeError(f"Unsupported session store: {settings.session_store}")
