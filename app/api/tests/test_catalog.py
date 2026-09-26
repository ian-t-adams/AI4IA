import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.catalog import load_catalog
from ai4ia_api.main import create_app
from tests.conftest import make_settings

# Rows added from the 2026-09-23 subscription evidence. Versions are the exact
# GA versions observed in both primary regions.
_NEW_TEXT_VERSIONS = {"gpt-6-sol": "2026-09-22", "gpt-6-luna": "2026-09-22", "gpt-5.5": "2026-04-24"}
_NEW_IMAGE_VERSIONS = {"gpt-image-2.5-flare": "2026-09-08", "gpt-image-2.5-sunburst": "2026-09-08"}


def test_packaged_catalog_loads():
    catalog = load_catalog()
    assert catalog.models, "catalog should not be empty"
    ids = {m.id for m in catalog.models}
    assert "gpt-5.2" in ids


def test_deployment_name_matches_bicep_convention():
    catalog = load_catalog()
    entry = catalog.get("gpt-5.2")
    assert entry is not None
    eastus2 = next((o for o in entry.options if o.region == "eastus2"), None)
    assert eastus2 is not None
    # {model}-slurmfactory-{region}-{skuShort}
    assert eastus2.deploymentName == "gpt-5.2-slurmfactory-eastus2-glbl"


def test_resolve_deployment_prefers_region():
    catalog = load_catalog()
    entry = catalog.get("gpt-5.2")
    assert entry is not None
    chosen = catalog.resolve_deployment("gpt-5.2", region="swedencentral")
    assert chosen is not None and chosen.region == "swedencentral"


def test_resolve_deployment_unknown_model_returns_none():
    catalog = load_catalog()
    assert catalog.resolve_deployment("does-not-exist") is None


@pytest.mark.parametrize(
    ("retired", "retained"),
    [
        ("gpt-5.1", "gpt-5.4"),
        ("gpt-5", "gpt-5.6-sol"),
        ("gpt-5-nano", "gpt-5.4-nano"),
        ("o3", "gpt-6-astra"),
        ("gpt-5-pro", "gpt-5.4-pro"),
        ("gpt-5-codex", "gpt-5.3-codex"),
        ("MAI-Image-2.5", "MAI-Image-2.6"),
        ("MAI-Image-2.5-Pro", "MAI-Image-2.6"),
        ("MAI-Image-2.5-Flash", "MAI-Image-2.6-Flash"),
        ("FLUX.1-Kontext-pro", "FLUX.2-pro"),
        ("FLUX-1.1-pro", "FLUX.2-flex"),
        ("gpt-audio", "gpt-audio-1.5"),
    ],
)
def test_retired_models_are_unavailable_not_aliased(client, retired, retained):
    catalog = client.app.state.catalog
    assert catalog.get(retired) is None
    assert catalog.resolve_deployment(retired) is None
    assert catalog.get(retained) is not None
    assert catalog.resolve_deployment(retained) is not None

    response = client.get("/api/models")
    assert response.status_code == 200, response.text
    advertised = {model["id"] for model in response.json()["models"]}
    assert retired not in advertised
    assert retained in advertised


def test_tts_ga_version_keeps_the_existing_runtime_deployment():
    entry = load_catalog().get("gpt-4o-mini-tts")
    assert entry is not None
    assert [
        (option.modelVersion, option.region, option.sku, option.deploymentName)
        for option in entry.options
    ] == [
        (
            "2025-12-15",
            "eastus2",
            "GlobalStandard",
            "gpt-4o-mini-tts-slurmfactory-eastus2-glbl",
        )
    ]


def test_resolve_deployment_rejects_unsatisfiable_region():
    """An explicit region is a requirement, not a hint.

    This used to fall through to ``options[0]``, so a caller asking for a region
    the model is not deployed in was silently served from another one -- with
    nothing in the response, the usage record or the logs saying so. For a
    residency constraint, an error is safer than a silent relocation.
    """
    catalog = load_catalog()
    entry = catalog.get("gpt-5.2")
    assert entry is not None, "fixture model missing from the catalog"
    assert not any(o.region == "antarctica-south" for o in entry.options)

    assert catalog.resolve_deployment("gpt-5.2", region="antarctica-south") is None


def test_resolve_deployment_rejects_unsatisfiable_data_zone():
    catalog = load_catalog()
    assert catalog.resolve_deployment("gpt-5.2", data_zone="ANTARCTICA") is None


