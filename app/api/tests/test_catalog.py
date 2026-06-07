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
