"""Content-free monetary receipt projections, without execution authority."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..hard_quota.models import Count, Digest


class ReceiptRunBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["usd_app_meter"]
    currency: Literal["USD"]
    budgetId: Digest
    revision: Count
    limitMicroUsd: Count
    settledMicroUsd: Count
    heldMicroUsd: Count
    unknownMicroUsd: Count
    remainingMicroUsd: Count
    blocked: bool = Field(strict=True)

    @model_validator(mode="after")
    def balanced(self):
        remaining = self.limitMicroUsd - self.settledMicroUsd - self.heldMicroUsd
        if (
            self.unknownMicroUsd > self.heldMicroUsd
            or self.remainingMicroUsd != max(0, remaining)
            or (not self.blocked and remaining < 0)
        ):
            raise ValueError("the monetary receipt does not balance")
        return self


class ReceiptApprovalSpend(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    quoteDigest: Digest
    bindingDigest: Digest
    currency: Literal["USD"]
    coverage: Literal["bounded", "unknown"]
    amountMicroUsd: Count | None
    priceVersion: str | None = Field(max_length=96)
    attemptVersion: str | None = Field(max_length=96)
    budgetId: Digest | None
    budgetRevision: Count | None
    remainingMicroUsd: Count | None

    @model_validator(mode="after")
    def explicit_unknown(self):
        if (self.coverage == "unknown") != (self.amountMicroUsd is None):
            raise ValueError("unknown spend cannot be recorded as zero")
        return self


class ReceiptWorkflowMoney(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["workflow-usd-receipt-v1"] = "workflow-usd-receipt-v1"
    budget: ReceiptRunBudget | None
    quotes: list[ReceiptApprovalSpend] = Field(max_length=4)
    quoteCount: int = Field(ge=0, le=24, strict=True)
    quotesDigest: Digest

    @model_validator(mode="after")
    def counted(self):
        if len(self.quotes) > self.quoteCount:
            raise ValueError("the monetary receipt lost its original quote count")
        return self