def test_resolve_deployment_requires_region_and_data_zone_together():
    """Both supplied means both required: honouring whichever happened to match
    first is the same silent relocation in a subtler form."""
    catalog = load_catalog()
    entry = catalog.get("gpt-5.2")
    assert entry is not None
    swedish = next((o for o in entry.options if o.region == "swedencentral"), None)
    assert swedish is not None, "fixture model missing its swedencentral deployment"

    # A real region paired with a data zone that region does not serve.
    mismatched = "ANTARCTICA" if swedish.dataZone != "ANTARCTICA" else "US"
    assert (
        catalog.resolve_deployment(
            "gpt-5.2", region="swedencentral", data_zone=mismatched
        )
        is None
    )
    # ...and the agreeing pair still resolves.
    if swedish.dataZone:
        chosen = catalog.resolve_deployment(
            "gpt-5.2", region="swedencentral", data_zone=swedish.dataZone
        )
        assert chosen is not None and chosen.region == "swedencentral"


def test_resolve_deployment_without_constraints_is_unchanged():
    """Guard against over-tightening: the common no-constraint call must still
    pick the model's first deployment."""
    catalog = load_catalog()
    entry = catalog.get("gpt-5.2")
    assert entry is not None
    chosen = catalog.resolve_deployment("gpt-5.2")
    assert chosen is not None and chosen == entry.options[0]


# --- conversational classification (model-surfacing redesign) ---


def test_conversational_categories_are_chat_targets():
    catalog = load_catalog()
    for model_id in (
        "gpt-5.4",          # chat
        "gpt-5.4-nano",     # chat-fast
        "gpt-5.4-pro",      # reasoning
        "MAI-Thinking-1",   # Microsoft adaptive reasoning
        "claude-opus-5",    # Anthropic Messages, explicit reduced profile
        "claude-sonnet-5",
        "DeepSeek-V3.2",    # reasoning-oss
        "model-router",     # router
        "o3-deep-research", # research
    ):
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert entry.conversational is True, model_id


def test_capability_and_voice_models_are_not_chat_targets():
    catalog = load_catalog()
    for model_id in (
        "gpt-image-1.5",            # image
        "sora-2",                   # video
        "gpt-4o-mini-tts",          # tts
        "whisper",                  # transcription
        "gpt-audio-1.5",            # audio
        "gpt-realtime",             # realtime
        "text-embedding-3-large",   # embedding
    ):
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert entry.conversational is False, model_id


def test_conversational_models_helper_excludes_capability_models():
    catalog = load_catalog()
    conv_ids = {m.id for m in catalog.conversational_models()}
    assert "gpt-5.4" in conv_ids
    assert "gpt-image-1.5" not in conv_ids
    assert "whisper" not in conv_ids


def test_shipped_catalog_keeps_sora_2_inventory_but_routes_no_video_model():
    """Sora 2 retires 2026-10-15 with no Foundry successor: runtime-disabled, not deleted."""
    catalog = load_catalog()
    entry = catalog.get("sora-2")
    assert entry is not None
    assert entry.runtimeEnabled is False
    assert [(o.region, o.sku, o.modelVersion) for o in entry.options] == [
        ("eastus2", "GlobalStandard", "2025-12-08"),
        ("swedencentral", "GlobalStandard", "2025-12-08"),
    ]
    assert not catalog.available(entry)
    assert catalog.resolve_deployment("sora-2") is None
    assert [m.id for m in catalog.models if m.category == "video" and catalog.available(m)] == []
    # The only other runtime-disabled row is gpt-realtime-2 (its own retirement).
    assert [m.id for m in catalog.models if not m.runtimeEnabled] == ["gpt-realtime-2", "sora-2"]
    assert catalog.resolve_deployment("gpt-5.4") is not None


def test_conversational_is_serialized():
    catalog = load_catalog()
    entry = catalog.get("gpt-5.4")
    assert entry is not None
    assert entry.model_dump()["conversational"] is True



# --- request-shape traits (reasoning effort / sampling support) ---
#
# These pin the CONTRACT between the gateway and the UI. The gateway strips
# temperature/top_p for reasoning models, so a catalog that reported
# supportsSampling=True for one of them would put the UI right back into the
# state this replaced: two sliders that silently do nothing.


