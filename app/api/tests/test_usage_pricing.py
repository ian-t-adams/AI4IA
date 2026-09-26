"""Pricing book estimation: micro-USD math, snapshots, and unknown handling."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai4ia_api.model_evidence import ModelCallRecorder
from ai4ia_api.usage.pricing import PriceRate, PricingBook, load_pricing

_CATALOG_PATH = Path(__file__).resolve().parents[3] / "infra" / "models.json"


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
    assert openai.known is False and openai.micro_usd is None
    for model in ("MAI-Image-2.5", "MAI-Image-2.6", "MAI-Image-2.6-Flash"):
        mai = book.estimate_image(model, size="1024x1024", quality="auto")
        assert mai.known is False and mai.micro_usd is None, model


def test_astra_uses_published_azure_short_context_estimate():
    estimate = load_pricing().estimate(
        "gpt-6-astra", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert estimate.known is True
    assert estimate.input_per_1m == 10.0
    assert estimate.output_per_1m == 50.0
    assert estimate.micro_usd == 60_000_000


@pytest.mark.parametrize(("model_id", "input_rate", "output_rate", "micro_usd"), [
    # Microsoft's 2026-09-22 GPT-6 Sol/Luna launch post, Global Standard short context.
    ("gpt-6-sol", 2.0, 10.0, 12_000_000),
    ("gpt-6-luna", 0.1, 0.5, 600_000),
    # Azure Retail Prices API '5.5 ShortCo inp Gl' / '5.5 ShortCo opt Gl'.
    ("gpt-5.5", 5.0, 30.0, 35_000_000),
])
def test_new_gpt_deployments_use_sourced_short_context_estimates(
    model_id, input_rate, output_rate, micro_usd,
):
    catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    naming = catalog["naming"]
    entry = next(model for model in catalog["catalog"] if model["name"] == model_id)
    book = load_pricing()
    assert len(entry["deployments"]) == 4
    for deployment in entry["deployments"]:
        name = (
            f"{model_id}-{naming['subscriptionToken']}-{deployment['region']}"
            f"-{naming['skuShort'][deployment['sku']]}"
        )
        estimate = book.estimate(
            model_id, prompt_tokens=1_000_000, completion_tokens=1_000_000, deployment=name,
        )
        assert estimate.known is True, name
        assert (estimate.input_per_1m, estimate.output_per_1m) == (input_rate, output_rate), name
        assert estimate.micro_usd == micro_usd, name


@pytest.mark.parametrize("model_id", ["gpt-6-sol", "gpt-6-luna", "gpt-5.5"])
def test_new_model_receipts_keep_known_cost_and_the_packaged_price_version(model_id):
    packaged = load_pricing()
    assert packaged.version
    for book, expected in (
        (packaged, packaged.version),
        # Control: the unchanged receipt identifier/redaction path drops a
        # token-shaped version, so the packaged assertion is not vacuous.
        (PricingBook({model_id: packaged.rate(model_id)}, currency="USD", version="a" * 40), None),
    ):
        recorder = ModelCallRecorder(model_id=model_id, deployment="synthetic-deployment", pricing=book)
        call = recorder.start("synthetic-deployment", "responses")
        call.request({"input": [{"role": "user", "content": "synthetic"}], "max_output_tokens": 16})
        call.report_usage({"prompt_tokens": 10, "completion_tokens": 5}, completed=True)
        evidence = json.loads(call.snapshot().model_dump_json())
        assert evidence["api"] == "responses"
        assert evidence["cost"]["coverage"] == "known"
        assert evidence["cost"]["priceVersion"] == expected


def test_gpt_image_25_token_meters_stay_cost_unknown_beside_a_mapped_image_meter():
    book = load_pricing()
    for model_id in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
        for quality in ("auto", "high"):
            estimate = book.estimate_image(model_id, size="1024x1024", quality=quality)
            assert estimate.known is False and estimate.micro_usd is None, (model_id, quality)
        # Their per-token retail meters are not flattened into the token book.
        assert book.rate(model_id) is None
    # Control: the same call shape is priced for an image model with a mapped meter.
    control = book.estimate_image("FLUX.2-pro", size="1024x1024", quality="auto")
    assert control.known is True and control.micro_usd == 30_729


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
    # Microsoft publishes advanced contextualization at $3.00 per 1M tokens --
    # 3x standard, not 1.5x. Pinning the ratio here keeps the two rows honest
    # relative to each other if either rate is ever re-sourced.
    assert advanced.micro_usd == 3_000_000
    assert advanced.micro_usd == context.micro_usd * 3


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
        f"{model['name']}/{deployment['sku']}"
        for model in catalog["catalog"]
        for deployment in model["deployments"]
        if model["category"] in _TOKEN_BILLED_CATEGORIES and book.rate(
            model["name"], deployment=(
                f"{model['name']}-{catalog['naming']['subscriptionToken']}-{deployment['region']}"
                f"-{catalog['naming']['skuShort'][deployment['sku']]}"
            ),
        ) is None
    )
    assert not missing, (
        "token-billed catalog models with no entry in pricing.json: "
        f"{missing}. Add per-1M USD rates from the Azure Retail Prices API "
        "(serviceName eq 'Foundry Models') or official provider documentation, for the exact SKU."
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
    rate_fields = {"avatar": "perAvatarUsd", "second": "perMinuteUsd"}
    for name, entry in raw["avatarModels"].items():
        assert entry["basis"] in rate_fields, f"{name} has an unknown avatar billing basis"
        assert set(entry) == {"basis", rate_fields[entry["basis"]]}, (
            f"{name} must carry exactly the rate its basis prices"
        )
        assert entry[rate_fields[entry["basis"]]] > 0, f"{name} has a non-positive avatar rate"


def test_the_catalog_photo_avatar_meter_is_priced_and_sourced() -> None:
    """Creating an avatar is billable; its meter must not ship unpriced.

    An unpriced meter would record every creation as cost-unknown and refuse
    creation for anyone under a cost cap, so the catalog's billing id and the
    price book move together.
    """
    catalog = json.loads(
        (Path(__file__).resolve().parents[3] / "infra" / "voice-providers.json").read_text(
            encoding="utf-8"
        )
    )["photoAvatars"]
    raw = json.loads(
        (Path(__file__).resolve().parents[1] / "src" / "ai4ia_api" / "data" / "pricing.json")
        .read_text(encoding="utf-8")
    )
    assert catalog["billingModelId"] in raw["avatarModels"]
    assert "azure.microsoft.com/pricing/details/cognitive-services/speech-services" in raw["_avatarSource"]
    estimate = load_pricing().estimate_avatar(catalog["billingModelId"])
    assert estimate.known is True
    assert estimate.micro_usd == 2_000_000
    assert estimate.billing_unit == "avatar" and estimate.billable_units == 1.0
    assert estimate.version == raw["version"]


def test_avatar_estimates_are_unknown_rather_than_free_without_a_valid_rate() -> None:
    priced = PricingBook(
        {}, currency="USD", version="v1",
        avatar_rates={"avatar": {"basis": "avatar", "perAvatarUsd": 2.0}},
    )
    assert priced.estimate_avatar("avatar", count=2).micro_usd == 4_000_000
    for book, model, count in (
        (priced, "missing", 1),
        (priced, "avatar", 0),
        (PricingBook({}, currency="USD", version="v1", avatar_rates={
            "avatar": {"basis": "image", "perAvatarUsd": 2.0},
        }), "avatar", 1),
        (PricingBook({}, currency="USD", version="v1", avatar_rates={
            "avatar": {"basis": "avatar", "perAvatarUsd": 0},
        }), "avatar", 1),
        (PricingBook({}, currency="USD", version="v1", avatar_rates={
            "avatar": {"basis": "avatar", "perAvatarUsd": "NaN"},
        }), "avatar", 1),
    ):
        estimate = book.estimate_avatar(model, count=count)
        assert estimate.known is False and estimate.micro_usd is None


def test_the_catalog_live_avatar_meter_is_priced_per_second_and_sourced() -> None:
    """Live avatar time is billed per second; its meter must not ship unpriced.

    An unpriced live meter would record every session as cost-unknown and
    refuse live avatars for anyone under a cost cap.
    """
    catalog = json.loads(
        (Path(__file__).resolve().parents[3] / "infra" / "voice-providers.json").read_text(
            encoding="utf-8"
        )
    )["photoAvatars"]
    raw = json.loads(
        (Path(__file__).resolve().parents[1] / "src" / "ai4ia_api" / "data" / "pricing.json")
        .read_text(encoding="utf-8")
    )
    live = catalog["liveBillingModelId"]
    assert live != catalog["billingModelId"]
    assert raw["avatarModels"][live] == {"basis": "second", "perMinuteUsd": 0.6}
    assert "billed per second" in raw["_avatarSource"]
    book = load_pricing()
    one_second = book.estimate_avatar_seconds(live, seconds=1)
    assert one_second.known is True and one_second.micro_usd == 10_000
    assert one_second.billing_unit == "second" and one_second.pricing_basis == "second"
    minute = book.estimate_avatar_seconds(live, seconds=61)
    assert minute.micro_usd == 610_000 and minute.billable_units == 61.0
    assert minute.version == raw["version"]
    # The live meter is not a per-avatar meter, and creation's is not per second.
    assert book.estimate_avatar(live).known is False
    assert book.estimate_avatar_seconds(catalog["billingModelId"], seconds=1).known is False


def test_live_avatar_second_estimates_are_unknown_rather_than_free_without_a_valid_rate() -> None:
    priced = PricingBook(
        {}, currency="USD", version="v1",
        avatar_rates={"live": {"basis": "second", "perMinuteUsd": 0.6}},
    )
    assert priced.estimate_avatar_seconds("live", seconds=90).micro_usd == 900_000
    for book, model, seconds in (
        (priced, "missing", 1),
        (priced, "live", 0),
        (priced, "live", -5),
        (priced, "live", 1.5),
        (priced, "live", True),
        (PricingBook({}, currency="USD", version="v1", avatar_rates={
            "live": {"basis": "avatar", "perMinuteUsd": 0.6},
        }), "live", 1),
        (PricingBook({}, currency="USD", version="v1", avatar_rates={
            "live": {"basis": "second", "perMinuteUsd": 0},
        }), "live", 1),
        (PricingBook({}, currency="USD", version="v1", avatar_rates={
            "live": {"basis": "second", "perMinuteUsd": "Infinity"},
        }), "live", 1),
        (PricingBook({}, currency="USD", version="v1", avatar_rates={
            "live": {"basis": "second", "perAvatarUsd": 0.6},
        }), "live", 1),
    ):
        estimate = book.estimate_avatar_seconds(model, seconds=seconds)  # type: ignore[arg-type]
        assert estimate.known is False and estimate.micro_usd is None
