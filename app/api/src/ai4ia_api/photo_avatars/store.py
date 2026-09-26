"""Owner-partition photo avatar records and the per-user limit ledger.

Container ``photoAvatars`` (partition ``/userId``), created by
``infra/modules/data.bicep`` only while the feature flag is on. Access goes
through the shared conditional :class:`~ai4ia_api.publishing.store.RecordStore`
(ETag reads, owner-partition atomic batches with fresh per-call options, and
``recordKind`` queries), so the Cosmos and in-memory stores share one contract.

Three record kinds share each owner's partition:

* ``ai4ia.photo_avatar.v1`` -- one per avatar, id = the opaque record id;
* ``ai4ia.photo_avatar.ledger.v1`` -- id ``photo-avatar-ledger``: the current
  record ids plus the rolling 24-hour creation and report timestamps. A create
  or report is written in the same batch as its ledger change and a delete
  removes in the same batch, so every limit holds under concurrency;
* ``ai4ia.photo_avatar.report.v1`` -- user reports, kept after an avatar is
  deleted and expired by a 90-day item TTL (the container's ``defaultTtl`` is
  ``-1``, so nothing else ever expires).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import Environment, SessionStoreKind, Settings
from ..publishing.store import (
    CosmosRecordStore,
    InMemoryRecordStore,
    RecordQuery,
    RecordSnapshot,
    RecordStore,
)
from .models import PhotoAvatarStatus, ReportReason

logger = logging.getLogger(__name__)

CONTAINER = "photoAvatars"
RECORD_KIND = "ai4ia.photo_avatar.v1"
LEDGER_KIND = "ai4ia.photo_avatar.ledger.v1"
REPORT_KIND = "ai4ia.photo_avatar.report.v1"
LEDGER_ID = "photo-avatar-ledger"
REPORT_TTL_SECONDS = 90 * 24 * 3600
DAY = timedelta(days=1)
MAX_CAS_ATTEMPTS = 6
MAX_LISTED = 200


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Persisted(BaseModel):
    # Cosmos system fields are stripped by the record store; anything else
    # unexpected is refused rather than silently round-tripped.
    model_config = ConfigDict(extra="forbid")


class PreviewMeta(_Persisted):
    width: int
    height: int
    bytes: int
    sha256Prefix: str = Field(max_length=16)


class CostSnapshot(_Persisted):
    currency: str = "USD"
    estimatedMicroUsd: int | None = None
    known: bool = False
    priceVersion: str | None = None


class PhotoAvatarRecord(_Persisted):
    id: str
    userId: str
    recordKind: Literal["ai4ia.photo_avatar.v1"] = RECORD_KIND
    schemaVersion: Literal[1] = 1
    providerAvatarId: str
    homeRegion: str
    displayName: str
    prompt: str
    attributes: dict[str, str | None]
    attestationVersion: str
    attestedAt: datetime
    status: PhotoAvatarStatus
    providerState: str | None = None
    # True accepted, False definitely not accepted, None unknown.
    providerAccepted: bool | None = None
    failureCode: str | None = None
    providerErrorCode: str | None = None
    preview: PreviewMeta | None = None
    cost: CostSnapshot = Field(default_factory=CostSnapshot)
    metered: bool = False
    createdAt: datetime
    updatedAt: datetime
    dispatchedAt: datetime | None = None
    readyAt: datetime | None = None
    lastReconciledAt: datetime | None = None
    deleteRequestedAt: datetime | None = None
    reportedAt: datetime | None = None
    # Set when a live session reported that the avatar failed verification.
    # Cleared only by a later provider read that still finds it Succeeded.
    liveVerificationFailedAt: datetime | None = None
    liveVerificationCode: str | None = Field(default=None, max_length=80)
    correlationId: str | None = Field(default=None, max_length=128)


class PhotoAvatarLedger(_Persisted):
    id: Literal["photo-avatar-ledger"] = LEDGER_ID
    userId: str
    recordKind: Literal["ai4ia.photo_avatar.ledger.v1"] = LEDGER_KIND
    schemaVersion: Literal[1] = 1
    active: list[str] = Field(default_factory=list)
    creations: list[datetime] = Field(default_factory=list)
    reports: list[datetime] = Field(default_factory=list)

    def recent(self, now: datetime) -> list[datetime]:
        return sorted(moment for moment in self.creations if moment > now - DAY)

    def recent_reports(self, now: datetime) -> list[datetime]:
        return sorted(moment for moment in self.reports if moment > now - DAY)


class PhotoAvatarReport(_Persisted):
    id: str
    userId: str
    recordKind: Literal["ai4ia.photo_avatar.report.v1"] = REPORT_KIND
    schemaVersion: Literal[1] = 1
    avatarId: str
    reason: ReportReason
    details: str | None = None
    createdAt: datetime
    ttl: int = REPORT_TTL_SECONDS


class LimitReached(Exception):
    """A per-user limit refused a write. ``retry_after`` is set for rolling daily caps."""

    def __init__(
        self, kind: Literal["max_avatars", "daily", "reports"], retry_after: int | None = None,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retry_after = retry_after


class StoreConflict(Exception):
    """Repeated concurrent changes kept a conditional write from applying."""


def _body(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def next_creation_at(ledger: PhotoAvatarLedger, now: datetime, max_per_day: int) -> datetime | None:
    recent = ledger.recent(now)
    if len(recent) < max_per_day:
        return None
    return recent[len(recent) - max_per_day] + DAY


def _seconds_until(moment: datetime, now: datetime) -> int:
    return max(1, int((moment - now).total_seconds()) + 1)


class PhotoAvatarStore:
    def __init__(self, records: RecordStore, *, closer: Any = None) -> None:
        self._records = records
        self._closer = closer

    async def list(self, user_id: str) -> list[PhotoAvatarRecord]:
        rows = await self._records.query(
            RecordQuery(kind=RECORD_KIND, owner_id=user_id, limit=MAX_LISTED),
        )
        records = [PhotoAvatarRecord.model_validate(row.body) for row in rows]
        records = [record for record in records if record.userId == user_id]
        return sorted(records, key=lambda record: record.createdAt, reverse=True)

    async def get(self, user_id: str, record_id: str) -> tuple[PhotoAvatarRecord, RecordSnapshot] | None:
        snapshot = await self._records.read(user_id, record_id)
        if snapshot is None or snapshot.body.get("recordKind") != RECORD_KIND:
            return None
        record = PhotoAvatarRecord.model_validate(snapshot.body)
        # Defense in depth: the partition already scopes reads to the owner.
        if record.userId != user_id or record.id != record_id:
            return None
        return record, snapshot

    async def _ledger(self, user_id: str) -> tuple[PhotoAvatarLedger, RecordSnapshot | None]:
        snapshot = await self._records.read(user_id, LEDGER_ID)
        if snapshot is None:
            return PhotoAvatarLedger(userId=user_id), None
        return PhotoAvatarLedger.model_validate(snapshot.body), snapshot

    async def ledger(self, user_id: str) -> PhotoAvatarLedger:
        ledger, _ = await self._ledger(user_id)
        return ledger

    async def reserve(
        self, record: PhotoAvatarRecord, *, max_avatars: int, max_per_day: int, now: datetime,
    ) -> None:
        """Atomically check both limits, count the creation and create the record."""
        for _ in range(MAX_CAS_ATTEMPTS):
            ledger, snapshot = await self._ledger(record.userId)
            recent = ledger.recent(now)
            if len(ledger.active) >= max_avatars:
                raise LimitReached("max_avatars")
            if len(recent) >= max_per_day:
                reopen = next_creation_at(ledger, now, max_per_day)
                raise LimitReached("daily", _seconds_until(reopen, now) if reopen else None)
            updated = ledger.model_copy(update={
                "active": [*ledger.active, record.id],
                "creations": [*recent, now],
            })
            if await self._records.atomic(
                record.userId,
                {LEDGER_ID: snapshot, record.id: None},
                {LEDGER_ID: _body(updated), record.id: _body(record)},
            ):
                return
        raise StoreConflict("Concurrent photo avatar changes kept the reservation from applying.")

    async def release(self, user_id: str, record_id: str, *, created_at: datetime) -> None:
        """Undo a reservation whose create was refused before it was sent."""
        for _ in range(MAX_CAS_ATTEMPTS):
            ledger, ledger_snapshot = await self._ledger(user_id)
            loaded = await self.get(user_id, record_id)
            expected: dict[str, RecordSnapshot | None] = {}
            changes: dict[str, dict[str, Any] | None] = {}
            if loaded is not None:
                expected[record_id] = loaded[1]
                changes[record_id] = None
            if ledger_snapshot is not None:
                creations = list(ledger.creations)
                if created_at in creations:
                    creations.remove(created_at)
                expected[LEDGER_ID] = ledger_snapshot
                changes[LEDGER_ID] = _body(ledger.model_copy(update={
                    "active": [item for item in ledger.active if item != record_id],
                    "creations": creations,
                }))
            if not expected or await self._records.atomic(user_id, expected, changes):
                return
        raise StoreConflict("Concurrent photo avatar changes kept the release from applying.")

    async def replace(self, snapshot: RecordSnapshot, record: PhotoAvatarRecord) -> bool:
        return await self._records.atomic(
            record.userId, {record.id: snapshot}, {record.id: _body(record)},
        )

    async def remove(self, user_id: str, record_id: str) -> None:
        """Delete the record and drop it from the ledger. Idempotent."""
        for _ in range(MAX_CAS_ATTEMPTS):
            ledger, ledger_snapshot = await self._ledger(user_id)
            loaded = await self.get(user_id, record_id)
            expected: dict[str, RecordSnapshot | None] = {}
            changes: dict[str, dict[str, Any] | None] = {}
            if loaded is not None:
                expected[record_id] = loaded[1]
                changes[record_id] = None
            if ledger_snapshot is not None and record_id in ledger.active:
                expected[LEDGER_ID] = ledger_snapshot
                changes[LEDGER_ID] = _body(ledger.model_copy(update={
                    "active": [item for item in ledger.active if item != record_id],
                }))
            if not expected or await self._records.atomic(user_id, expected, changes):
                return
        raise StoreConflict("Concurrent photo avatar changes kept the deletion from applying.")

    async def add_report(self, report: PhotoAvatarReport, *, max_per_day: int, now: datetime) -> None:
        """Create a report and count it against the rolling daily report cap atomically."""
        for _ in range(MAX_CAS_ATTEMPTS):
            ledger, snapshot = await self._ledger(report.userId)
            recent = ledger.recent_reports(now)
            if len(recent) >= max_per_day:
                raise LimitReached("reports", _seconds_until(recent[0] + DAY, now))
            updated = ledger.model_copy(update={"reports": [*recent, now]})
            if await self._records.atomic(
                report.userId,
                {LEDGER_ID: snapshot, report.id: None},
                {LEDGER_ID: _body(updated), report.id: _body(report)},
            ):
                return
        raise StoreConflict("Concurrent photo avatar changes kept the report from applying.")

    async def close(self) -> None:
        if self._closer is not None:
            await self._closer()


class _CosmosRecords:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        container = self._client.get_database_client(database).get_container_client(CONTAINER)
        self.records = CosmosRecordStore(container)

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()


def build_photo_avatar_store(settings: Settings) -> PhotoAvatarStore:
    """Cosmos when configured (required outside local), else in-memory."""
    if settings.session_store == SessionStoreKind.cosmos and settings.cosmos_endpoint:
        cosmos = _CosmosRecords(settings.cosmos_endpoint, settings.cosmos_database)
        return PhotoAvatarStore(cosmos.records, closer=cosmos.close)
    if settings.env != Environment.local:
        logger.warning("photo avatars: using the in-memory store outside local")
    return PhotoAvatarStore(InMemoryRecordStore())
