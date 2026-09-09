"""Source-only CAS adapter for the existing ``usage`` /userId partition.

There is deliberately no create/upsert, migration, credential factory or runtime
activation path here. A future reviewed cutover must supply existing state and
a live account-properties reader, not a configuration acknowledgement. OCC
requires a single write region; multi-write last-writer-wins is not admission.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .models import POLICY_VERSION, STATE_ID, STATE_KIND, QuotaError, QuotaState, Snapshot, state_document

if TYPE_CHECKING:
    from azure.cosmos.aio import ContainerProxy


class CosmosReservationStore:
    def __init__(
        self,
        container: ContainerProxy,
        *,
        read_account: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> None:
        self._container = container
        self._read_account = read_account

    async def _validate_layout(self) -> None:
        account = await self._read_account()
        writes = account.get("writableLocations")
        if (
            account.get("enableMultipleWriteLocations") is not False
            or not isinstance(writes, list) or len(writes) != 1
        ):
            raise QuotaError("Hard quota requires observed single-region Cosmos writes.")
        container = await self._container.read()
        if (
            container.get("id") != "usage"
            or container.get("partitionKey", {}).get("paths") != ["/userId"]
            or container.get("partitionKey", {}).get("kind") != "Hash"
            or container.get("defaultTtl") not in (None, -1)
        ):
            raise QuotaError("Hard quota Cosmos partition or retention is incompatible.")

    async def read(self, owner: str) -> Snapshot:
        from azure.core.exceptions import AzureError

        try:
            await self._validate_layout()
            raw = await self._container.read_item(item=STATE_ID, partition_key=owner)
            if (raw.get("id"), raw.get("kind"), raw.get("policyVersion")) != (
                STATE_ID, STATE_KIND, POLICY_VERSION,
            ):
                raise QuotaError("Hard quota coordination state is incompatible.")
            etag = raw.get("_etag")
            if not isinstance(etag, str) or not etag:
                raise QuotaError("Hard quota coordination state has no ETag.")
            headers = raw.get_response_headers()
            date = headers.get("date")
            if not isinstance(date, str):
                raise QuotaError("Hard quota coordination time is unavailable.")
            observed = parsedate_to_datetime(date)
            if observed.tzinfo is None:
                raise QuotaError("Hard quota coordination time is invalid.")
            document = {key: value for key, value in raw.items() if not key.startswith("_")}
            state = QuotaState.model_validate(document)
            state_document(state)
            if state.userId != owner:
                raise QuotaError("Hard quota owner mismatch.", code=403)
            return Snapshot(state, etag, int(observed.timestamp()))
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
            await self._validate_layout()
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
