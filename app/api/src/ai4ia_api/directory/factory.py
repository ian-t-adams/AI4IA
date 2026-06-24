"""Selects and constructs the user directory repo, mirroring the session store.

Like the usage ledger and entitlement store, the directory shares Cosmos with
sessions (same account/database) and follows ``settings.session_store``:
``cosmos`` -> Cosmos directory, otherwise the in-memory directory. This keeps the
stores from ever disagreeing about durability.
"""
from __future__ import annotations

from ..config import Settings, SessionStoreKind
from .memory_repo import InMemoryUserDirectoryRepository
from .repository import UserDirectoryRepository


def build_user_directory_repository(settings: Settings) -> UserDirectoryRepository:
    if settings.session_store == SessionStoreKind.cosmos and settings.cosmos_endpoint:
        from .cosmos_repo import CosmosUserDirectoryRepository

        return CosmosUserDirectoryRepository(
            settings.cosmos_endpoint, settings.cosmos_database
        )
    return InMemoryUserDirectoryRepository()
