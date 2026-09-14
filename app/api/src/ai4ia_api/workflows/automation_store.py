"""Owner-partition CAS for schedules, run admission and durable usage outboxes."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Generic, Protocol, TypeVar

from .automation_common import AutomationError, utc
from .automation_models import (
    AutomationOwner, WorkflowSchedule, persisted_model, writable_body,
)
from .record_types import (
    AUTOMATION_ID_PREFIX, AUTOMATION_OWNER_ID, AUTOMATION_OWNER_KIND, AUTOMATION_SCHEDULE_KIND,
)

Value = TypeVar("Value")


@dataclass(frozen=True)
class Stored(Generic[Value]):
    value: Value
    etag: str
    now: datetime


class AutomationStore(Protocol):
    owner_id: str
    owner_kind: str
    schedule_kind: str
    schedule_prefix: str

    async def read_owner(self, owner: str) -> Stored[AutomationOwner] | None: ...
    async def create_owner(self, value: AutomationOwner) -> bool: ...
    async def write_owner(self, prior: Stored[AutomationOwner], value: AutomationOwner) -> bool: ...
    async def read_schedule(self, owner: str, schedule_id: str) -> Stored[WorkflowSchedule] | None: ...
    async def write_schedule(
        self, prior_owner: Stored[AutomationOwner], owner: AutomationOwner,
        value: WorkflowSchedule, prior: Stored[WorkflowSchedule] | None,
    ) -> bool: ...


def _assert_owner(prior: AutomationOwner, value: AutomationOwner) -> None:
    if (
        value.id != prior.id or value.userId != prior.userId or value.epoch != prior.epoch
        or value.recordKind != prior.recordKind or value.revision != prior.revision + 1
        or value.requestFloor < prior.requestFloor
    ):
        raise AutomationError("state_corrupt", "An owner transition changed its durable identity.")


def _assert_schedule(
    owner: AutomationOwner, value: WorkflowSchedule, prior: WorkflowSchedule | None,
) -> None:
    if value.userId != owner.userId or value.scheduleId not in owner.schedules:
        raise AutomationError("state_corrupt", "The schedule is not admitted by this owner.")
    if prior is not None and (
        prior.id != value.id or prior.scheduleId != value.scheduleId
        or value.revision != prior.revision + 1
        or value.generation < prior.generation
        or (value.generation == prior.generation and value.consumed < prior.consumed)
    ):
        raise AutomationError("state_corrupt", "The schedule transition rewound its identity.")


class CosmosAutomationStore:
    """Borrows the existing workflow container and its existing managed identity."""

    def __init__(self, container: Any) -> None:
        self._container = container
        self.owner_id = AUTOMATION_OWNER_ID
        self.owner_kind = AUTOMATION_OWNER_KIND
        self.schedule_kind = AUTOMATION_SCHEDULE_KIND
        self.schedule_prefix = AUTOMATION_ID_PREFIX + "schedule:"

    @staticmethod
    def _observed(value: Value, raw: Any) -> Stored[Value]:
        etag = raw.get("_etag") if isinstance(raw, dict) else None
        read_headers = getattr(raw, "get_response_headers", None)
        headers = read_headers() if callable(read_headers) else {}
        if not isinstance(headers, Mapping):
            raise AutomationError("storage_unavailable", "Workflow storage headers are unavailable.")
        when = headers.get("Date") or headers.get("date")
        if not isinstance(etag, str) or not etag or not isinstance(when, str):
            raise AutomationError("storage_unavailable", "Workflow storage omitted coordination evidence.")
        try:
            now = utc(parsedate_to_datetime(when))
        except (TypeError, ValueError, OverflowError) as exc:
            raise AutomationError("storage_unavailable", "Workflow storage time is unavailable.") from exc
        return Stored(value, etag, now)

    async def read_owner(self, owner: str) -> Stored[AutomationOwner] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            raw = await self._container.read_item(item=self.owner_id, partition_key=owner)
        except CosmosResourceNotFoundError:
            return None
        result = persisted_model(AutomationOwner, raw)
        if result.userId != owner or result.id != self.owner_id or result.recordKind != self.owner_kind:
            raise AutomationError("state_corrupt", "Workflow coordination ownership does not match.")
        return self._observed(result, raw)

    async def create_owner(self, value: AutomationOwner) -> bool:
        from azure.cosmos.exceptions import CosmosResourceExistsError

        if value.id != self.owner_id or value.recordKind != self.owner_kind:
            raise AutomationError("state_corrupt", "Invalid workflow coordination identity.")
        try:
            await self._container.create_item(writable_body(value))
        except CosmosResourceExistsError:
            return False
        return True

    async def write_owner(self, prior: Stored[AutomationOwner], value: AutomationOwner) -> bool:
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosAccessConditionFailedError

        _assert_owner(prior.value, value)
        try:
            await self._container.replace_item(
                item=value.id, body=writable_body(value), etag=prior.etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosAccessConditionFailedError:
            return False
        return True

    async def read_schedule(self, owner: str, schedule_id: str) -> Stored[WorkflowSchedule] | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        item_id = self.schedule_prefix + schedule_id
        try:
            raw = await self._container.read_item(item=item_id, partition_key=owner)
        except CosmosResourceNotFoundError:
            return None
        value = persisted_model(WorkflowSchedule, raw)
        if (
            value.id != item_id or value.scheduleId != schedule_id
            or value.userId != owner or value.recordKind != self.schedule_kind
        ):
            raise AutomationError("state_corrupt", "Schedule ownership does not match.")
        return self._observed(value, raw)

    async def write_schedule(
        self, prior_owner: Stored[AutomationOwner], owner: AutomationOwner,
        value: WorkflowSchedule, prior: Stored[WorkflowSchedule] | None,
    ) -> bool:
        from azure.cosmos.exceptions import CosmosBatchOperationError

        from ..sessions.cosmos_deletion import batch_failed_at

        _assert_owner(prior_owner.value, owner)
        _assert_schedule(owner, value, prior.value if prior else None)
        if value.id != self.schedule_prefix + value.scheduleId or value.recordKind != self.schedule_kind:
            raise AutomationError("state_corrupt", "Invalid schedule identity.")
        operations = [
            ("replace", (owner.id, writable_body(owner)), {"if_match_etag": prior_owner.etag}),
            ("replace", (value.id, writable_body(value)), {"if_match_etag": prior.etag})
            if prior else ("create", (writable_body(value),), {}),
        ]
        try:
            await self._container.execute_item_batch(
                partition_key=owner.userId,
                batch_operations=[(operation, args, dict(options)) for operation, args, options in operations],
            )
        except CosmosBatchOperationError as exc:
            if any(batch_failed_at(exc, index, 409, 412) for index in (0, 1)):
                return False
            raise
        return True


class InMemoryAutomationStore:
    def __init__(self) -> None:
        self.owner_id = AUTOMATION_OWNER_ID
        self.owner_kind = AUTOMATION_OWNER_KIND
        self.schedule_kind = AUTOMATION_SCHEDULE_KIND
        self.schedule_prefix = AUTOMATION_ID_PREFIX + "schedule:"
        self.owners: dict[str, AutomationOwner] = {}
        self.schedules: dict[tuple[str, str], WorkflowSchedule] = {}
        self._lock = asyncio.Lock()
        self.clock = lambda: datetime.now(timezone.utc)

    async def read_owner(self, owner: str) -> Stored[AutomationOwner] | None:
        async with self._lock:
            value = self.owners.get(owner)
            return Stored(value.model_copy(deep=True), str(value.revision), self.clock()) if value else None

    async def create_owner(self, value: AutomationOwner) -> bool:
        writable_body(value)
        if value.id != self.owner_id or value.recordKind != self.owner_kind:
            raise AutomationError("state_corrupt", "Invalid workflow coordination identity.")
        async with self._lock:
            if value.userId in self.owners:
                return False
            self.owners[value.userId] = value.model_copy(deep=True)
            return True

    async def write_owner(self, prior: Stored[AutomationOwner], value: AutomationOwner) -> bool:
        _assert_owner(prior.value, value)
        writable_body(value)
        async with self._lock:
            current = self.owners.get(value.userId)
            if current is None or current != prior.value or str(current.revision) != prior.etag:
                return False
            self.owners[value.userId] = value.model_copy(deep=True)
            return True

    async def read_schedule(self, owner: str, schedule_id: str) -> Stored[WorkflowSchedule] | None:
        async with self._lock:
            value = self.schedules.get((owner, schedule_id))
            return Stored(value.model_copy(deep=True), str(value.revision), self.clock()) if value else None

    async def write_schedule(
        self, prior_owner: Stored[AutomationOwner], owner: AutomationOwner,
        value: WorkflowSchedule, prior: Stored[WorkflowSchedule] | None,
    ) -> bool:
        _assert_owner(prior_owner.value, owner)
        _assert_schedule(owner, value, prior.value if prior else None)
        if value.id != self.schedule_prefix + value.scheduleId or value.recordKind != self.schedule_kind:
            raise AutomationError("state_corrupt", "Invalid schedule identity.")
        writable_body(owner)
        writable_body(value)
        async with self._lock:
            key = (owner.userId, value.scheduleId)
            current = self.schedules.get(key)
            if self.owners.get(owner.userId) != prior_owner.value:
                return False
            if current != (prior.value if prior else None):
                return False
            self.owners[owner.userId] = owner.model_copy(deep=True)
            self.schedules[key] = value.model_copy(deep=True)
            return True
