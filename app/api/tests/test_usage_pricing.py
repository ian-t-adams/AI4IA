"""Pricing book estimation: micro-USD math, snapshots, and unknown handling."""
from __future__ import annotations

from ai4ia_api.usage.pricing import PriceRate, PricingBook, load_pricing


def _book() -> PricingBook:
    return PricingBook(
        {"gpt-x": PriceRate(input_per_1m=2.0, output_per_1m=8.0)},
        currency="USD",
        version="test-1",
    )


def test_estimate_known_model_micro_usd():
    est = _book().estimate("gpt-x", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert est.known is True
    # 1M * 2.0 + 1M * 8.0 = 10.0 USD = 10_000_000 micro-USD.
    assert est.micro_usd == 10_000_000
    assert est.input_per_1m == 2.0
    assert est.output_per_1m == 8.0
    assert est.version == "test-1"


def test_estimate_small_token_counts_round_to_micro():
    est = _book().estimate("gpt-x", prompt_tokens=100, completion_tokens=50)
    # 100*2.0 + 50*8.0 = 600 micro-USD.
    assert est.micro_usd == 600
    assert est.known is True


def test_estimate_unknown_model_is_not_known():
    est = _book().estimate("nope", prompt_tokens=10, completion_tokens=10)
    assert est.known is False
    assert est.micro_usd is None
    assert est.input_per_1m is None


def test_estimate_missing_tokens_is_unknown():
    est = _book().estimate("gpt-x", prompt_tokens=None, completion_tokens=5)
    assert est.known is False
    assert est.micro_usd is None
    # The rate snapshot is still surfaced even when tokens are missing.
    assert est.input_per_1m == 2.0


def test_load_pricing_missing_path_returns_empty_book():
    book = load_pricing("F:/definitely/not/here/pricing.json")
    assert book.rate("gpt-5.2") is None
    est = book.estimate("gpt-5.2", prompt_tokens=10, completion_tokens=10)
    assert est.known is False


def test_packaged_pricing_loads_and_has_token_models():
    book = load_pricing()
    # The packaged book should price at least one common chat model.
    est = book.estimate("gpt-5.2", prompt_tokens=1_000_000, completion_tokens=0)
    assert est.known is True
    assert est.micro_usd is not None and est.micro_usd > 0
