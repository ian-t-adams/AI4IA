"""Builds the per-user workflow store.

The store kind mirrors ``settings.session_store`` (cosmos vs in-memory) so it
never disagrees with the session store about durability. A loud warning is
emitted when the in-memory store is selected outside ``local`` — there, a user's
saved workflows would silently vanish on restart/replica rollover.
"""
from __future__ import annotations

import logging

from ..config import Environment, Settings, SessionStoreKind
from .store import WorkflowStore

logger = logging.getLogger(__name__)


def build_workflow_store(settings: Settings) -> WorkflowStore:
    if settings.session_store == SessionStoreKind.cosmos and settings.cosmos_endpoint:
        from .cosmos_store import CosmosWorkflowStore

        return CosmosWorkflowStore(settings.cosmos_endpoint, settings.cosmos_database)
    if settings.env != Environment.local:
        logger.warning(
            "workflows: using the in-memory store outside local; saved workflows "
            "will not survive a restart or replica rollover."
        )
    from .store import InMemoryWorkflowStore

    return InMemoryWorkflowStore()
