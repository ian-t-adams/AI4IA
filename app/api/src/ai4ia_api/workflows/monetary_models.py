"""Per-run USD application-meter state; never an owner balance or Azure bill cap."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator,
)

from ..hard_quota.models import MAX_QUANTITY, Bounds
from .automation_common import AutomationModel, MAX_RUN_DISPATCHES, SHA256_PATTERN, digest, utc

MicroUsd = Annotated[int, Field(strict=True, ge=0, le=MAX_QUANTITY)]
MoneyDigest = Annotated[str, Field(pattern=SHA256_PATTERN)]
MoneyRevision = Annotated[int, Field(strict=True, ge=0, le=MAX_QUANTITY)]


class MonetaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MonetaryCompatibleModel(AutomationModel):
    legacy_optional_fields: ClassVar[tuple[str, ...]] = ()

    @model_serializer(mode="wrap")
    def legacy_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        body = handler(self)
        # Previously accepted v3 records have no monetary contract. Preserve
        # their exact shape rather than manufacturing a zero-valued account.
        if self.legacy_optional_fields and getattr(self, self.legacy_optional_fields[0]) is None:
            for name in self.legacy_optional_fields:
                body.pop(name, None)
        return body


class RunMoney(MonetaryModel):
    version: Literal["workflow-usd-v1"] = "workflow-usd-v1"
    budgetId: MoneyDigest
    currency: Literal["USD"] = "USD"
    limitMicroUsd: MicroUsd
    revision: MoneyRevision
    settledMicroUsd: MicroUsd
    heldMicroUsd: MicroUsd
    unknownMicroUsd: MicroUsd
    compactedMicroUsd: MicroUsd
    compactedReservations: int = Field(strict=True, ge=0, le=MAX_RUN_DISPATCHES)
    reservations: int = Field(strict=True, ge=0, le=MAX_RUN_DISPATCHES)
    blocked: bool = Field(strict=True)
    reason: Literal["bound_exceeded", "accounting_overflow"] | None

    @model_validator(mode="after")
    def coherent_totals(self) -> RunMoney:
        if (
            self.unknownMicroUsd > self.heldMicroUsd
            or self.compactedMicroUsd > self.settledMicroUsd
            or self.compactedReservations > self.reservations
            or self.blocked != (self.reason is not None)
            or (
                not self.blocked
                and self.settledMicroUsd + self.heldMicroUsd > self.limitMicroUsd
            )
        ):
            raise ValueError("inconsistent per-run monetary accounting")
        return self

    @property
    def remaining_micro_usd(self) -> int:
        return max(0, self.limitMicroUsd - self.settledMicroUsd - self.heldMicroUsd)

    @classmethod
    def new(cls, budget_id: str, limit: int) -> RunMoney:
        return cls(
            budgetId=budget_id, limitMicroUsd=limit, revision=0,
            settledMicroUsd=0, heldMicroUsd=0, unknownMicroUsd=0, compactedMicroUsd=0,
            compactedReservations=0, reservations=0, blocked=False, reason=None,
        )


class DispatchMoney(MonetaryModel):
    version: Literal["workflow-usd-v1"] = "workflow-usd-v1"
    budgetId: MoneyDigest
    budgetRevision: MoneyRevision
    bounds: Bounds
    phase: Literal["held", "settled", "unknown"]
    chargedMicroUsd: MicroUsd
    settlementDigest: MoneyDigest | None
    approvalSpendDigest: MoneyDigest | None

    @model_validator(mode="after")
    def complete_contract(self) -> DispatchMoney:
        if (
            self.bounds.amounts.microUsd is None
            or self.bounds.amounts.tokens is None
            or self.bounds.amounts.requests != 1 or self.bounds.amounts.compute != 0
            or self.bounds.maxAttempts != 1
            or self.bounds.basis not in {"catalog-text-v1", "catalog-embedding-v1"}
            or (self.phase == "held") != (self.settlementDigest is None)
            or (
                self.phase in {"held", "unknown"}
                and self.chargedMicroUsd != self.bounds.amounts.microUsd
            )
        ):
            raise ValueError("incomplete monetary dispatch contract")
        return self


class BudgetView(MonetaryModel):
    mode: Literal["no_hard_dollar_cap", "usd_app_meter"]
    currency: Literal["USD"] = "USD"
    budgetId: MoneyDigest | None
    revision: MoneyRevision | None
    limitMicroUsd: MicroUsd | None
    settledMicroUsd: MicroUsd | None
    heldMicroUsd: MicroUsd | None
    unknownMicroUsd: MicroUsd | None
    remainingMicroUsd: MicroUsd | None
    blocked: bool = Field(strict=True)

    @model_validator(mode="after")
    def complete_budget(self) -> BudgetView:
        values = (
            self.budgetId, self.revision, self.limitMicroUsd, self.settledMicroUsd,
            self.heldMicroUsd, self.unknownMicroUsd, self.remainingMicroUsd,
        )
        if self.mode == "no_hard_dollar_cap":
            if any(value is not None for value in values) or self.blocked:
                raise ValueError("an uncapped run has no monetary balance")
        elif any(value is None for value in values):
            raise ValueError("the run budget snapshot is incomplete")
        else:
            assert self.limitMicroUsd is not None and self.settledMicroUsd is not None
            assert self.heldMicroUsd is not None and self.unknownMicroUsd is not None
            if (
                self.unknownMicroUsd > self.heldMicroUsd
                or self.remainingMicroUsd != max(
                    0, self.limitMicroUsd - self.settledMicroUsd - self.heldMicroUsd,
                )
            ):
                raise ValueError("the run budget snapshot does not balance")
        return self

    @classmethod
    def from_account(cls, account: RunMoney | None) -> BudgetView:
        return cls(
            mode="usd_app_meter" if account else "no_hard_dollar_cap",
            budgetId=account.budgetId if account else None,
            revision=account.revision if account else None,
            limitMicroUsd=account.limitMicroUsd if account else None,
            settledMicroUsd=account.settledMicroUsd if account else None,
            heldMicroUsd=account.heldMicroUsd if account else None,
            unknownMicroUsd=account.unknownMicroUsd if account else None,
            remainingMicroUsd=account.remaining_micro_usd if account else None,
            blocked=account.blocked if account else False,
        )


class OperationSpend(MonetaryModel):
    coverage: Literal["bounded", "unknown"]
    amountMicroUsd: MicroUsd | None
    currency: Literal["USD"] = "USD"
    basis: Literal[
        "local-no-metered-effects-v1", "catalog-text-v1", "catalog-embedding-v1",
        "unbounded-operation", "legacy-unquoted",
    ]
    reason: Literal[
        "repository-local-handler", "catalog-token-envelope", "meter-coverage-unknown",
        "attempt-contract-unavailable", "price-unavailable", "legacy-unquoted",
    ]
    bounds: Bounds | None
    localContractDigest: MoneyDigest | None

    @model_validator(mode="after")
    def proven_amount(self) -> OperationSpend:
        if self.coverage == "unknown":
            if (
                self.amountMicroUsd is not None or self.bounds is not None
                or self.localContractDigest is not None
                or self.basis not in {"unbounded-operation", "legacy-unquoted"}
                or self.reason not in {
                    "meter-coverage-unknown", "attempt-contract-unavailable",
                    "price-unavailable", "legacy-unquoted",
                }
            ):
                raise ValueError("unknown monetary impact cannot appear free")
        elif self.basis == "local-no-metered-effects-v1":
            if (
                self.amountMicroUsd != 0 or self.bounds is not None
                or self.localContractDigest is None or self.reason != "repository-local-handler"
            ):
                raise ValueError("zero spend requires an exact local-only operation")
        elif (
            self.bounds is None or self.bounds.maxAttempts != 1
            or self.amountMicroUsd is None or self.amountMicroUsd != self.bounds.amounts.microUsd
            or self.basis != self.bounds.basis or self.localContractDigest is not None
            or self.reason != "catalog-token-envelope"
        ):
            raise ValueError("a quantified impact requires a versioned monetary bound")
        return self


class ApprovalSpend(MonetaryModel):
    version: Literal["workflow-approval-spend-v1"] = "workflow-approval-spend-v1"
    quoteDigest: MoneyDigest
    bindingDigest: MoneyDigest
    quotedAt: datetime
    expiresAt: datetime
    impact: OperationSpend
    budget: BudgetView

    @model_validator(mode="after")
    def immutable_quote(self) -> ApprovalSpend:
        if utc(self.expiresAt) <= utc(self.quotedAt):
            raise ValueError("the monetary quote lifetime is invalid")
        if self.quoteDigest != self.content_digest():
            raise ValueError("the immutable monetary quote does not match its content")
        return self

    def content_digest(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"quoteDigest"}))

    @classmethod
    def create(
        cls, *, binding: str, now: datetime, expires: datetime,
        impact: OperationSpend, budget: BudgetView,
    ) -> ApprovalSpend:
        body = {
            "version": "workflow-approval-spend-v1", "bindingDigest": binding,
            "quotedAt": utc(now).isoformat().replace("+00:00", "Z"),
            "expiresAt": utc(expires).isoformat().replace("+00:00", "Z"),
            "impact": impact.model_dump(mode="json"), "budget": budget.model_dump(mode="json"),
        }
        return cls.model_validate({**body, "quoteDigest": digest(body)})


class ApprovalSpendView(MonetaryModel):
    scope: Literal["exact_tool_operation"] = "exact_tool_operation"
    status: Literal["quoted", "legacy_unquoted"]
    impact: OperationSpend
    quote: ApprovalSpend | None

    @model_validator(mode="after")
    def quoted_content(self) -> ApprovalSpendView:
        if self.status == "quoted":
            if self.quote is None or self.impact != self.quote.impact:
                raise ValueError("the approval spend view changed its immutable quote")
        elif self.quote is not None or self.impact.basis != "legacy-unquoted":
            raise ValueError("an older approval cannot acquire invented spend evidence")
        return self

    @classmethod
    def from_quote(cls, quote: ApprovalSpend | None) -> ApprovalSpendView:
        return cls(
            status="quoted" if quote else "legacy_unquoted",
            impact=quote.impact if quote else OperationSpend(
                coverage="unknown", amountMicroUsd=None, basis="legacy-unquoted",
                reason="legacy-unquoted", bounds=None, localContractDigest=None,
            ),
            quote=quote,
        )


def budget_identity(owner: str, owner_epoch: str, run_id: str, fingerprint: str, limit: int) -> str:
    return digest(["workflow-usd-v1", owner, owner_epoch, run_id, fingerprint, limit])
