"""Tenant/subscription portability of the model-deployment naming token.

The deployment-name token (`slurmfactory` today) is stamped into every model deployment name
by BOTH infra/main.bicep and the catalog tooling. For a subscription/tenant move to be 1:1, the
token must live in ONE place (infra/models.json `naming.subscriptionToken`) and flow consistently
through every consumer. These tests pin that: a different token produces matching names from the
build-time generator (scripts/gen-model-catalog.py) and the runtime dev-fallback transform
(ai4ia_api.catalog._transform_infra_models), with no residual hardcoded `slurmfactory`.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GEN = _REPO_ROOT / "scripts" / "gen-model-catalog.py"
_MODELS = _REPO_ROOT / "infra" / "models.json"


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_model_catalog", _GEN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_models(token: str) -> dict:
    return {
        "naming": {
            "subscriptionToken": token,
            "foundryToken": "aiforia",
            "pattern": "{model}-{subscriptionToken}-{region}-{skuShort}",
            "skuShort": {"GlobalStandard": "glbl", "Standard": "std"},
        },
        "regions": {"eastus2": {"dataZone": "US", "primary": True}},
        "catalog": [
            {
                "name": "gpt-x",
                "format": "OpenAI",
                "category": "chat",
                "deployments": [
                    {"region": "eastus2", "sku": "GlobalStandard", "capacity": 1, "version": "1"}
                ],
            }
        ],
    }


def test_generator_uses_token_from_naming_not_a_hardcoded_value():
    gen = _load_gen()
    out = gen.build_catalog(_synthetic_models("newtenant"))
    assert out["subscriptionToken"] == "newtenant"
    name = out["models"][0]["options"][0]["deploymentName"]
    assert name == "gpt-x-newtenant-eastus2-glbl"
    assert "slurmfactory" not in name


def test_runtime_transform_uses_token_from_naming():
    from ai4ia_api.catalog import _transform_infra_models

    out = _transform_infra_models(_synthetic_models("newtenant"))
    name = out["models"][0]["options"][0]["deploymentName"]
    assert name == "gpt-x-newtenant-eastus2-glbl"


def test_build_time_and_runtime_agree_on_deployment_names():
    # The single source of truth must yield identical names build-time and (dev) runtime, so a
    # move can never drift the routing table from the provisioned deployments.
    from ai4ia_api.catalog import _transform_infra_models

    gen = _load_gen()
    models = _synthetic_models("acme-prod")
    gen_name = gen.build_catalog(models)["models"][0]["options"][0]["deploymentName"]
    rt_name = _transform_infra_models(models)["models"][0]["options"][0]["deploymentName"]
    assert gen_name == rt_name == "gpt-x-acme-prod-eastus2-glbl"


def test_checked_in_models_json_declares_both_tokens():
    naming = json.loads(_MODELS.read_text(encoding="utf-8"))["naming"]
    assert naming["subscriptionToken"] == "slurmfactory"
    assert naming["foundryToken"] == "aiforia"


def test_required_realtime_protocol_survives_generator_and_dev_fallback():
    from ai4ia_api.catalog import ModelCatalog, _transform_infra_models

    source = _synthetic_models("tenant")
    source["catalog"][0].update(category="realtime", requiredRealtimeProtocol="ga")
    for raw in (_load_gen().build_catalog(source), _transform_infra_models(source)):
        entry = ModelCatalog.model_validate(raw).models[0]
        assert entry.requiredRealtimeProtocol == "ga"
        assert not entry.supports_realtime_protocol("preview")
        assert entry.supports_realtime_protocol("ga")

    del source["catalog"][0]["requiredRealtimeProtocol"]
    for raw in (_load_gen().build_catalog(source), _transform_infra_models(source)):
        entry = ModelCatalog.model_validate(raw).models[0]
        assert entry.supports_realtime_protocol("preview")
        assert entry.supports_realtime_protocol("ga")


def test_runtime_disable_survives_generator_and_dev_fallback():
    from ai4ia_api.catalog import ModelCatalog, _transform_infra_models

    source = _synthetic_models("tenant")
    source["catalog"][0]["runtimeEnabled"] = False
    for raw in (_load_gen().build_catalog(source), _transform_infra_models(source)):
        catalog = ModelCatalog.model_validate(raw)
        assert catalog.models[0].runtimeEnabled is False
        assert catalog.get("gpt-x") is catalog.models[0]
        assert not catalog.available(catalog.models[0])
        assert catalog.resolve_deployment("gpt-x") is None
    del source["catalog"][0]["runtimeEnabled"]
    for raw in (_load_gen().build_catalog(source), _transform_infra_models(source)):
        catalog = ModelCatalog.model_validate(raw)
        assert catalog.get("gpt-x").runtimeEnabled is True
        assert catalog.resolve_deployment("gpt-x") is not None


def test_realtime_and_external_profiles_survive_the_same_catalog_roundtrip():
    from ai4ia_api.catalog import ModelCatalog, _transform_infra_models

    source = json.loads(_MODELS.read_text(encoding="utf-8"))
    models = source["catalog"]
    assert any(m.get("deploymentTarget") == "external-claude" for m in models)
    assert any(m.get("requiredRealtimeProtocol") == "ga" for m in models)
    retained = next(m for m in models if m["name"] == "gpt-realtime-2")
    assert retained.get("runtimeEnabled", True) is True
    for enabled in (True, False):
        if not enabled:
            retained["runtimeEnabled"] = False
        for raw in (_load_gen().build_catalog(source), _transform_infra_models(source)):
            catalog = ModelCatalog.model_validate(raw)
            restored = ModelCatalog.model_validate(catalog.model_dump())
            for declared, entry in zip(models, restored.models, strict=True):
                assert entry.id == declared["name"]
                assert entry.runtimeEnabled is declared.get("runtimeEnabled", True)
                assert entry.requiredRealtimeProtocol == declared.get("requiredRealtimeProtocol")
                assert entry.deploymentTarget == declared.get("deploymentTarget", "source")
                assert entry.anthropicThinking == declared.get("anthropicThinking")
                assert entry.samplingSupported is declared.get("samplingSupported")
                if entry.deploymentTarget == "external-claude":
                    entry.require_external_profile()
                assert restored.get(entry.id) is entry
                assert bool(restored.eligible_options(entry, policy_filter=False)) is entry.runtimeEnabled
            assert restored.get(retained["name"]).runtimeEnabled is enabled


def test_runtime_disable_survives_generator_and_dev_fallback_without_routing():
    from ai4ia_api.catalog import ModelCatalog, _transform_infra_models

    gen = _load_gen()
    source = _synthetic_models("tenant")
    row = source["catalog"][0]
    for enabled in (True, False):
        if enabled:
            row.pop("runtimeEnabled", None)
        else:
            row["runtimeEnabled"] = False
        generated = gen.build_catalog(source)
        # Sparse like deploymentTarget: only the non-default state is packaged.
        assert ("runtimeEnabled" in generated["models"][0]) is (not enabled)
        for raw in (generated, _transform_infra_models(source)):
            catalog = ModelCatalog.model_validate(raw)
            entry = catalog.get("gpt-x")
            assert entry is not None and entry.runtimeEnabled is enabled
            assert catalog.available(entry) is enabled
            assert (catalog.resolve_deployment("gpt-x") is not None) is enabled
            assert [m.id for m in catalog.conversational_models()] == (["gpt-x"] if enabled else [])
            # Past calls keep their deployment metadata for receipts and usage.
            assert catalog.for_deployment("gpt-x-tenant-eastus2-glbl") is entry
            # Enabled rows serialize exactly as before, keeping their digests stable.
            assert ("runtimeEnabled" in entry.model_dump()) is (not enabled)
            restored = ModelCatalog.model_validate(catalog.model_dump())
            assert restored.models[0].runtimeEnabled is enabled


def test_runtime_enabled_is_a_strict_boolean_in_generator_runtime_and_schema():
    import jsonschema
    import pytest
    from pydantic import ValidationError

    from ai4ia_api.catalog import ModelCatalog, _transform_infra_models

    gen = _load_gen()
    schema = json.loads((_REPO_ROOT / "infra" / "models.schema.json").read_text(encoding="utf-8"))
    for value in (False, True):
        source = _synthetic_models("tenant")
        source["catalog"][0]["runtimeEnabled"] = value
        jsonschema.Draft7Validator(schema).validate(source)
        gen.build_catalog(source)
        ModelCatalog.model_validate(_transform_infra_models(source))
    for value in ("false", 0, None):
        source = _synthetic_models("tenant")
        source["catalog"][0]["runtimeEnabled"] = value
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft7Validator(schema).validate(source)
        with pytest.raises(ValueError, match="runtimeEnabled must be a Boolean"):
            gen.build_catalog(source)
        with pytest.raises(ValidationError):
            ModelCatalog.model_validate(_transform_infra_models(source))
