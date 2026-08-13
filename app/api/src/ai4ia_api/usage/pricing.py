"""Best-effort token, image, and document cost estimation.

Estimates are directional telemetry, never billing. Unsupported models or
option combinations remain explicitly cost-unknown rather than appearing free.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any

_PACKAGED = Path(__file__).resolve().parents[1] / "data" / "pricing.json"


@dataclass(frozen=True)
class PriceRate:
    input_per_1m: float
    output_per_1m: float


@dataclass(frozen=True)
class CostEstimate:
    """Result of an estimate. ``known`` is False when no price was found."""

    micro_usd: int | None
    known: bool
    input_per_1m: float | None
    output_per_1m: float | None
    currency: str
    version: str | None


@dataclass(frozen=True)
class OperationCostEstimate:
    """A non-token estimate with its billing basis preserved."""

    micro_usd: int | None
    known: bool
    pricing_basis: str | None
    billable_units: float | None
    billing_unit: str | None
    currency: str
    version: str | None


class PricingBook:
    def __init__(
        self,
        rates: dict[str, PriceRate],
        *,
        currency: str,
        version: str | None,
        image_rates: dict[str, dict[str, Any]] | None = None,
        document_rates: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._rates = rates
        self._image_rates = image_rates or {}
        self._document_rates = document_rates or {}
        self._currency = currency
        self._version = version

    @property
    def currency(self) -> str:
        return self._currency

    @property
    def version(self) -> str | None:
        return self._version

    def rate(self, model_id: str) -> PriceRate | None:
        return self._rates.get(model_id)

    def estimate(
        self, model_id: str, *, prompt_tokens: int | None, completion_tokens: int | None
    ) -> CostEstimate:
        rate = self._rates.get(model_id)
        if rate is None or prompt_tokens is None or completion_tokens is None:
            return CostEstimate(
                micro_usd=None,
                known=False,
                input_per_1m=rate.input_per_1m if rate else None,
                output_per_1m=rate.output_per_1m if rate else None,
                currency=self._currency,
                version=self._version,
            )
        micro = round(prompt_tokens * rate.input_per_1m + completion_tokens * rate.output_per_1m)
        return CostEstimate(
            micro_usd=int(micro),
            known=True,
            input_per_1m=rate.input_per_1m,
            output_per_1m=rate.output_per_1m,
            currency=self._currency,
            version=self._version,
        )

    def estimate_image(
        self,
        model_id: str,
        *,
        size: str | None,
        quality: str | None,
        count: int = 1,
    ) -> OperationCostEstimate:
        rate = self._image_rates.get(model_id)
        if rate is None or count <= 0:
            return self._unknown_operation()

        basis = str(rate.get("basis") or "")
        per_image_cost: Decimal | None = None
        billable_units = Decimal(count)
        billing_unit = "image"
        try:
            if basis == "image":
                per_image_cost = _decimal(rate["perImageUsd"])
            elif basis in {"megapixel", "megapixel_tiered"}:
                megapixels = _megapixels(size)
                if megapixels is None:
                    return self._unknown_operation()
                billable_units = megapixels * Decimal(count)
                billing_unit = "megapixel"
                if basis == "megapixel":
                    per_image_cost = megapixels * _decimal(rate["perMegapixelUsd"])
                else:
                    initial = _decimal(rate["initialMegapixelUsd"])
                    additional = _decimal(rate["additionalMegapixelUsd"])
                    per_image_cost = initial + max(Decimal(0), megapixels - Decimal(1)) * additional
            elif basis == "quality_size":
                if not size or not quality:
                    return self._unknown_operation()
                prices = rate.get("pricesUsd")
                if not isinstance(prices, dict):
                    return self._unknown_operation()
                raw_price = prices.get(f"{quality}:{size}")
                if raw_price is None:
                    return self._unknown_operation()
                per_image_cost = _decimal(raw_price)
            else:
                return self._unknown_operation()
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return self._unknown_operation()

        return OperationCostEstimate(
            micro_usd=_to_micro_usd(per_image_cost * Decimal(count)),
            known=True,
            pricing_basis=basis,
            billable_units=float(billable_units),
            billing_unit=billing_unit,
            currency=self._currency,
            version=self._version,
        )

    def estimate_pages(self, model_id: str, *, pages: int) -> OperationCostEstimate:
        rate = self._document_rates.get(model_id)
        if rate is None or pages <= 0 or rate.get("basis") != "page":
            return self._unknown_operation()
        try:
            per_page = _decimal(rate["perPageUsd"])
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return self._unknown_operation()
        return OperationCostEstimate(
            micro_usd=_to_micro_usd(per_page * Decimal(pages)),
            known=True,
            pricing_basis="page",
            billable_units=float(pages),
            billing_unit="page",
            currency=self._currency,
            version=self._version,
        )

    def _unknown_operation(self) -> OperationCostEstimate:
        return OperationCostEstimate(
            micro_usd=None,
            known=False,
            pricing_basis=None,
            billable_units=None,
            billing_unit=None,
            currency=self._currency,
            version=self._version,
        )


def _parse(raw: dict[str, Any]) -> PricingBook:
    currency = raw.get("currency", "USD")
    version = raw.get("version")
    rates: dict[str, PriceRate] = {}
    for model_id, entry in (raw.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        in_rate = entry.get("inputPer1M")
        out_rate = entry.get("outputPer1M")
        if in_rate is None and out_rate is None:
            continue
        rates[model_id] = PriceRate(
            input_per_1m=float(in_rate or 0.0),
            output_per_1m=float(out_rate or 0.0),
        )
    return PricingBook(
        rates,
        currency=currency,
        version=version,
        image_rates=_operation_rates(raw.get("imageModels")),
        document_rates=_operation_rates(raw.get("documentModels")),
    )


def _operation_rates(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    return {str(model_id): entry for model_id, entry in raw.items() if isinstance(entry, dict)}


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _megapixels(size: str | None) -> Decimal | None:
    if not size:
        return None
    dimensions = size.lower().split("x", maxsplit=1)
    if len(dimensions) != 2:
        return None
    try:
        width = Decimal(dimensions[0])
        height = Decimal(dimensions[1])
    except InvalidOperation:
        return None
    if width <= 0 or height <= 0:
        return None
    return width * height / Decimal(1_000_000)


def _to_micro_usd(cost_usd: Decimal) -> int:
    return int((cost_usd * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))


@lru_cache
def load_pricing(explicit_path: str | None = None) -> PricingBook:
    path = Path(explicit_path) if explicit_path else _PACKAGED
    if not path.exists():
        # Missing price book is non-fatal: everything is recorded as cost-unknown.
        return PricingBook({}, currency="USD", version=None)
    return _parse(json.loads(path.read_text(encoding="utf-8")))
