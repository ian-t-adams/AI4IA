"""Builds the per-user agent store.

The store kind mirrors ``settings.session_store`` (cosmos vs in-memory) so it
never disagrees with the session store about durability. A loud warning is
emitted when the in-memory store is selected outside ``local`` — there, a user's
saved agents would silently vanish on restart/replica rollover.
"""
from __future__ import annotations

import logging

from ..config import Environment, Settings, SessionStoreKind
from .store import UserAgentStore

logger = logging.getLogger(__name__)


def build_user_agent_store(settings: Settings) -> UserAgentStore:
    if settings.session_store == SessionStoreKind.cosmos and settings.cosmos_endpoint:
        from .cosmos_store import CosmosUserAgentStore

        return CosmosUserAgentStore(settings.cosmos_endpoint, settings.cosmos_database)
    if settings.env != Environment.local:
        logger.warning(
            "user agents: using the in-memory store outside local; saved agents "
            "will not survive a restart or replica rollover."
        )
    from .store import InMemoryUserAgentStore

    return InMemoryUserAgentStore()