def test_reasoning_models_do_not_advertise_sampling():
    catalog = load_catalog()
    for model_id in (
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.2",
        "o3-deep-research",
        "gpt-5.3-codex",
        "MAI-Thinking-1",
        "claude-opus-5",
        "claude-sonnet-5",
    ):
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert entry.supportsSampling is False, model_id


def test_non_reasoning_models_still_advertise_sampling():
    catalog = load_catalog()
    # model-router is the load-bearing case: it is deliberately excluded from the
    # reasoning rule because it accepts the standard parameter set and drops what
    # it cannot use when it routes onward.
    for model_id in ("Mistral-Large-3", "grok-4-1-fast-reasoning", "model-router"):
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert entry.supportsSampling is True, model_id


def test_new_gpt_models_use_responses_and_reject_minimal():
    """The load-bearing case for reading these from the catalog, not the name.

    A name-based rule put "minimal" in front of every ``gpt-5*`` model. Probing
    the live deployments showed the whole GPT-5.6 family 400s on it while
    GPT-5.4 accepts it -- two models one minor version apart, opposite answers,
    and no naming convention that predicts it.
    """
    catalog = load_catalog()
    for model_id in (
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
    ):
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert "minimal" not in entry.reasoningEffortOptions, model_id
        assert entry.reasoningEffortOptions == [
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
        ], model_id
        assert entry.api == "responses", model_id

    for model_id in ("gpt-5-mini", "gpt-5.2", "gpt-5.4"):
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert "minimal" in entry.reasoningEffortOptions, model_id


def test_astra_metadata_and_residency():
    entry = load_catalog().get("gpt-6-astra")
    assert entry is not None
    assert entry.contextWindow == 1_050_000
    assert entry.maxOutputTokens == 128_000
    assert entry.inputModalities == ["text", "image"]
    assert entry.toolCalling is True
    # 2026-09-23 read-only offering/quota evidence: eastus2 offers
    # DataZoneStandard (US zone, 0/333 used); swedencentral offers only
    # GlobalStandard, so there is deliberately no EU zone row.
    assert [(option.region, option.sku, option.residency) for option in entry.options] == [
        ("eastus2", "GlobalStandard", "global"),
        ("eastus2", "DataZoneStandard", "us"),
        ("swedencentral", "GlobalStandard", "global"),
    ]
    assert {option.modelVersion for option in entry.options} == {"2026-09-03"}


@pytest.mark.parametrize(("policy", "expected"), [
    ("global", ("eastus2", "GlobalStandard", "global")),
    ("us", ("eastus2", "DataZoneStandard", "us")),
    ("zonal", ("eastus2", "DataZoneStandard", "us")),
    ("eu", None),
])
def test_astra_zone_policies_route_only_to_the_us_data_zone_row(policy, expected):
    chosen = load_catalog(None, policy).resolve_deployment("gpt-6-astra")
    assert (None if chosen is None else (chosen.region, chosen.sku, chosen.residency)) == expected


@pytest.mark.parametrize(("policy", "residencies"), [
    ("global", {"global", "us"}),
    ("us", {"us"}),
    ("eu", None),
])
def test_models_api_advertises_the_astra_zone_row_exactly_where_policy_routes_it(policy, residencies):
    with TestClient(create_app(make_settings(data_residency=policy))) as client:
        response = client.get("/api/models")
    assert response.status_code == 200, response.text
    advertised = {model["id"]: model for model in response.json()["models"]}
    if residencies is None:
        assert "gpt-6-astra" not in advertised
    else:
        assert {option["residency"] for option in advertised["gpt-6-astra"]["options"]} == residencies


_TEXT_PROFILE = (
    "format", "category", "api", "contextWindow", "maxOutputTokens", "toolCalling",
    "inputModalities", "reasoningEffortOptions", "supportsSampling", "supportsTools", "conversational",
)


