"""Orchestration for custom photo avatars.

Create: validate against the catalog -> the shared availability predicate ->
the entitlement gate -> a cost-cap guard (an unknown per-avatar price refuses
under any cost cap) -> ensure the avatar project -> an atomic reservation in the
owner ledger -> one admitted, single-attempt provider create -> classify ->
meter once -> record. The dispatch runs shielded, so a client that disconnects
cannot strand the record without its recorded outcome.

A create whose acceptance is unknown becomes ``confirming``. It is never sent
again: a later status read adopts what the provider reports, and only a
provider 404 well after the proxy could still have sent it (``CONFIRM_GRACE``)
turns it into ``failed/not_created``. Status reads poll the provider at most
once per call and at a bounded rate per record; they copy the preview into Blob
as soon as the provider reports success.

Reads, status, preview, deletion and reports never depend on the Limited Access
capability, so a record stays visible and deletable when access changes.
Nothing here deletes a record automatically.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from ..auth.base import AuthenticatedUser
from ..config import Settings
from ..entitlements.service import EntitlementService
from ..logging_setup import emit_custom_event, get_correlation_id
from ..policy.context import current_binding, require_bound_policy, require_policy
from ..policy.models import PolicyError, PolicyRequest
from ..policy.service import PolicyService
from ..usage.models import PHOTO_AVATAR_PROVIDER, PHOTO_AVATAR_TARGET, TokenUsage, UsageTarget
from ..usage.pricing import PricingBook
from ..usage.service import UsageService
from .availability import (
    Availability,
    CapabilityProbe,
    PolicyState,
    evaluate_availability,
    policy_state,
)
from .catalog import ATTRIBUTE_NAMES, PhotoAvatarCatalog, PhotoAvatarPreviewCatalog
from .live import LiveAvatarError, LiveAvatarGrant
from .models import (
    ATTESTATION_VERSION,
    FAILURE_MESSAGES,
    RECORD_ID_PATTERN,
    TERMINAL_STATUSES,
    CreatePhotoAvatarRequest,
    PhotoAvatar,
    PhotoAvatarAttributeOptions,
    PhotoAvatarAttributes,
    PhotoAvatarConfig,
    PhotoAvatarCost,
    PhotoAvatarError,
    PhotoAvatarFailure,
    PhotoAvatarLimits,
    PhotoAvatarList,
    PhotoAvatarPreview,
    PhotoAvatarPricing,
    PhotoAvatarReportReceipt,
    PhotoAvatarReportRequest,
    attestation_info,
    disclosure_info,
    feedback_info,
)
from .preview import (
    BlobNotFoundError,
    PhotoAvatarArtifactStore,
    PreviewImage,
    PreviewRejected,
    PreviewUnavailable,
    fetch_preview,
)
from .provider import (
    CREATE_PROXY_TTL_SECONDS,
    CreateOutcome,
    PhotoAvatarGateway,
    PreviewLink,
    build_create_body,
    new_provider_avatar_id,
    valid_provider_avatar_id,
)
from .store import (
    CostSnapshot,
    LimitReached,
    PhotoAvatarLedger,
    PhotoAvatarRecord,
    PhotoAvatarReport,
    PhotoAvatarStore,
    PreviewMeta,
    next_creation_at,
    utcnow,
)

logger = logging.getLogger(__name__)

# An unknown create is only treated as absent after the proxy can no longer
# send it (its TTL) plus a wide margin for provider-side propagation.
CONFIRM_GRACE = timedelta(seconds=CREATE_PROXY_TTL_SECONDS) + timedelta(minutes=4)
FAST_POLL = timedelta(seconds=3)
SLOW_POLL = timedelta(seconds=30)
FAST_POLL_WINDOW = timedelta(minutes=10)
# After a live session reports a verification failure, no re-check (and so no
# new live session) runs for this long: an auto-reconnecting client costs at
# most one upstream session per avatar per cooldown.
REVERIFY_COOLDOWN = timedelta(minutes=5)
MAX_REPORTS_PER_DAY = 20
USAGE_SESSION_ID = "photo-avatars"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RECORD_ID = re.compile(RECORD_ID_PATTERN)
_PROVIDER_CODE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

PreviewFetcher = Callable[[PreviewLink, PhotoAvatarPreviewCatalog], Awaitable[PreviewImage]]


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units: the unit the APIM policy's C# check measures."""
    return len(text.encode("utf-16-le")) // 2


