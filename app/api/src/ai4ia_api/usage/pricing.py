"""Best-effort cost estimation from a packaged price book.

``pricing.json`` maps a catalog model id to per-1M-token USD rates. Prices are
**estimates** and may be stale — they exist to give cost a sense of scale for
tracking/demo, not to bill. A model absent from the book (or a non-token modality
like image/audio/video) yields ``None`` cost, which the ledger records as
*cost unknown* rather than zero.

Cost is computed in integer **micro-USD** to avoid float drift across a ledger:

    micro_usd = prompt * inputPer1M + completion * outputPer1M

(``inputPer1M`` is USD per 1,000,000 tokens, so ``tokens * ratePer1M`` is already
in micro-USD.)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
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


class PricingBook:
    def __init__(self, rates: dict[str, PriceRate], *, currency: str, version: str | None) -> None:
        self._rates = rates
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
    return PricingBook(rates, currency=currency, version=version)


@lru_cache
def load_pricing(explicit_path: str | None = None) -> PricingBook:
    path = Path(explicit_path) if explicit_path else _PACKAGED
    if not path.exists():
        # Missing price book is non-fatal: everything is recorded as cost-unknown.
        return PricingBook({}, currency="USD", version=None)
    return _parse(json.loads(path.read_text(encoding="utf-8")))