@pytest.mark.parametrize(("model_id", "version"), sorted(_NEW_TEXT_VERSIONS.items()))
def test_new_ga_text_models_carry_the_learn_verified_gpt6_profile(model_id, version):
    """Microsoft Learn documents the same 1,050,000/128,000-token, text+image,
    tool-calling Responses profile for these rows as for gpt-6-astra."""
    catalog = load_catalog()
    entry, astra = catalog.get(model_id), catalog.get("gpt-6-astra")
    assert entry is not None and astra is not None
    assert {key: getattr(entry, key) for key in _TEXT_PROFILE} == {
        key: getattr(astra, key) for key in _TEXT_PROFILE
    }
    assert (entry.api, entry.contextWindow, entry.maxOutputTokens) == ("responses", 1_050_000, 128_000)
    assert entry.inputModalities == ["text", "image"] and entry.supportsTools is True
    assert [
        (option.region, option.sku, option.residency, option.modelVersion) for option in entry.options
    ] == [
        ("eastus2", "GlobalStandard", "global", version),
        ("eastus2", "DataZoneStandard", "us", version),
        ("swedencentral", "GlobalStandard", "global", version),
        ("swedencentral", "DataZoneStandard", "eu", version),
    ]


@pytest.mark.parametrize(("model_id", "version"), sorted(_NEW_IMAGE_VERSIONS.items()))
def test_new_ga_image_models_mirror_gpt_image_2(model_id, version):
    catalog = load_catalog()
    entry, sibling = catalog.get(model_id), catalog.get("gpt-image-2")
    assert entry is not None and sibling is not None
    profile = ("format", "category", "api", "imageSizes", "imageQualities", "conversational", "supportsTools")
    assert {key: getattr(entry, key) for key in profile} == {key: getattr(sibling, key) for key in profile}
    assert (entry.category, entry.api, entry.conversational) == ("image", "chat", False)
    assert [
        (option.region, option.sku, option.residency, option.modelVersion) for option in entry.options
    ] == [
        # One eastus2 GlobalStandard deployment only: the subscription's gpt-image-2.5
        # GlobalStandard quota is a single 2-unit pool shared by every region, so a
        # second regional replica can never be provisioned (InsufficientQuota, 2/2).
        ("eastus2", "GlobalStandard", "global", version),
    ]


@pytest.mark.parametrize(("policy", "region"), [("us", "eastus2"), ("eu", "swedencentral")])
def test_zone_policies_route_new_text_rows_but_not_global_only_image_rows(policy, region):
    zoned, unrestricted = load_catalog(None, policy), load_catalog()
    for model_id in _NEW_TEXT_VERSIONS:
        chosen = zoned.resolve_deployment(model_id)
        assert chosen is not None, model_id
        assert (chosen.region, chosen.sku, chosen.residency) == (region, "DataZoneStandard", policy)
    for model_id in _NEW_IMAGE_VERSIONS:
        # Control: the same GlobalStandard-only rows route when no zone is required.
        assert unrestricted.resolve_deployment(model_id) is not None, model_id
        assert zoned.resolve_deployment(model_id) is None, model_id


@pytest.mark.parametrize("policy", ["global", "eu"])
def test_models_api_advertises_new_rows_exactly_where_policy_routes_them(policy):
    with TestClient(create_app(make_settings(data_residency=policy))) as client:
        response = client.get("/api/models")
    assert response.status_code == 200, response.text
    advertised = {model["id"]: model for model in response.json()["models"]}
    for model_id, version in _NEW_TEXT_VERSIONS.items():
        options = advertised[model_id]["options"]
        assert {option["modelVersion"] for option in options} == {version}, model_id
        assert {option["residency"] for option in options} == (
            {"global", "us", "eu"} if policy == "global" else {"eu"}
        ), model_id
        assert advertised[model_id]["supportsTools"] is True
    for model_id in _NEW_IMAGE_VERSIONS:
        assert (model_id in advertised) is (policy == "global"), model_id


def test_mai_images_use_native_api_and_single_region():
    for model_id in (
        "MAI-Image-2.6",
        "MAI-Image-2.6-Flash",
    ):
        entry = load_catalog().get(model_id)
        assert entry is not None, model_id
        assert entry.category == "image"
        assert entry.api == "mai"
        assert entry.imageSizes == ["1024x1024"]
        assert entry.imageQualities == ["auto"]
        assert [(option.region, option.sku) for option in entry.options] == [
            ("westus", "GlobalStandard"),
        ]
        assert entry.conversational is False


def test_o_series_excludes_minimal_reasoning_effort():
    # Sending "minimal" or "none" to an o-series deployment is a 400, so the
    # option list is per-model and must come from the server rather than a
    # hardcoded UI array.
    catalog = load_catalog()
    entry = catalog.get("o3-deep-research")
    assert entry is not None
    assert entry.reasoningEffortOptions == ["low", "medium", "high", "xhigh"]
    assert "minimal" not in entry.reasoningEffortOptions
    assert "none" not in entry.reasoningEffortOptions