def _micro_to_usd(value: int | None) -> float | None:
    return round(value / 1_000_000, 6) if value is not None else None


def _log_background(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("photo avatar dispatch finished with an error after its request ended")


class PhotoAvatarService:
    def __init__(
        self,
        *,
        settings: Settings,
        catalog: PhotoAvatarCatalog,
        store: PhotoAvatarStore,
        artifacts: PhotoAvatarArtifactStore,
        gateway: PhotoAvatarGateway,
        capability: CapabilityProbe,
        entitlements: EntitlementService,
        usage: UsageService,
        pricing: PricingBook,
        clock: Callable[[], datetime] = utcnow,
        preview_fetcher: PreviewFetcher | None = None,
        policy: PolicyService | None = None,
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._store = store
        self._artifacts = artifacts
        self._gateway = gateway
        self._capability = capability
        self._entitlements = entitlements
        self._usage = usage
        self._pricing = pricing
        self._clock = clock
        self._fetch: PreviewFetcher = preview_fetcher or fetch_preview
        self._policy = policy
        self._project_ready = False
        self._project_lock = asyncio.Lock()
        self._background: set[asyncio.Task[Any]] = set()

    # --- availability -------------------------------------------------------

    async def availability(
        self,
        operation: str = "avatar.create",
        *,
        policy: Callable[[], Awaitable[PolicyState]] | None = None,
    ) -> Availability:
        """The one availability predicate. ``policy`` replaces the display snapshot."""
        use = "avatar.use" if operation == "avatar.use" else "avatar.create"
        return await evaluate_availability(
            enabled=self._settings.photo_avatars_enabled,
            storage_ready=True,
            residency_ok=self._catalog.satisfies_residency(
                (self._settings.data_residency or "").strip().lower(),
            ),
            policy=policy or (lambda: policy_state(use)),
            capability=self._capability.status,
        )

    async def _enforced_use_policy(self, owner_id: str) -> PolicyState:
        """Enforce ``avatar.use`` for the bound caller, as execution does.

        Unlike the display snapshot, an enabled policy service with no binding
        for this request refuses (``reauthentication_required``), so a caller
        that forgot to bind policy cannot turn the check into an allow.
        """
        binding = current_binding()
        if binding is not None and binding.owner_id != owner_id:
            return "denied"
        request = PolicyRequest("avatar.use")
        try:
            if self._policy is not None:
                await require_bound_policy(self._policy, request)
            elif self._settings.group_policy_enabled:
                return "unavailable"
            else:
                await require_policy(request, owner_id=owner_id)
        except PolicyError as exc:
            return "unavailable" if exc.decision.outcome == "unavailable" else "denied"
        return "allowed"

    async def _usable_now(self) -> bool:
        return (await self.availability("avatar.use")).available

    # --- reads --------------------------------------------------------------

    async def config(self, user: AuthenticatedUser) -> PhotoAvatarConfig:
        availability = await self.availability()
        ledger = await self._store.ledger(user.internal_user_id)
        now = self._clock()
        count, recent = len(ledger.active), len(ledger.recent(now))
        estimate = self._pricing.estimate_avatar(self._catalog.billingModelId)
        return PhotoAvatarConfig(
            enabled=True,
            available=availability.available,
            reason=availability.reason,
            canCreate=(
                availability.available
                and count < self._settings.photo_avatar_max_per_user
                and recent < self._settings.photo_avatar_max_creations_per_day
            ),
            limits=PhotoAvatarLimits(
                maxAvatars=self._settings.photo_avatar_max_per_user,
                avatarCount=count,
                maxCreationsPerDay=self._settings.photo_avatar_max_creations_per_day,
                creationsInLastDay=recent,
                nextCreationAt=next_creation_at(
                    ledger, now, self._settings.photo_avatar_max_creations_per_day,
                ),
                promptMaxChars=self._catalog.promptMaxChars,
            ),
            attributes=PhotoAvatarAttributeOptions(
                **{name: list(self._catalog.attributes.options(name)) for name in ATTRIBUTE_NAMES},
            ),
            attestation=attestation_info(),
            disclosure=disclosure_info(),
            pricing=PhotoAvatarPricing(
                currency=estimate.currency,
                estimatedUsdPerAvatar=_micro_to_usd(estimate.micro_usd) if estimate.known else None,
                known=estimate.known,
                priceVersion=estimate.version,
            ),
            feedback=feedback_info(),
        )

    async def list(self, user: AuthenticatedUser) -> PhotoAvatarList:
        records = await self._store.list(user.internal_user_id)
        usable = await self._usable_now() if any(r.status == "ready" for r in records) else False
        return PhotoAvatarList(avatars=[self._view(record, usable) for record in records])

    async def get(self, user: AuthenticatedUser, avatar_id: str) -> PhotoAvatar:
        record = await self._reconcile(await self._owned(user, avatar_id))
        if record.status == "ready" and record.liveVerificationFailedAt is not None:
            record = await self._reverify(record)
        usable = await self._usable_now() if record.status == "ready" else False
        return self._view(record, usable)

    async def preview(self, user: AuthenticatedUser, avatar_id: str) -> bytes:
        record = await self._owned(user, avatar_id)
        if record.status != "ready" or record.preview is None:
            raise PhotoAvatarError(404, "not_found", "This avatar has no preview yet.")
        try:
            return await self._artifacts.get(user.internal_user_id, avatar_id)
        except BlobNotFoundError as exc:
            raise PhotoAvatarError(404, "not_found", "Not found.") from exc

    async def _owned(self, user: AuthenticatedUser, avatar_id: str) -> PhotoAvatarRecord:
        loaded = await self._store.get(user.internal_user_id, avatar_id)
        if loaded is None:
            raise PhotoAvatarError(404, "not_found", "Not found.")
        return loaded[0]

    # --- create -------------------------------------------------------------

    async def create(
        self, user: AuthenticatedUser, request: CreatePhotoAvatarRequest,
    ) -> PhotoAvatar:
        owner = user.internal_user_id
        display_name, prompt, attributes = self._validated(request)
        availability = await self.availability()
        if not availability.available:
            raise self._unavailable(availability)
        decision = await self._entitlements.check(owner)
        if not decision.allowed:
            raise PhotoAvatarError(
                decision.code,
                "entitlement_denied" if decision.code == 403
                else "rate_limited" if decision.code == 429 else "entitlements_unavailable",
                decision.reason or "Photo avatar creation is not permitted.",
                retry_after=decision.retry_after_seconds,
            )
        estimate = self._pricing.estimate_avatar(self._catalog.billingModelId)
        if not estimate.known and await self._cost_capped(owner):
            raise PhotoAvatarError(
                503, "cost_unknown_under_cap",
                "The photo avatar price is unknown, so it cannot be admitted under a cost limit.",
            )
        now = self._clock()
        self._check_limits(await self._store.ledger(owner), now)
        if not await self._ensure_project():
            raise PhotoAvatarError(
                503, "avatar_project_unavailable", "The avatar project is not available right now.",
            )
        correlation = self._correlation()
        record = PhotoAvatarRecord(
            id=uuid.uuid4().hex,
            userId=owner,
            providerAvatarId=new_provider_avatar_id(),
            homeRegion=self._catalog.homeRegion,
            displayName=display_name,
            prompt=prompt,
            attributes=attributes,
            attestationVersion=request.attestation.version,
            attestedAt=now,
            status="creating",
            cost=CostSnapshot(
                currency=estimate.currency,
                estimatedMicroUsd=estimate.micro_usd if estimate.known else None,
                known=estimate.known,
                priceVersion=estimate.version,
            ),
            createdAt=now,
            updatedAt=now,
            correlationId=correlation,
        )
        try:
            await self._store.reserve(
                record,
                max_avatars=self._settings.photo_avatar_max_per_user,
                max_per_day=self._settings.photo_avatar_max_creations_per_day,
                now=now,
            )
        except LimitReached as exc:
            raise self._limit_error(exc) from exc
        task = asyncio.ensure_future(self._dispatch(record, prompt, attributes, created_at=now))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_background)
        return self._view(await asyncio.shield(task), usable=False)

    async def _dispatch(
        self, record: PhotoAvatarRecord, prompt: str, attributes: dict[str, str | None],
        *, created_at: datetime,
    ) -> PhotoAvatarRecord:
        try:
            outcome = await self._gateway.create_avatar(
                record.providerAvatarId, build_create_body(prompt, attributes),
                correlation_id=record.correlationId,
            )
        except Exception:
            # Refused before anything was sent (hard quota or policy admission).
            await self._release(record, created_at)
            raise
        if outcome.kind == "not_sent":
            # The proxy was never reached, so nothing was created anywhere; do
            # not spend the user's daily creation on an infrastructure fault.
            await self._release(record, created_at)
            raise PhotoAvatarError(
                503, "photo_avatar_gateway_unavailable",
                "The avatar service could not be reached. Nothing was created; try again.",
            )
        now = self._clock()
        fields = self._outcome_fields(outcome, now)
        if outcome.kind in {"accepted", "unknown"}:
            fields["metered"] = await self._meter(record, accepted=outcome.kind == "accepted")
        else:
            fields["metered"] = True  # definitely not accepted: nothing to meter
        logger.info(
            "photo avatar create outcome=%s status=%s record=%s",
            outcome.kind, outcome.status, record.id[:8],
        )
        updated = await self._apply(
            record.userId, record.id, fields, only_if=lambda current: current.status == "creating",
        )
        return updated or record

    async def _release(self, record: PhotoAvatarRecord, created_at: datetime) -> None:
        try:
            await self._store.release(record.userId, record.id, created_at=created_at)
        except Exception:  # noqa: BLE001 - reconciliation marks a stale reservation not_created
            logger.warning("photo avatar reservation release failed record=%s", record.id[:8])

    def _outcome_fields(self, outcome: CreateOutcome, now: datetime) -> dict[str, Any]:
        if outcome.kind == "accepted":
            avatar = outcome.avatar
            if avatar is not None and avatar.state == "failed":
                return {
                    "status": "failed", "providerAccepted": True, "dispatchedAt": now,
                    "failureCode": "provider_failed", "providerState": avatar.raw_state,
                    "providerErrorCode": avatar.error_code,
                }
            return {
                "status": "generating", "providerAccepted": True, "dispatchedAt": now,
                "providerState": avatar.raw_state if avatar else None,
            }
        if outcome.kind == "rejected":
            status = outcome.status or 0
            if status in {401, 403}:
                self._capability.invalidate()
            if status == 404:
                self._project_ready = False
            return {
                "status": "failed", "providerAccepted": False, "dispatchedAt": now,
                "failureCode": {
                    429: "provider_throttled", 401: "provider_forbidden",
                    403: "provider_forbidden", 404: "project_unavailable",
                }.get(status, "provider_rejected"),
                "providerErrorCode": outcome.error_code,
            }
        return {
            "status": "confirming", "providerAccepted": None, "dispatchedAt": now,
            "providerErrorCode": outcome.error_code,
        }

    async def _meter(self, record: PhotoAvatarRecord, *, accepted: bool) -> bool:
        """One ledger row per dispatched create; the first row wins."""
        return await self._usage.record_operation_once(
            record_id=f"photo-avatar-create-{record.id}",
            user_id=record.userId,
            session_id=USAGE_SESSION_ID,
            model_id=self._catalog.billingModelId,
            target=UsageTarget.managed_service(
                provider=PHOTO_AVATAR_PROVIDER, target=PHOTO_AVATAR_TARGET,
                region=self._catalog.homeRegion,
            ),
            usage=TokenUsage(known=False, complete=False, calls=1),
            status="complete" if accepted else "error",
            provider_completed=accepted,
            correlation_id=record.correlationId,
            billable_units=1 if accepted else None,
            billing_unit="avatar" if accepted else None,
        )

    # --- reconcile ----------------------------------------------------------

    async def _reconcile(self, record: PhotoAvatarRecord) -> PhotoAvatarRecord:
        if record.status in TERMINAL_STATUSES or record.status == "deleting":
            return record
        now = self._clock()
        interval = FAST_POLL if now - record.createdAt < FAST_POLL_WINDOW else SLOW_POLL
        if record.lastReconciledAt is not None and now - record.lastReconciledAt < interval:
            return record
        read = await self._gateway.get_avatar(
            record.providerAvatarId, correlation_id=self._correlation(),
        )
        fields: dict[str, Any] = {"lastReconciledAt": now}
        if read.kind == "absent":
            if record.status in {"creating", "confirming"}:
                if now - (record.dispatchedAt or record.createdAt) >= CONFIRM_GRACE:
                    fields.update(status="failed", failureCode="not_created", providerAccepted=False)
            else:
                fields.update(status="failed", failureCode="provider_missing")
        elif read.kind == "found" and read.avatar is not None:
            avatar = read.avatar
            fields["providerAccepted"] = True
            fields["providerState"] = avatar.raw_state
            if not record.metered:
                fields["metered"] = await self._meter(record, accepted=True)
            if avatar.state == "failed":
                fields.update(
                    status="failed", failureCode="provider_failed", providerErrorCode=avatar.error_code,
                )
            elif avatar.state == "succeeded" and avatar.preview is not None:
                fields.update(await self._copy_preview(record, avatar.preview, now))
            else:
                fields["status"] = "generating"
        updated = await self._apply(
            record.userId, record.id, fields,
            only_if=lambda current: current.status not in TERMINAL_STATUSES
            and current.status != "deleting",
        )
        return updated or record

    async def _copy_preview(
        self, record: PhotoAvatarRecord, link: PreviewLink, now: datetime,
    ) -> dict[str, Any]:
        try:
            image = await self._fetch(link, self._catalog.preview)
        except PreviewRejected as exc:
            logger.warning("photo avatar preview rejected code=%s record=%s", exc.code, record.id[:8])
            return {"status": "failed", "failureCode": "preview_rejected"}
        except PreviewUnavailable as exc:
            logger.info("photo avatar preview unavailable code=%s record=%s", exc.code, record.id[:8])
            return {"status": "generating"}
        try:
            await self._artifacts.put(record.userId, record.id, image.data)
        except Exception:  # noqa: BLE001 - retried on the next status read
            logger.warning("photo avatar preview store failed record=%s", record.id[:8])
            return {"status": "generating"}
        return {
            "status": "ready",
            "readyAt": now,
            "preview": PreviewMeta(
                width=image.width, height=image.height, bytes=len(image.data),
                sha256Prefix=image.sha256[:16],
            ),
        }

    # --- live sessions (server-only; see live.py) ----------------------------

    async def resolve_live_avatar(self, user: AuthenticatedUser, record_id: str) -> LiveAvatarGrant:
        """Grant one live session the right to name ``record_id``, or refuse.

        Order: id shape and ownership (indistinguishable not-found), then the
        record state, then a pending re-verification, then the full
        availability predicate with ``avatar.use`` enforced.
        """
        owner = user.internal_user_id
        if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
            raise LiveAvatarError(404, "not_found", "Not found.")
        loaded = await self._store.get(owner, record_id)
        if loaded is None:
            raise LiveAvatarError(404, "not_found", "Not found.")
        record = loaded[0]
        if (
            record.status != "ready"
            or record.preview is None
            or not valid_provider_avatar_id(record.providerAvatarId)
        ):
            raise LiveAvatarError(409, "avatar_not_ready", "This avatar is not ready.")
        if record.homeRegion != self._catalog.homeRegion:
            raise LiveAvatarError(
                409, "avatar_home_changed",
                "This avatar belongs to a different avatar home than the one in use.",
            )
        if record.liveVerificationFailedAt is not None:
            record = await self._reverify(record)
            if record.liveVerificationFailedAt is not None or record.status != "ready":
                raise self._reverification_refusal(record)
        availability = await self.availability(
            "avatar.use", policy=lambda: self._enforced_use_policy(owner),
        )
        if availability.reason == "policy_denied":
            raise LiveAvatarError(
                403, "policy_denied", "Using photo avatars is not permitted for your account.",
            )
        if not availability.available:
            raise LiveAvatarError(
                503, "photo_avatars_unavailable", "Photo avatars are unavailable.",
                reason=availability.reason,
            )
        return LiveAvatarGrant(
            record_id=record.id,
            provider_avatar_id=record.providerAvatarId,
            base_model=self._catalog.baseModel,
            home_region=record.homeRegion,
        )

    async def mark_live_avatar_verification_failed(
        self, user: AuthenticatedUser, record_id: str, *, provider_code: str | None = None,
    ) -> bool:
        owner = user.internal_user_id
        if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
            return False
        loaded = await self._store.get(owner, record_id)
        if loaded is None or loaded[0].status != "ready":
            return False
        code = provider_code if isinstance(provider_code, str) and _PROVIDER_CODE.fullmatch(provider_code) else None
        updated = await self._apply(
            owner, record_id,
            {"liveVerificationFailedAt": self._clock(), "liveVerificationCode": code},
            only_if=lambda current: current.status == "ready",
        )
        marked = updated is not None and updated.liveVerificationFailedAt is not None
        if marked:
            logger.warning(
                "photo avatar live verification failed record=%s code=%s", record_id[:8], code,
            )
            emit_custom_event("photo_avatar_live_verification_failed", {
                "avatar": record_id[:8], "code": code, "correlationId": self._correlation(),
            })
        return marked

    async def _reverify(self, record: PhotoAvatarRecord) -> PhotoAvatarRecord:
        """Clear a verification failure only on a fresh provider read after the cooldown.

        A provider ``Succeeded`` clears it; ``Failed`` or 404 make the record
        terminal (the owner can still see and delete it); anything else keeps
        the flag so live use stays refused. The record, its stored preview and
        cleanup are never withdrawn by this check.
        """
        failed_at = record.liveVerificationFailedAt
        if failed_at is None or self._clock() - failed_at < REVERIFY_COOLDOWN:
            return record
        read = await self._gateway.get_avatar(
            record.providerAvatarId, correlation_id=self._correlation(),
        )
        fields: dict[str, Any] = {"lastReconciledAt": self._clock()}
        if read.kind == "found" and read.avatar is not None and read.avatar.state == "succeeded":
            fields.update(liveVerificationFailedAt=None, liveVerificationCode=None)
        elif read.kind == "found" and read.avatar is not None and read.avatar.state == "failed":
            fields.update(
                status="failed", failureCode="provider_failed", providerErrorCode=read.avatar.error_code,
            )
        elif read.kind == "absent":
            fields.update(status="failed", failureCode="provider_missing")
        else:
            # Unknown or pending: keep refusing, and wait another cooldown.
            fields["liveVerificationFailedAt"] = self._clock()
        updated = await self._apply(
            record.userId, record.id, fields,
            only_if=lambda current: current.liveVerificationFailedAt == failed_at,
        )
        return updated or record

    def _reverification_refusal(self, record: PhotoAvatarRecord) -> LiveAvatarError:
        if record.status != "ready":
            return LiveAvatarError(409, "avatar_not_ready", "This avatar is no longer available.")
        failed_at = record.liveVerificationFailedAt or self._clock()
        remaining = REVERIFY_COOLDOWN - (self._clock() - failed_at)
        return LiveAvatarError(
            409, "avatar_needs_reverification",
            "The avatar service could not verify this avatar. Try again later.",
            retry_after=max(1, math.ceil(remaining.total_seconds())),
        )

    # --- delete and report --------------------------------------------------

    async def delete(self, user: AuthenticatedUser, avatar_id: str) -> None:
        owner = user.internal_user_id
        loaded = await self._store.get(owner, avatar_id)
        if loaded is None:
            # Idempotent; also drops a ledger entry left by an interrupted delete.
            await self._store.remove(owner, avatar_id)
            return
        record = loaded[0]
        now = self._clock()
        if record.status in {"creating", "confirming"}:
            remaining = CONFIRM_GRACE - (now - (record.dispatchedAt or record.createdAt))
            if remaining > timedelta(0):
                # The provider may still accept the create; deleting now could
                # leave a billable avatar that no record points to.
                raise PhotoAvatarError(
                    409, "avatar_confirming",
                    "This avatar's creation is still being confirmed. Try again shortly.",
                    retry_after=max(1, math.ceil(remaining.total_seconds())),
                )
        if record.status != "deleting":
            record = await self._apply(
                owner, avatar_id, {"status": "deleting", "deleteRequestedAt": now},
            ) or record
        result = await self._gateway.delete_avatar(
            record.providerAvatarId, correlation_id=self._correlation(),
        )
        if result not in {"deleted", "absent"}:
            raise PhotoAvatarError(
                502, "provider_delete_failed",
                "The avatar service did not confirm deletion. Delete again to finish.",
            )
        try:
            await self._artifacts.delete(owner, avatar_id)
        except Exception as exc:  # noqa: BLE001 - the record stays so a retry finishes
            raise PhotoAvatarError(
                503, "delete_incomplete", "Deletion did not finish. Delete again to finish.",
            ) from exc
        await self._store.remove(owner, avatar_id)
        logger.info("photo avatar deleted record=%s provider=%s", avatar_id[:8], result)

    async def report(
        self, user: AuthenticatedUser, avatar_id: str, request: PhotoAvatarReportRequest,
    ) -> PhotoAvatarReportReceipt:
        owner = user.internal_user_id
        await self._owned(user, avatar_id)
        now = self._clock()
        details = request.details.strip() if request.details else None
        report = PhotoAvatarReport(
            id=f"report-{secrets.token_hex(10)}",
            userId=owner,
            avatarId=avatar_id,
            reason=request.reason,
            details=details or None,
            createdAt=now,
        )
        try:
            await self._store.add_report(report, max_per_day=MAX_REPORTS_PER_DAY, now=now)
        except LimitReached as exc:
            raise PhotoAvatarError(
                429, "report_limit", "Too many reports today. Try again later.",
                retry_after=exc.retry_after,
            ) from exc
        await self._apply(owner, avatar_id, {"reportedAt": now})
        # Content-free: reason and short prefixes only, never the details or prompt.
        emit_custom_event("photo_avatar_report", {
            "reason": request.reason, "avatar": avatar_id[:8], "correlationId": self._correlation(),
        })
        logger.info("photo avatar report reason=%s record=%s", request.reason, avatar_id[:8])
        return PhotoAvatarReportReceipt(
            id=report.id, avatarId=avatar_id, reason=request.reason, createdAt=now,
        )

    # --- helpers ------------------------------------------------------------

    def _validated(
        self, request: CreatePhotoAvatarRequest,
    ) -> tuple[str, str, dict[str, str | None]]:
        if request.attestation.version != ATTESTATION_VERSION:
            raise PhotoAvatarError(
                422, "attestation_outdated",
                "The attestation is for a different version. Review it again and resubmit.",
            )
        display_name = request.displayName.strip()
        if not display_name or not display_name.isprintable():
            raise PhotoAvatarError(422, "invalid_photo_avatar_request", "displayName is not valid.")
        prompt = request.prompt.strip()
        if not prompt or _CONTROL.search(prompt):
            raise PhotoAvatarError(422, "invalid_photo_avatar_request", "prompt is not valid.")
        if utf16_length(prompt) > self._catalog.promptMaxChars:
            raise PhotoAvatarError(
                422, "invalid_photo_avatar_request",
                f"prompt must be at most {self._catalog.promptMaxChars} characters.",
            )
        attributes: dict[str, str | None] = {}
        for name in ATTRIBUTE_NAMES:
            value = getattr(request, name)
            if value is not None and value not in self._catalog.attributes.options(name):
                raise PhotoAvatarError(422, "invalid_photo_avatar_request", f"{name} is not a listed option.")
            attributes[name] = value
        return display_name, prompt, attributes

    def _unavailable(self, availability: Availability) -> PhotoAvatarError:
        if availability.reason == "policy_denied":
            return PhotoAvatarError(403, "policy_denied", "Photo avatar creation is not permitted for your account.")
        if availability.reason == "disabled":
            return PhotoAvatarError(404, "photo_avatars_disabled", "Photo avatars are disabled.")
        return PhotoAvatarError(
            503, "photo_avatars_unavailable", "Photo avatar creation is unavailable.",
            reason=availability.reason,
        )

    async def cost_capped(self, owner_id: str) -> bool:
        """Whether ``owner_id``'s effective limits carry a cost cap right now.

        Creation's exact rule, public for the live relay: combined with a
        meter's price, an unknown price must refuse under a cap. It may raise
        ``PolicyError`` like any execution-time policy check.
        """
        return await self._cost_capped(owner_id)

    async def _cost_capped(self, owner: str) -> bool:
        """Whether a cost limit is enforced for ``owner`` right now.

        Soft limits (entitlements and group-policy spend) only count when
        enforcement is on; hard admission refuses an unbounded dollar meter at
        dispatch on its own. A policy binding for a different actor says nothing
        about ``owner``'s limits, so it is treated as capped (fail closed).
        """
        if not self._entitlements.enabled:
            return False
        binding = current_binding()
        if binding is not None and binding.service.enabled:
            if binding.owner_id != owner:
                return True
            limits = (await binding.resolve()).limits
        else:
            limits = await self._entitlements.get_effective(owner)
        return limits.costPerDayMicroUsd is not None or limits.costPerMonthMicroUsd is not None

    def _check_limits(self, ledger: PhotoAvatarLedger, now: datetime) -> None:
        if len(ledger.active) >= self._settings.photo_avatar_max_per_user:
            raise self._limit_error(LimitReached("max_avatars"))
        if len(ledger.recent(now)) >= self._settings.photo_avatar_max_creations_per_day:
            reopen = next_creation_at(ledger, now, self._settings.photo_avatar_max_creations_per_day)
            wait = max(1, math.ceil((reopen - now).total_seconds())) if reopen else None
            raise self._limit_error(LimitReached("daily", wait))

    def _limit_error(self, exc: LimitReached) -> PhotoAvatarError:
        if exc.kind == "max_avatars":
            return PhotoAvatarError(
                409, "avatar_limit_reached",
                f"You already have {self._settings.photo_avatar_max_per_user} avatars. Delete one to create another.",
            )
        return PhotoAvatarError(
            429, "daily_creation_limit", "The daily photo avatar creation limit is reached.",
            retry_after=exc.retry_after,
        )

    async def _ensure_project(self) -> bool:
        if self._project_ready:
            return True
        async with self._project_lock:
            if self._project_ready:
                return True
            state = await self._gateway.get_project(correlation_id=self._correlation())
            if state == "present":
                self._project_ready = True
            elif state == "absent":
                self._project_ready = await self._gateway.create_project(
                    correlation_id=self._correlation(),
                )
            return self._project_ready

    async def _apply(
        self, owner: str, record_id: str, fields: dict[str, Any],
        *, only_if: Callable[[PhotoAvatarRecord], bool] | None = None,
    ) -> PhotoAvatarRecord | None:
        """CAS-update one record; returns the stored record (or None if it vanished)."""
        for _ in range(4):
            loaded = await self._store.get(owner, record_id)
            if loaded is None:
                return None
            current, snapshot = loaded
            if only_if is not None and not only_if(current):
                return current
            updated = PhotoAvatarRecord.model_validate({
                **current.model_dump(), **fields, "updatedAt": self._clock(),
            })
            if await self._store.replace(snapshot, updated):
                return updated
        loaded = await self._store.get(owner, record_id)
        return loaded[0] if loaded else None

    @staticmethod
    def _correlation() -> str | None:
        value = get_correlation_id()
        return value if value and value != "-" else None

    def _view(self, record: PhotoAvatarRecord, usable: bool) -> PhotoAvatar:
        failure = (
            PhotoAvatarFailure(code=record.failureCode, message=FAILURE_MESSAGES[record.failureCode])  # pyright: ignore[reportArgumentType]
            if record.status == "failed" and record.failureCode in FAILURE_MESSAGES else None
        )
        preview = (
            PhotoAvatarPreview(
                url=f"/api/photo-avatars/{record.id}/preview",
                width=record.preview.width, height=record.preview.height, bytes=record.preview.bytes,
            )
            if record.status == "ready" and record.preview is not None else None
        )
        return PhotoAvatar(
            id=record.id,
            displayName=record.displayName,
            prompt=record.prompt,
            attributes=PhotoAvatarAttributes(
                **{name: record.attributes.get(name) for name in ATTRIBUTE_NAMES},
            ),
            status=record.status,
            failure=failure,
            preview=preview,
            cost=PhotoAvatarCost(
                currency=record.cost.currency,
                estimatedUsd=_micro_to_usd(record.cost.estimatedMicroUsd),
                known=record.cost.known,
                priceVersion=record.cost.priceVersion,
            ),
            usable=usable and record.status == "ready" and record.liveVerificationFailedAt is None,
            reported=record.reportedAt is not None,
            needsReverification=record.status == "ready" and record.liveVerificationFailedAt is not None,
            createdAt=record.createdAt,
            updatedAt=record.updatedAt,
            readyAt=record.readyAt,
        )

    async def close(self) -> None:
        for task in list(self._background):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except BaseException:  # noqa: BLE001 - shutdown drains best-effort
                pass
        for closer in (self._gateway.close, self._store.close, self._artifacts.close):
            try:
                await closer()
            except Exception:  # noqa: BLE001 - one failed close must not skip the others
                logger.warning("photo avatar resource close failed", exc_info=True)
