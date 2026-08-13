"""Pricing book estimation: micro-USD math, snapshots, and unknown handling."""
from __future__ import annotations

import json
from pathlib import Path

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


def test_packaged_flux_image_rates_preserve_each_meter_basis():
    book = load_pricing()

    fixed = book.estimate_image(
        "FLUX-1.1-pro", size="1024x1024", quality="auto"
    )
    tiered = book.estimate_image(
        "FLUX.2-pro", size="1024x1024", quality="auto"
    )
    megapixel = book.estimate_image(
        "FLUX.2-flex", size="1024x1440", quality="auto"
    )

    assert fixed.known and fixed.micro_usd == 40_000
    assert fixed.pricing_basis == "image"
    assert tiered.known and tiered.micro_usd == 30_729
    assert tiered.pricing_basis == "megapixel_tiered"
    assert megapixel.known and megapixel.micro_usd == 73_728
    assert megapixel.billable_units == 1.47456


def test_openai_and_mai_image_models_remain_unknown_without_azure_meter():
    book = load_pricing()

    openai = book.estimate_image(
        "gpt-image-1.5", size="1024x1536", quality="high"
    )
    mai = book.estimate_image(
        "MAI-Image-2.5", size="1024x1024", quality="auto"
    )

    assert openai.known is False and openai.micro_usd is None
    assert mai.known is False and mai.micro_usd is None


def test_quality_size_basis_is_supported_without_inventing_packaged_rates():
    book = PricingBook(
        {},
        currency="USD",
        version="test",
        image_rates={
            "future-image": {
                "basis": "quality_size",
                "pricesUsd": {"high:1024x1024": 0.125},
            }
        },
    )

    estimate = book.estimate_image(
        "future-image", size="1024x1024", quality="high"
    )

    assert estimate.known is True
    assert estimate.micro_usd == 125_000


def test_mistral_document_rates_are_page_based():
    book = load_pricing()

    document_ai = book.estimate_pages("mistral-document-ai-2512", pages=7)
    ocr = book.estimate_pages("mistral-ocr-4-0", pages=7)

    assert document_ai.known and document_ai.micro_usd == 21_000
    assert document_ai.billing_unit == "page"
    assert ocr.known and ocr.micro_usd == 28_000


def test_content_understanding_page_and_contextualization_rates():
    book = load_pricing()

    minimal = book.estimate_pages(
        "content-understanding-document-minimal", pages=3
    )
    basic = book.estimate_pages(
        "content-understanding-document-basic", pages=3
    )
    standard = book.estimate_pages(
        "content-understanding-document-standard", pages=3
    )
    context = book.estimate(
        "content-understanding-contextualization-standard",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    advanced = book.estimate(
        "content-understanding-contextualization-advanced",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )

    assert minimal.micro_usd == 30
    assert basic.micro_usd == 3_000
    assert standard.micro_usd == 15_000
    assert context.micro_usd == 1_000_000
    assert advanced.micro_usd == 1_500_000


# Categories billed per token. Other modalities use their own explicit price-book
# sections where an unambiguous meter exists and remain cost-unknown otherwise.
_TOKEN_BILLED_CATEGORIES = frozenset(
    {"chat", "chat-fast", "reasoning", "reasoning-oss", "research", "router", "embedding"}
)


def test_every_token_billed_catalog_model_has_a_price() -> None:
    """A token-billed model must never ship unpriced.

    Without this gate a catalog addition silently books every call at *cost
    unknown*, which reads as zero spend on the usage dashboard. That is exactly
    how the gpt-5.6 family shipped with no rates. Adding a model to
    infra/models.json is therefore a two-file change: the catalog and this book.
    """
    catalog = json.loads(
        (Path(__file__).resolve().parents[3] / "infra" / "models.json").read_text(
            encoding="utf-8"
        )
    )
    book = load_pricing()
    missing = sorted(
        model["name"]
        for model in catalog["catalog"]
        if model["category"] in _TOKEN_BILLED_CATEGORIES and book.rate(model["name"]) is None
    )
    assert not missing, (
        "token-billed catalog models with no entry in pricing.json: "
        f"{missing}. Add per-1M USD rates from the Azure Retail Prices API "
        "(serviceName eq 'Foundry Models'), using the GlobalStandard meter."
    )


def test_packaged_pricing_rates_are_positive_and_sane() -> None:
    """Guard against a zero/negative rate silently zeroing out cost."""
    raw = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src"
            / "ai4ia_api"
            / "data"
            / "pricing.json"
        ).read_text(encoding="utf-8")
    )
    for name, entry in raw["models"].items():
        assert entry["inputPer1M"] > 0, f"{name} has a non-positive input rate"
        # Embeddings legitimately have no output charge; everything else must.
        assert entry["outputPer1M"] >= 0, f"{name} has a negative output rate"
        assert entry["outputPer1M"] >= entry["inputPer1M"] or entry["outputPer1M"] == 0.0, (
            f"{name} prices output below input, which no Foundry meter does — "
            "likely a transposed or stale rate"
        )
    for name, entry in raw["imageModels"].items():
        numeric_rates = [
            value
            for key, value in entry.items()
            if key.endswith("Usd") and isinstance(value, (int, float))
        ]
        if isinstance(entry.get("pricesUsd"), dict):
            numeric_rates.extend(entry["pricesUsd"].values())
        assert numeric_rates and all(rate > 0 for rate in numeric_rates), (
            f"{name} has a missing or non-positive image rate"
        )
    for name, entry in raw["documentModels"].items():
        assert entry["perPageUsd"] > 0, (
            f"{name} has a non-positive per-page rate"
        )
