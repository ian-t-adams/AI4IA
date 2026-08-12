"""Data-residency policy: what the app will actually route to, and why.

The distinction these tests defend is that ``dataZone`` describes the ENDPOINT's
geography while ``residency`` describes where PROCESSING may occur, and the two
diverge for ``GlobalStandard`` -- the SKU serving nearly every model here. A
GlobalStandard deployment in Sweden Central is reachable from the EU but may be
processed anywhere, so it cannot satisfy an EU residency requirement.

Conflating them would make the control worse than useless: it would report a
sovereignty guarantee the platform is not making.
"""
from __future__ import annotations

import pathlib

import pytest

from ai4ia_api.catalog import DeploymentOption, ModelCatalog, ModelEntry, load_catalog
from ai4ia_api.config import Settings
from tests.conftest import make_settings


def _option(sku: str, region: str, zone: str | None) -> DeploymentOption:
    return DeploymentOption(
        region=region, dataZone=zone, sku=sku, deploymentName=f"m-{region}-{sku}"
    )


def _catalog(policy: str, *options: DeploymentOption) -> ModelCatalog:
    return ModelCatalog(
        residencyPolicy=policy,
        models=[
            ModelEntry(
                id="m",
                displayName="M",
                category="chat",
                format="OpenAI",
                options=list(options),
            )
        ],
    )


# --- residency is derived from the SKU, not the region -----------------------


@pytest.mark.parametrize(
    ("sku", "zone", "expected"),
    [
        # The case that motivates all of this: an EU endpoint that is NOT EU-bound.
        ("GlobalStandard", "EU", "global"),
        ("GlobalStandard", "US", "global"),
        # Regional/zonal SKUs are bounded, so they carry their zone.
        ("Standard", "EU", "eu"),
        ("Standard", "US", "us"),
        ("DataZoneStandard", "EU", "eu"),
        # No recorded zone: report the weakest claim rather than guess.
        ("Standard", None, "global"),
    ],
)
def test_residency_comes_from_sku(sku, zone, expected):
    assert _option(sku, "swedencentral", zone).residency == expected


def test_global_deployment_never_satisfies_a_zone_requirement():
    """The core guarantee. If this inverts, the control lies."""
    eu_endpoint_global_sku = _option("GlobalStandard", "swedencentral", "EU")

    assert eu_endpoint_global_sku.satisfies("global") is True
    assert eu_endpoint_global_sku.satisfies("eu") is False
    assert eu_endpoint_global_sku.satisfies("us") is False


def test_bounded_deployment_satisfies_only_its_own_zone():
    eu = _option("DataZoneStandard", "swedencentral", "EU")

    assert eu.satisfies("eu") is True
    assert eu.satisfies("us") is False
    assert eu.satisfies("global") is True


# --- the `zonal` tier ---------------------------------------------------------


def test_zonal_accepts_any_bounded_zone_but_never_global():
    """`zonal` means "stay inside SOME data zone, I don't mind which".

    It exists because DataZoneStandard availability is uneven between regions:
    forcing a choice between `global` and one specific zone would either deny
    those models to everyone or make `us` and `eu` silently unequal with no way
    to say "regional, either one".
    """
    us = _option("DataZoneStandard", "eastus2", "US")
    eu = _option("DataZoneStandard", "swedencentral", "EU")
    regional = _option("Standard", "eastus2", "US")
    globally = _option("GlobalStandard", "swedencentral", "EU")

    assert us.satisfies("zonal") is True
    assert eu.satisfies("zonal") is True
    assert regional.satisfies("zonal") is True
    assert globally.satisfies("zonal") is False, (
        "a GlobalStandard deployment may process anywhere, so it cannot satisfy "
        "a request to stay inside a data zone"
    )


def test_zonal_is_strictly_wider_than_a_specific_zone():
    """Every model reachable under `us` or `eu` must also be reachable under
    `zonal` -- otherwise the ladder is not a ladder."""
    zonal = load_catalog(None, "zonal")
    zonal_ids = {m.id for m in zonal.models if zonal.available(m)}

    for policy in ("us", "eu"):
        catalog = load_catalog(None, policy)
        specific = {m.id for m in catalog.models if catalog.available(m)}
        assert specific <= zonal_ids, f"{policy} reaches models 'zonal' does not"


