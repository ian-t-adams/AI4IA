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