def test_gpt54_pro_offers_only_its_probed_efforts():
    catalog = load_catalog()
    entry = catalog.get("gpt-5.4-pro")
    assert entry is not None
    assert entry.reasoningEffortOptions == ["medium", "high", "xhigh"]


def test_every_reasoning_model_has_probed_effort_values():
    """No conversational reasoning model may fall back to the heuristic floor.

    The fallback is deliberately conservative (low/medium/high), so relying on
    it silently drops "xhigh" from every model that supports it and would have
    offered gpt-5.4-pro "low", which it rejects. Adding a reasoning model means
    probing it -- this is the gate that says so.
    """
    from ai4ia_api.model_traits import is_reasoning_deployment

    catalog = load_catalog()
    missing = [
        entry.id
        for entry in catalog.conversational_models()
        if is_reasoning_deployment(entry.id) and entry.reasoningEffort is None
    ]
    assert not missing, (
        "these reasoning models have no probed reasoningEffort in "
        f"infra/models.json: {missing}"
    )


def test_catalog_effort_values_are_known_tokens():
    catalog = load_catalog()
    known = {"none", "minimal", "low", "medium", "high", "xhigh"}
    for entry in catalog.models:
        assert set(entry.reasoningEffortOptions) <= known, entry.id


def test_non_reasoning_models_offer_no_reasoning_effort():
    catalog = load_catalog()
    for model_id in (
        "Mistral-Large-3",
        "model-router",
        "DeepSeek-V3.2",
        "MAI-Thinking-1",
    ):
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert entry.reasoningEffortOptions == [], model_id


def test_mai_thinking_metadata_and_adaptive_reasoning_contract():
    entry = load_catalog().get("MAI-Thinking-1")
    assert entry is not None
    assert entry.displayName == "MAI Thinking 1"
    assert entry.format == "Microsoft"
    assert entry.category == "reasoning"
    assert entry.api == "mai"
    assert entry.contextWindow == 256_000
    assert entry.maxOutputTokens == 64_000
    assert entry.reasoningEffortOptions == []
    assert entry.supportsSampling is False
    assert {(option.region, option.sku) for option in entry.options} == {
        ("eastus2", "GlobalStandard"),
        ("swedencentral", "GlobalStandard"),
    }


def test_flux_models_are_image_only_and_provider_constrained():
    catalog = load_catalog()
    expected = {
        "FLUX.2-pro": {"1024x1024", "1024x1536", "1536x1024", "auto"},
        "FLUX.2-flex": {"1024x1024", "1024x1536", "1536x1024", "auto"},
    }
    for model_id, sizes in expected.items():
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert entry.format == "Black Forest Labs"
        assert entry.category == "image"
        assert entry.api == "bfl"
        assert entry.conversational is False
        assert set(entry.imageSizes or []) == sizes
        assert entry.imageQualities == ["auto"]
        assert entry.supportsTools is False
        assert {(option.region, option.sku) for option in entry.options} == {
            ("eastus2", "GlobalStandard"),
            ("swedencentral", "GlobalStandard"),
        }


def test_provider_capabilities_distinguish_chat_from_agent_models():
    catalog = load_catalog()
    deepseek = catalog.get("DeepSeek-V3.2")
    deep_research = catalog.get("o3-deep-research")
    mistral = catalog.get("Mistral-Large-3")
    assert deepseek is not None and deep_research is not None and mistral is not None
    assert deepseek.conversational is True
    assert deepseek.supportsTools is False
    assert deepseek.inputModalities == ["text"]
    assert deep_research.conversational is True
    assert deep_research.supportsTools is False
    assert mistral.supportsTools is True
    assert mistral.inputModalities == ["text", "image"]


def test_mistral_document_models_are_not_chat_targets():
    catalog = load_catalog()
    for model_id in ("mistral-document-ai-2512", "mistral-ocr-4-0"):
        entry = catalog.get(model_id)
        assert entry is not None
        assert entry.category == "document-ocr"
        assert entry.api == "mistral_ocr"
        assert entry.conversational is False
        assert entry.supportsTools is False
        assert entry.inputModalities == ["document", "image"]
        assert {(option.region, option.sku) for option in entry.options} == {
            ("eastus2", "GlobalStandard"),
            ("swedencentral", "GlobalStandard"),
        }