def test_zonal_is_strictly_narrower_than_global():
    zonal = load_catalog(None, "zonal")
    globally = load_catalog(None, "global")

    zonal_ids = {m.id for m in zonal.models if zonal.available(m)}
    global_ids = {m.id for m in globally.models if globally.available(m)}
    assert zonal_ids < global_ids, "zonal must exclude something global allows"


def test_zonal_routes_only_to_bounded_deployments():
    catalog = load_catalog(None, "zonal")

    for entry in catalog.models:
        for option in catalog.eligible_options(entry):
            assert option.sku != "GlobalStandard"
            assert option.residency in {"us", "eu"}


def test_asymmetric_models_are_reachable_rather_than_excluded():
    """Four models offer DataZoneStandard in eastus2 only. Excluding them to
    keep `us` and `eu` symmetric also denied them to `zonal` users for no
    benefit, so they are included and the asymmetry is made visible instead."""
    zonal = load_catalog(None, "zonal")
    reachable = {m.id for m in zonal.models if zonal.available(m)}

    for model_id in ("gpt-5.2", "gpt-5.3-codex", "gpt-5.4-nano", "grok-4-1-fast-reasoning"):
        assert model_id in reachable, f"{model_id} should be reachable under zonal"
        # ...and only via its US deployment, since that is the only zone it has.
        entry = zonal.get(model_id)
        assert entry is not None
        assert {o.residency for o in zonal.eligible_options(entry)} == {"us"}


def test_us_and_eu_are_deliberately_unequal_and_that_is_visible():
    """Not a defect: Azure offers DataZoneStandard for more models in eastus2.
    Pinned so the difference is a recorded fact rather than a surprise."""
    us = load_catalog(None, "us")
    eu = load_catalog(None, "eu")

    us_chat = {m.id for m in us.conversational_models()}
    eu_chat = {m.id for m in eu.conversational_models()}

    assert eu_chat < us_chat, "expected EU to be a strict subset of US today"
    assert us_chat - eu_chat == {
        "claude-opus-4-8",
        "gpt-5.2",
        "gpt-5.3-codex",
        "gpt-5.4-nano",
        "grok-4-1-fast-reasoning",
    }


# --- the policy filters routing ---------------------------------------------


def test_policy_excludes_global_deployments_from_a_zone_policy():
    catalog = _catalog(
        "eu",
        _option("GlobalStandard", "swedencentral", "EU"),
        _option("Standard", "swedencentral", "EU"),
    )

    chosen = catalog.resolve_deployment("m")
    assert chosen is not None
    assert chosen.sku == "Standard", "a GlobalStandard deployment served an EU-only policy"


def test_policy_can_make_a_model_unreachable_rather_than_relocate_it():
    catalog = _catalog("us", _option("GlobalStandard", "swedencentral", "EU"))

    assert catalog.resolve_deployment("m") is None
    assert catalog.available(catalog.models[0]) is False
    assert catalog.conversational_models() == []


def test_caller_constraints_cannot_widen_the_policy():
    """A request may narrow routing further, never escape the policy."""
    catalog = _catalog("eu", _option("GlobalStandard", "eastus2", "US"))

    # Explicitly asking for the US deployment must still be refused.
    assert catalog.resolve_deployment("m", region="eastus2") is None
    assert catalog.resolve_deployment("m", data_zone="US") is None


def test_global_policy_preserves_existing_behaviour():
    catalog = _catalog("global", _option("GlobalStandard", "eastus2", "US"))

    chosen = catalog.resolve_deployment("m")
    assert chosen is not None and chosen.region == "eastus2"


def test_unknown_policy_falls_back_to_permissive_rather_than_empty():
    # Settings validates first; reaching this means a caller bypassed config, and
    # silently blocking every model would look like an outage.
    catalog = load_catalog(None, "not-a-policy")
    assert catalog.residencyPolicy == "global"
    assert catalog.conversational_models()


# --- startup validation ------------------------------------------------------


