"""Builds the entitlement store + the default (unlimited) entitlement.

The store kind mirrors ``settings.session_store`` (cosmos vs in-memory) so the
two never disagree about durability. A loud warning is emitted when the
in-memory store is selected outside ``local`` — there, an admin-set limit would
silently vanish on restart/replica-rollover.

The *default* entitlement is composed from the optional ``default_*`` settings.
Those ship unset, so the default is fully unlimited; an operator can introduce a
global cap via env without touching per-user overrides.
"""
from __future__ import annotations

import logging

from ..config import Environment, Settings, SessionStoreKind
from .models import Entitlement
from .store import EntitlementStore

logger = logging.getLogger(__name__)

_DEFAULT_ENTITLEMENT_ID = "__default__"


def build_entitlement_store(settings: Settings) -> EntitlementStore:
    if settings.session_store == SessionStoreKind.cosmos and settings.cosmos_endpoint:
        from .cosmos_store import CosmosEntitlementStore

        return CosmosEntitlementStore(settings.cosmos_endpoint, settings.cosmos_database)
    if settings.env != Environment.local:
        logger.warning(
            "entitlements: using the in-memory store outside local; admin-set "
            "limits will not survive a restart or replica rollover."
        )
    from .memory_store import InMemoryEntitlementStore

    return InMemoryEntitlementStore()


def build_default_entitlement(settings: Settings) -> Entitlement:
    """The policy applied to any user without an override. Unlimited unless an
    operator configured a global ``default_*`` cap."""
    return Entitlement(
        id=_DEFAULT_ENTITLEMENT_ID,
        userId=_DEFAULT_ENTITLEMENT_ID,
        requestsPerMinute=settings.default_requests_per_minute,
        tokensPerDay=settings.default_tokens_per_day,
        costPerDayMicroUsd=settings.default_cost_per_day_micro_usd,
        tokensPerMonth=settings.default_tokens_per_month,
        costPerMonthMicroUsd=settings.default_cost_per_month_micro_usd,
    )
