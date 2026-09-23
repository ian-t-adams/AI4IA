"""CAS adapter for the existing ``usage`` /userId partition.

There is deliberately no create/upsert, migration or repair path here. Owner
documents come only from the operator bootstrap; the production factory
constructs this adapter only after an approved rollout record validates. OCC
requires a single write region; multi-write last-writer-wins is not admission.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .models import POLICY_VERSION, STATE_ID, STATE_KIND, QuotaError, QuotaState, Snapshot, state_document

if TYPE_CHECKING:
    from azure.cosmos.aio import ContainerProxy

USAGE_CONTAINER = "usage"
MAX_LAYOUT_TTL_SECONDS = 300


def check_account_layout(account: Mapping[str, Any]) -> None:
    """Exactly one writable region, no multi-write, and the IaC's Session level."""
    writes = account.get("writableLocations")
    consistency = account.get("consistencyPolicy")
    if (
        account.get("enableMultipleWriteLocations") is not False
        or not isinstance(writes, list) or len(writes) != 1
        or not isinstance(consistency, Mapping)
        or consistency.get("defaultConsistencyLevel") != "Session"
    ):
        raise QuotaError("Hard quota requires observed single-region Session-consistency Cosmos writes.")


def check_container_layout(container: Mapping[str, Any]) -> None:
    partition = container.get("partitionKey")
    default_ttl = container.get("defaultTtl")
    analytical_ttl = container.get("analyticalStorageTtl")
    if (
        container.get("id") != USAGE_CONTAINER
        or not isinstance(partition, Mapping)
        or partition.get("paths") != ["/userId"]
        or partition.get("kind") != "Hash"
        # Exact types: a Boolean must not pass as the -1/0 retention sentinels.
        or not (default_ttl is None or (type(default_ttl) is int and default_ttl == -1))
        or not (analytical_ttl is None or (type(analytical_ttl) is int and analytical_ttl == 0))
    ):
        raise QuotaError("Hard quota Cosmos partition or retention is incompatible.")


def coordination_time(response: Any) -> int:
    """The Cosmos response ``Date``; there is no replica-local clock fallback."""
    read_headers = getattr(response, "get_response_headers", None)
    headers = read_headers() if callable(read_headers) else None
    date = headers.get("date") if isinstance(headers, Mapping) else None
    if not isinstance(date, str):
        raise QuotaError("Hard quota coordination time is unavailable.")
    observed = parsedate_to_datetime(date)
    if observed.tzinfo is None:
        raise QuotaError("Hard quota coordination time is invalid.")
    return int(observed.timestamp())


class CosmosReservationStore:
    def __init__(
        self,
        container: ContainerProxy,
        *,
        read_account: Callable[[], Awaitable[Mapping[str, Any]]],
        layout_ttl_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= layout_ttl_seconds <= MAX_LAYOUT_TTL_SECONDS:
            raise ValueError("Invalid hard quota layout revalidation interval.")
        self._container = container
        self._read_account = read_account
        self._layout_ttl = layout_ttl_seconds
        self._clock = clock
        self._layout_checked: float | None = None

    async def validate_layout(self) -> None:
        # Metadata requests have separate limits and no SLA, so production checks
        # at startup and then at most once per bounded interval. Zero keeps the
        # historical per-operation observation.
        if (
            self._layout_checked is not None and self._layout_ttl > 0
            and self._clock() - self._layout_checked < self._layout_ttl
        ):
            return
        self._layout_checked = None
        check_account_layout(await self._read_account())
        check_container_layout(await self._container.read())
        self._layout_checked = self._clock()

    async def read(self, owner: str) -> Snapshot:
        from azure.core.exceptions import AzureError
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            await self.validate_layout()
        except (AzureError, OSError, TimeoutError, ValueError) as exc:
            raise QuotaError("Hard quota coordination is unavailable or incompatible.") from exc
        try:
            raw = await self._container.read_item(item=STATE_ID, partition_key=owner)
        except CosmosResourceNotFoundError as exc:
            # Never an empty balance: only the reviewed operator bootstrap creates it.
            raise QuotaError("Hard quota state is absent; reviewed bootstrap is required.") from exc
        except (AzureError, OSError, TimeoutError, ValueError) as exc:
            raise QuotaError("Hard quota coordination is unavailable or incompatible.") from exc
        try:
            if (raw.get("id"), raw.get("kind"), raw.get("policyVersion")) != (
                STATE_ID, STATE_KIND, POLICY_VERSION,
            ):
                raise QuotaError("Hard quota coordination state is incompatible.")
            etag = raw.get("_etag")
            if not isinstance(etag, str) or not etag:
                raise QuotaError("Hard quota coordination state has no ETag.")
            observed = coordination_time(raw)
            document = {key: value for key, value in raw.items() if not key.startswith("_")}
            state = QuotaState.model_validate(document, context={"persisted_quota": True})
            state_document(state)
            if state.userId != owner:
                raise QuotaError("Hard quota owner mismatch.", code=403)
            return Snapshot(state, etag, observed)
        except (AzureError, OSError, TimeoutError, ValidationError, ValueError) as exc:
            raise QuotaError("Hard quota coordination is unavailable or incompatible.") from exc

    async def replace(self, owner: str, snapshot: Snapshot, state: QuotaState) -> bool:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        if owner != state.userId or owner != snapshot.state.userId:
            raise QuotaError("Hard quota owner mismatch.", code=403)
        if not snapshot.etag:
            raise QuotaError("Hard quota coordination state has no ETag.")
        document = state_document(state)
        try:
            await self.validate_layout()
            await self._container.replace_item(
                item=STATE_ID, body=document, etag=snapshot.etag,
                match_condition=MatchConditions.IfNotModified,
                # Never retry an ambiguous write automatically.
                retry_write=0,
            )
        except CosmosAccessConditionFailedError:
            return False
        except (AzureError, OSError, TimeoutError) as exc:
            raise QuotaError("Hard quota coordination write is unavailable.") from exc
        return True