def test_invalid_policy_fails_startup():
    settings = make_settings(data_residency="atlantis")
    with pytest.raises(RuntimeError, match="not a valid policy"):
        settings.validate_runtime()


@pytest.mark.parametrize("policy", ["zonal", "us", "eu"])
def test_zone_policies_are_usable_with_datazone_deployments(policy):
    """The lever has to actually work, not merely exist.

    infra/models.json carries DataZoneStandard deployments in both eastus2 (US)
    and swedencentral (EU), so a zone policy must leave a real working set: chat
    models to pick from, plus the embedding model memory and library RAG depend
    on. If this fails, the catalog lost its zone-bounded deployments.
    """
    catalog = load_catalog(None, policy)

    chat = catalog.conversational_models()
    assert chat, f"{policy} left no conversational model"
    assert {m.category for m in chat} >= {"chat", "chat-fast", "reasoning"}

    embedding = catalog.resolve_deployment("text-embedding-3-large")
    assert embedding is not None, "memory + library RAG would silently switch off"
    assert embedding.sku == "DataZoneStandard"
    # Under a specific zone the deployment carries that zone. Under `zonal` it
    # carries whichever concrete zone served -- "zonal" is a policy, never a
    # deployment's residency.
    if policy == "zonal":
        assert embedding.residency in {"us", "eu"}
    else:
        assert embedding.residency == policy


@pytest.mark.parametrize("policy", ["us", "eu"])
def test_zone_policies_never_route_to_a_global_deployment(policy):
    """The guarantee, asserted over the real catalog rather than a fixture."""
    catalog = load_catalog(None, policy)

    for entry in catalog.models:
        for option in catalog.eligible_options(entry):
            assert option.sku != "GlobalStandard", (
                f"{entry.id} would serve {policy} from a GlobalStandard deployment"
            )
            assert option.residency == policy


def test_startup_fails_when_an_enabled_feature_loses_its_model():
    """Memory and library RAG fail OPEN at runtime -- they log and switch
    themselves off. Under a residency policy that silent degradation is the
    failure mode this check exists to convert into a loud one."""
    settings = make_settings(
        data_residency="eu",
        memory_store="cosmos",
        cosmos_endpoint="https://example.documents.azure.com/",
        # A model with no zone-bounded deployment anywhere.
        memory_embedding_model="embed-v-4-0",
    )
    with pytest.raises(RuntimeError) as err:
        settings.validate_runtime()

    message = str(err.value)
    assert "enabled feature without a compliant model" in message
    assert "embed-v-4-0" in message
    assert "fail OPEN" in message, "the error must explain why silence is the danger"


def test_disabled_features_do_not_block_a_zone_policy():
    """Only features that are switched ON are checked."""
    settings = make_settings(
        data_residency="eu",
        memory_store="disabled",
        document_understanding_enabled=False,
        memory_embedding_model="embed-v-4-0",  # unreachable, but unused
    )
    settings.validate_runtime()


def test_policy_with_no_reachable_chat_model_fails_startup_with_guidance():
    """A tenant whose catalog has only GlobalStandard chat deployments.

    This was AI4IA's own situation before the DataZoneStandard deployments were
    added, and remains the situation for any fresh subscription that has not
    added them. Booting with an empty model picker would look like a broken
    deployment, so startup refuses and names the fix.
    """
    import json
    import tempfile

    catalog = {
        "models": [
            {
                "id": "only-global",
                "displayName": "Only Global",
                "category": "chat",
                "format": "OpenAI",
                "options": [
                    {
                        "region": "swedencentral",
                        "dataZone": "EU",
                        "sku": "GlobalStandard",
                        "deploymentName": "only-global-eu-glbl",
                    }
                ],
            }
        ]
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "catalog.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        settings = make_settings(data_residency="eu", model_catalog_path=str(path))
        with pytest.raises(RuntimeError) as err:
            settings.validate_runtime()

    message = str(err.value)
    assert "no conversational model" in message
    assert "DataZoneStandard" in message, "the error must say what to add"


def test_default_policy_is_permissive_and_starts():
    assert Settings().data_residency == "global"
    make_settings().validate_runtime()
