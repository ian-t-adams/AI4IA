"""Builds hard-admission coordination only for an exactly approved rollout.

Flag off: nothing is constructed. The explicitly seeded local fake keeps its
historical contract. Every other configuration reads the one selected
``hard_quota_rollout_v1`` record from the existing ``usage`` container, checks
the account and container layout, and only then constructs the Cosmos adapter
with the approved request-count scope. Any failure refuses startup; there is no
fallback to soft admission, a local store or an empty balance.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..config import Environment, SessionStoreKind, Settings
from .cosmos_store import (
    USAGE_CONTAINER,
    CosmosReservationStore,
    check_account_layout,
    check_container_layout,
    coordination_time,
)
from .models import CONTROL_PARTITION, QuotaError
from .rollout import ROLLOUT_ID_PATTERN, HardQuotaRollout, parse_rollout
from .service import RequestCountScope
from .store import LocalReservationStore, ReservationStore

LAYOUT_REVALIDATE_SECONDS = 60
STARTUP_SECONDS = 30


class HardQuotaActivationError(RuntimeError):
    """A fixed startup refusal; never includes endpoints, ids or provider bodies."""


@dataclass(frozen=True)
class CosmosConnection:
    container: Any
    read_account: Callable[[], Awaitable[Mapping[str, Any]]]
    close: Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class AdmissionBinding:
    store: ReservationStore
    scope: RequestCountScope | None
    close: Callable[[], Awaitable[None]]


async def _nothing() -> None:
    return None


def account_observation(account: Any) -> dict[str, Any]:
    """Project the SDK account object; missing attributes stay missing (refused)."""
    return {
        "enableMultipleWriteLocations": getattr(account, "_EnableMultipleWritableLocations", None),
        "writableLocations": getattr(account, "WritableLocations", None),
        "consistencyPolicy": getattr(account, "ConsistencyPolicy", None),
    }


async def connect_cosmos(endpoint: str, database: str) -> CosmosConnection:
    from azure.cosmos.aio import CosmosClient
    from azure.identity.aio import DefaultAzureCredential

    credential = DefaultAzureCredential()
    client = CosmosClient(endpoint, credential=credential)
    container = client.get_database_client(database).get_container_client(USAGE_CONTAINER)

    async def read_account() -> Mapping[str, Any]:
        # The aio facade exposes the account read on its connection; this is a
        # data-plane metadata read, not a management-plane call.
        return account_observation(await client.client_connection.GetDatabaseAccount())

    async def close() -> None:
        try:
            await client.close()
        finally:
            await credential.close()

    return CosmosConnection(container, read_account, close)


async def read_rollout(container: Any, rollout_id: str) -> HardQuotaRollout:
    from azure.core.exceptions import AzureError
    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    if not re.fullmatch(ROLLOUT_ID_PATTERN, rollout_id):
        raise HardQuotaActivationError("The hard quota rollout id is invalid.")
    try:
        raw = await container.read_item(item=rollout_id, partition_key=CONTROL_PARTITION)
    except CosmosResourceNotFoundError as exc:
        raise HardQuotaActivationError("An approved hard quota rollout record is required.") from exc
    except (AzureError, OSError, TimeoutError) as exc:
        raise HardQuotaActivationError("The hard quota rollout record is unavailable.") from exc
    try:
        return parse_rollout(raw, rollout_id=rollout_id, observed_at=coordination_time(raw))
    except (QuotaError, ValidationError, ValueError, TypeError) as exc:
        raise HardQuotaActivationError("Hard quota rollout evidence does not match.") from exc


async def _approved(connection: CosmosConnection, rollout_id: str) -> AdmissionBinding:
    from azure.core.exceptions import AzureError

    rollout = await read_rollout(connection.container, rollout_id)
    try:
        check_account_layout(await connection.read_account())
        check_container_layout(await connection.container.read())
    except QuotaError as exc:
        raise HardQuotaActivationError(str(exc)) from exc
    except (AzureError, OSError, TimeoutError) as exc:
        raise HardQuotaActivationError("Hard quota storage layout is unavailable.") from exc
    # Constructed only after the exactly selected approval and layout validate.
    store = CosmosReservationStore(
        connection.container, read_account=connection.read_account,
        layout_ttl_seconds=LAYOUT_REVALIDATE_SECONDS,
    )
    return AdmissionBinding(store, RequestCountScope(rollout.coverageStart), connection.close)


async def build_admission_binding(
    settings: Settings,
    *,
    connect: Callable[[str, str], Awaitable[CosmosConnection]] = connect_cosmos,
) -> AdmissionBinding | None:
    if not settings.hard_quota_enabled:
        return None
    if settings.env == Environment.local and settings.session_store == SessionStoreKind.memory:
        # Explicitly seeded test coordination only; nothing is ever seeded here.
        return AdmissionBinding(LocalReservationStore(), None, _nothing)
    if settings.session_store != SessionStoreKind.cosmos or not settings.cosmos_endpoint:
        raise HardQuotaActivationError("Hard quota admission requires the Cosmos store.")
    if not re.fullmatch(ROLLOUT_ID_PATTERN, settings.hard_quota_rollout_id):
        raise HardQuotaActivationError("The hard quota rollout id is invalid.")
    connection = await connect(settings.cosmos_endpoint, settings.cosmos_database)
    try:
        async with asyncio.timeout(STARTUP_SECONDS):
            return await _approved(connection, settings.hard_quota_rollout_id)
    except TimeoutError as exc:
        await connection.close()
        raise HardQuotaActivationError("Hard quota activation checks timed out.") from exc
    except BaseException:
        await connection.close()
        raise