def test_catalog_values_win_over_the_heuristic():
    """The heuristic is a floor for unprobed models, never an override."""
    from ai4ia_api.catalog import ModelEntry
    from ai4ia_api.model_traits import reasoning_effort_options

    probed = ModelEntry(
        id="gpt-5.4-pro",
        displayName="x",
        category="reasoning",
        format="OpenAI",
        reasoningEffort=["medium", "high", "xhigh"],
        options=[],
    )
    assert probed.reasoningEffortOptions == ["medium", "high", "xhigh"]
    assert reasoning_effort_options("gpt-5.4-pro") == ["low", "medium", "high"]

    unprobed = ModelEntry(
        id="gpt-5.4-pro",
        displayName="x",
        category="reasoning",
        format="OpenAI",
        options=[],
    )
    assert unprobed.reasoningEffortOptions == ["low", "medium", "high"]

    # An empty list is data ("this model takes no effort value"), not absence,
    # so it must NOT fall through to the floor.
    none_taken = ModelEntry(
        id="gpt-5.4-pro",
        displayName="x",
        category="reasoning",
        format="OpenAI",
        reasoningEffort=[],
        options=[],
    )
    assert none_taken.reasoningEffortOptions == []


def test_claude_is_wired_for_chat_and_agents_through_messages():
    catalog = load_catalog()
    entry = catalog.get("claude-opus-5")
    assert entry is not None
    assert entry.displayName == "Claude Opus 5"
    assert entry.api == "anthropic"
    assert entry.conversational is True
    assert entry.contextWindow == 1_000_000
    assert entry.maxOutputTokens == 128_000
    assert entry.reasoningEffortOptions == ["low", "medium", "high"]
    assert entry.anthropicThinking == "disabled"
    assert entry.deploymentTarget == "external-claude"
    assert entry.supportsSampling is False
    # 2026-09-25: GlobalStandard quota for Opus 5 is held by a separately owned
    # deployment in the target subscription, so the dedicated account serves it
    # as US DataZoneStandard only.
    assert {(option.region, option.sku) for option in entry.options} == {
        ("eastus2", "DataZoneStandard"),
    }
    sonnet = catalog.get("claude-sonnet-5")
    assert sonnet is not None
    assert {(option.region, option.sku) for option in sonnet.options} == {
        ("eastus2", "GlobalStandard"),
        ("eastus2", "DataZoneStandard"),
    }


def test_claude_opus_5_5_uses_the_explicit_adaptive_text_only_profile():
    """Opus 5.5 rejects disabled thinking, so it ships text-only: no tool loop, no replay."""
    catalog = load_catalog()
    entry = catalog.get("claude-opus-5-5")
    assert entry is not None
    assert (entry.displayName, entry.category, entry.api, entry.format) == (
        "Claude Opus 5.5", "reasoning", "anthropic", "Anthropic",
    )
    assert (entry.deploymentTarget, entry.anthropicThinking) == ("external-claude", "adaptive")
    assert (entry.contextWindow, entry.maxOutputTokens) == (1_000_000, 128_000)
    assert entry.inputModalities == ["text"]
    assert entry.toolCalling is False and entry.supportsTools is False
    assert entry.conversational is True and entry.supportsSampling is False
    assert entry.reasoningEffortOptions == ["low", "medium", "high"]
    # Exactly the two deployments that exist in the dedicated account (2026-09-25).
    assert [
        (option.region, option.sku, option.residency, option.modelVersion, option.deploymentName)
        for option in entry.options
    ] == [
        ("eastus2", "GlobalStandard", "global", "2", "claude-opus-5-5-slurmfactory-eastus2-glbl"),
        ("eastus2", "DataZoneStandard", "us", "2", "claude-opus-5-5-slurmfactory-eastus2-dz"),
    ]
    assert load_catalog(None, "global", False).get("claude-opus-5-5") is None


