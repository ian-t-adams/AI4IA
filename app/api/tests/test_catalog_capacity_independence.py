"""Production allocation policy must not become a second runtime routing catalog."""

from __future__ import annotations

import copy
import json
import runpy
from pathlib import Path

from ai4ia_api import catalog

ROOT = Path(__file__).resolve().parents[3]


def test_configured_production_fixture_preserves_packaged_and_dev_routing(tmp_path, monkeypatch):
    fixture = runpy.run_path(str(ROOT / "scripts" / "tests" / "_production_fixture.py"))
    generator = runpy.run_path(str(ROOT / "scripts" / "gen-model-catalog.py"))
    configured = fixture["production_document"]()
    baseline = copy.deepcopy(configured)
    del baseline["productionCapacityPolicy"]
    for deployment in baseline["catalog"][0]["deployments"]:
        del deployment["production"]

    projected = generator["build_catalog"](configured)
    assert projected == generator["build_catalog"](baseline)
    assert catalog._transform_infra_models(configured) == catalog._transform_infra_models(baseline)
    path = tmp_path / "model_catalog.json"
    path.write_text(json.dumps(projected), encoding="utf-8")
    monkeypatch.setattr(catalog, "_PACKAGED", path)
    catalog.load_catalog.cache_clear()
    try:
        packaged = catalog.load_catalog()
        dev = catalog.ModelCatalog(**catalog._transform_infra_models(configured))
        assert packaged.model_dump() == dev.model_dump()
        for model in configured["catalog"]:
            for deployment in model["deployments"]:
                option = packaged.resolve_deployment(model["name"], region=deployment["region"])
                assert option is not None
                assert option.sku == deployment["sku"]
                assert option.deploymentName == (
                    f"{model['name']}-{configured['naming']['subscriptionToken']}-"
                    f"{deployment['region']}-{configured['naming']['skuShort'][deployment['sku']]}"
                )
                assert "capacity" not in option.model_dump()
                assert "production" not in option.model_dump()
    finally:
        catalog.load_catalog.cache_clear()
