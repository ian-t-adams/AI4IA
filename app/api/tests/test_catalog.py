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


# --- conversational classification (model-surfacing redesign) ---


def test_conversational_categories_are_chat_targets():
    catalog = load_catalog()
    for model_id in (
        "gpt-5.4",          # chat
        "gpt-5-nano",       # chat-fast
        "o3",               # reasoning
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