def test_models_schema_requires_the_exact_adaptive_text_only_shape():
    from copy import deepcopy

    import jsonschema

    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "infra" / "models.schema.json").read_text(encoding="utf-8"))
    document = json.loads((root / "infra" / "models.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    assert not list(validator.iter_errors(document))
    name = next(m["name"] for m in document["catalog"] if m.get("anthropicThinking") == "adaptive")
    for field, value in (
        ("toolCalling", True), ("toolCalling", None), ("inputModalities", ["text", "image"]),
        ("inputModalities", None), ("samplingSupported", True), ("samplingSupported", None),
        ("reasoningEffort", ["low", "xhigh"]), ("reasoningEffort", []), ("anthropicThinking", "enabled"),
    ):
        changed = deepcopy(document)
        row = next(m for m in changed["catalog"] if m["name"] == name)
        if value is None:
            row.pop(field)
        else:
            row[field] = value
        assert list(validator.iter_errors(changed)), (field, value)


def test_claude_entitlement_gate_removes_model_from_runtime_catalog():
    disabled = load_catalog(None, "global", False)
    enabled = load_catalog(None, "global", True)

    assert disabled.get("claude-opus-5") is None
    assert "claude-opus-5" not in {
        model.id for model in disabled.conversational_models()
    }
    assert enabled.get("claude-opus-5") is not None
    assert enabled.get("claude-sonnet-5") is not None
    assert enabled.get("claude-opus-4-8") is None


def test_request_shape_traits_are_serialized():
    catalog = load_catalog()
    dumped = catalog.get("gpt-5.6-sol").model_dump()
    assert dumped["supportsSampling"] is False
    assert dumped["reasoningEffortOptions"] == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert dumped["api"] == "responses"


def test_catalog_traits_agree_with_the_gateway_normalizer():
    """The catalog must not claim a control the gateway is about to remove.

    Asserted against the gateway's real normalizer rather than a second copy of
    the rule, so the two cannot drift.
    """
    from ai4ia_api.gateway.client import ModelGatewayClient, _normalize_params_for_deployment
    from tests.conftest import make_settings

    catalog = load_catalog()
    checked = 0
    for entry in catalog.conversational_models():
        deployment = entry.options[0].deploymentName
        kept = {"temperature": 0.5, "top_p": 0.9}
        if entry.api == "anthropic":
            # Anthropic does not use the OpenAI normalizer; exercise the actual
            # request adapter rather than a proxy for its sampling behavior.
            client = ModelGatewayClient(make_settings(claude_enabled=True, claude_external_enabled=True))
            kept = client.build_anthropic_request(
                deployment=deployment, messages=[{"role": "user", "content": "synthetic"}], params=kept,
            ).json
        else:
            _normalize_params_for_deployment(kept, deployment)
        survived = "temperature" in kept and "top_p" in kept
        assert survived is entry.supportsSampling, entry.id
        checked += 1
    assert checked >= 15, f"expected the full conversational set, saw {checked}"


def test_reasoning_effort_survives_the_gateway_normalizer():
    """A model that advertises the control must actually be able to use it."""
    from ai4ia_api.gateway.client import _normalize_params_for_deployment

    catalog = load_catalog()
    checked = 0
    for entry in catalog.conversational_models():
        if not entry.reasoningEffortOptions:
            continue
        deployment = entry.options[0].deploymentName
        effort = entry.reasoningEffortOptions[0]
        kept = {"reasoning_effort": effort}
        _normalize_params_for_deployment(kept, deployment)
        assert kept.get("reasoning_effort") == effort, entry.id
        checked += 1
    assert checked >= 10, f"expected the reasoning models, saw {checked}"

def test_every_builtin_mistral_analyzer_resolves_in_the_model_catalog():
    """A builtin analyzer names a catalog model by id, so the two can drift.

    ``infra/models.json`` is the source of truth for what is deployed, but the
    Mistral analyzers hardcode their ``modelId``. Removing or renaming a
    ``document-ocr`` entry there would leave the analyzer advertised in the UI
    and failing only at upload time, per user, with a provider error. This
    binds them: drop the catalog model and the build fails instead.
    """
    from ai4ia_api.library.models import BUILTIN_ANALYZERS, AnalyzerProvider

    catalog = load_catalog()
    mistral = [
        analyzer
        for analyzer in BUILTIN_ANALYZERS
        if analyzer.provider is AnalyzerProvider.mistral
    ]
    # Control: the loop below is meaningless if it iterates nothing.
    assert mistral, "expected at least one builtin Mistral analyzer"
    for analyzer in mistral:
        assert catalog.resolve_deployment(analyzer.modelId) is not None, (
            f"builtin analyzer {analyzer.id!r} names model {analyzer.modelId!r}, "
            "which has no deployment in the model catalog"
        )