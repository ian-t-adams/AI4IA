"""Selects and constructs the usage ledger, mirroring the session store kind.

The ledger shares Cosmos with sessions (same account/database), so it follows
``settings.session_store``: ``cosmos`` -> Cosmos ledger, otherwise the in-memory
ledger. This keeps the two stores from ever disagreeing about durability.
"""
from __future__ import annotations

from ..config import Settings, SessionStoreKind
from .memory_repo import InMemoryUsageRepository
from .repository import UsageRepository


def build_usage_repository(settings: Settings) -> UsageRepository:
    if settings.session_store == SessionStoreKind.cosmos and settings.cosmos_endpoint:
        from .cosmos_repo import CosmosUsageRepository

        return CosmosUsageRepository(settings.cosmos_endpoint, settings.cosmos_database)
    return InMemoryUsageRepository()
