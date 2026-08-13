from ai4ia_api.catalog import load_catalog


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
        "gpt-5-nano",       # chat-fast
        "o3",               # reasoning
        "MAI-Thinking-1",   # Microsoft adaptive reasoning
        "claude-opus-4-8",  # Anthropic Messages reasoning
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
        "gpt-audio",                # audio
        "gpt-realtime",             # realtime
        "text-embedding-3-large",   # embedding
        "Cohere-rerank-v4.0-pro",   # rerank
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
        "gpt-5.6-sol",
        "gpt-5.4",
        "gpt-5",
        "o3",
        "gpt-5-codex",
        "MAI-Thinking-1",
        "claude-opus-4-8",
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


def test_gpt56_rejects_minimal_but_earlier_gpt5_accepts_it():
    """The load-bearing case for reading these from the catalog, not the name.

    A name-based rule put "minimal" in front of every ``gpt-5*`` model. Probing
    the live deployments showed the whole GPT-5.6 family 400s on it while
    GPT-5.4 accepts it -- two models one minor version apart, opposite answers,
    and no naming convention that predicts it.
    """
    catalog = load_catalog()
    for model_id in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
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

    for model_id in ("gpt-5", "gpt-5.1", "gpt-5.2", "gpt-5.4"):
        entry = catalog.get(model_id)
        assert entry is not None, model_id
        assert "minimal" in entry.reasoningEffortOptions, model_id


def test_o_series_excludes_minimal_reasoning_effort():
    # Sending "minimal" or "none" to an o-series deployment is a 400, so the
    # option list is per-model and must come from the server rather than a
    # hardcoded UI array.
    catalog = load_catalog()
    entry = catalog.get("o3")
    assert entry is not None
    assert entry.reasoningEffortOptions == ["low", "medium", "high", "xhigh"]
    assert "minimal" not in entry.reasoningEffortOptions
    assert "none" not in entry.reasoningEffortOptions


def test_gpt5_pro_offers_only_high():
    """The narrowest model in the catalog, and the one a heuristic gets worst.

    gpt-5-pro rejects every reasoning_effort except "high". A family-wide rule
    would have offered it four values, three of which are a 400.
    """
    catalog = load_catalog()
    entry = catalog.get("gpt-5-pro")
    assert entry is not None
    assert entry.reasoningEffortOptions == ["high"]


def test_every_reasoning_model_has_probed_effort_values():
    """No conversational reasoning model may fall back to the heuristic floor.

    The fallback is deliberately conservative (low/medium/high), so relying on
    it silently drops "xhigh" from every model that supports it and would have
    offered gpt-5-pro two values it rejects. Adding a reasoning model means
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
        "claude-opus-4-8",
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
        "FLUX.1-Kontext-pro": {"1024x1024", "auto"},
        "FLUX-1.1-pro": {"1024x1024", "1024x1440", "1440x1024", "auto"},
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
    mistral = catalog.get("Mistral-Large-3")
    assert deepseek is not None and mistral is not None
    assert deepseek.conversational is True
    assert deepseek.supportsTools is False
    assert deepseek.inputModalities == ["text"]
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
        id="gpt-5-pro",
        displayName="x",
        category="reasoning",
        format="OpenAI",
        reasoningEffort=["high"],
        options=[],
    )
    assert probed.reasoningEffortOptions == ["high"]
    assert reasoning_effort_options("gpt-5-pro") == ["low", "medium", "high"]

    unprobed = ModelEntry(
        id="gpt-5-pro",
        displayName="x",
        category="reasoning",
        format="OpenAI",
        options=[],
    )
    assert unprobed.reasoningEffortOptions == ["low", "medium", "high"]

    # An empty list is data ("this model takes no effort value"), not absence,
    # so it must NOT fall through to the floor.
    none_taken = ModelEntry(
        id="gpt-5-pro",
        displayName="x",
        category="reasoning",
        format="OpenAI",
        reasoningEffort=[],
        options=[],
    )
    assert none_taken.reasoningEffortOptions == []


def test_claude_is_wired_for_chat_and_agents_through_messages():
    catalog = load_catalog()
    entry = catalog.get("claude-opus-4-8")
    assert entry is not None
    assert entry.displayName == "Claude Opus 4.8"
    assert entry.api == "anthropic"
    assert entry.conversational is True
    assert entry.contextWindow == 1_000_000
    assert entry.maxOutputTokens == 128_000
    assert entry.reasoningEffortOptions == []
    assert entry.supportsSampling is False
    assert {(option.region, option.sku) for option in entry.options} == {
        ("eastus2", "GlobalStandard"),
        ("eastus2", "DataZoneStandard"),
    }


def test_claude_entitlement_gate_removes_model_from_runtime_catalog():
    disabled = load_catalog(None, "global", False)
    enabled = load_catalog(None, "global", True)

    assert disabled.get("claude-opus-4-8") is None
    assert "claude-opus-4-8" not in {
        model.id for model in disabled.conversational_models()
    }
    assert enabled.get("claude-opus-4-8") is not None


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


def test_catalog_traits_agree_with_the_gateway_normalizer():
    """The catalog must not claim a control the gateway is about to remove.

    Asserted against the gateway's real normalizer rather than a second copy of
    the rule, so the two cannot drift.
    """
    from ai4ia_api.gateway.client import _normalize_params_for_deployment

    catalog = load_catalog()
    checked = 0
    for entry in catalog.conversational_models():
        deployment = entry.options[0].deploymentName
        kept = {"temperature": 0.5, "top_p": 0.9}
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
