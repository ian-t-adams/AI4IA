"""``imageEditing`` is a strict, catalog-owned capability of Azure OpenAI image rows.

Every seam that offers image editing reads this flag through the packaged catalog.
These tests pin the checked-in rows Learn documents as edit-capable, prove the flag
survives the generator and the dev fallback without disturbing any other row, and
show each strictness rule refusing a defect that the same row accepts when correct.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from ai4ia_api.catalog import ModelCatalog, _transform_infra_models, load_catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "scripts"
_MODELS = _REPO_ROOT / "infra" / "models.json"
_SCHEMA = _REPO_ROOT / "infra" / "models.schema.json"
EDIT_CAPABLE = {
    "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-2",
    "gpt-image-2.5-flare", "gpt-image-2.5-sunburst",
}


def _load(name: str, filename: str):
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(**image_fields) -> dict:
    return {
        "naming": {
            "subscriptionToken": "tenant",
            "foundryToken": "aiforia",
            "pattern": "{model}-{subscriptionToken}-{region}-{skuShort}",
            "skuShort": {"GlobalStandard": "glbl"},
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
            },
            {
                "name": "gpt-image-x",
                "format": "OpenAI",
                "category": "image",
                **image_fields,
                "deployments": [
                    {"region": "eastus2", "sku": "GlobalStandard", "capacity": 1, "version": "1"}
                ],
            },
        ],
    }


def test_checked_in_catalog_marks_exactly_the_documented_editing_models():
    source = json.loads(_MODELS.read_text(encoding="utf-8"))
    editing = {m["name"] for m in source["catalog"] if m.get("imageEditing") is True}
    assert editing == EDIT_CAPABLE
    assert [m["name"] for m in source["catalog"] if m.get("imageEditingDefault")] == [
        "gpt-image-2.5-sunburst"
    ]
    # gpt-image-1 is Preview with a 2026-10-23 inference retirement: never added.
    assert "gpt-image-1" not in {m["name"] for m in source["catalog"]}
    # Other image providers use different surfaces and never declare the flag.
    for model in source["catalog"]:
        if model["category"] == "image" and model["name"] not in EDIT_CAPABLE:
            assert "imageEditing" not in model, model["name"]

    packaged = load_catalog()
    assert {m.id for m in packaged.models if m.imageEditing} == EDIT_CAPABLE
    assert [m.id for m in packaged.models if m.imageEditingDefault] == ["gpt-image-2.5-sunburst"]


def test_flags_are_sparse_and_survive_generator_and_dev_fallback():
    gen = _load("gen_model_catalog_editing", "gen-model-catalog.py")
    for declared in (False, True):
        fields = {"imageEditing": True, "imageEditingDefault": True} if declared else {}
        source = _source(**fields)
        generated = gen.build_catalog(source)
        chat, image = generated["models"]
        # Only the declaring row records the flags; every other row is unchanged.
        assert "imageEditing" not in chat and "imageEditingDefault" not in chat
        assert ("imageEditing" in image) is declared
        assert ("imageEditingDefault" in image) is declared
        for raw in (generated, _transform_infra_models(source)):
            catalog = ModelCatalog.model_validate(raw)
            entry = catalog.get("gpt-image-x")
            assert entry is not None
            assert entry.imageEditing is declared
            assert entry.imageEditingDefault is declared
            dumped = entry.model_dump()
            assert ("imageEditing" in dumped) is declared
            assert "imageEditing" not in catalog.get("gpt-x").model_dump()
            restored = ModelCatalog.model_validate(catalog.model_dump())
            assert restored.get("gpt-image-x").imageEditing is declared


@pytest.mark.parametrize("field", ["imageEditing", "imageEditingDefault"])
def test_flags_are_strict_booleans_in_schema_generator_and_runtime(field):
    gen = _load("gen_model_catalog_strict", "gen-model-catalog.py")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    valid = _source(imageEditing=True, imageEditingDefault=True)
    jsonschema.Draft7Validator(schema).validate(valid)
    gen.build_catalog(valid)
    ModelCatalog.model_validate(_transform_infra_models(valid))
    for value in ("true", 1, None):
        broken = _source(imageEditing=True, imageEditingDefault=True)
        broken["catalog"][1][field] = value
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft7Validator(schema).validate(broken)
        with pytest.raises(ValueError, match=f"{field} must be a Boolean"):
            gen.build_catalog(broken)
        with pytest.raises(ValidationError):
            ModelCatalog.model_validate(_transform_infra_models(broken))


@pytest.mark.parametrize(
    "defect",
    [
        {"category": "chat"},
        {"api": "mai", "format": "Microsoft"},
        {"api": "bfl", "format": "Black Forest Labs"},
        {"format": "Microsoft"},
    ],
    ids=["chat-row", "mai-image", "bfl-image", "non-openai-format"],
)
def test_editing_is_limited_to_azure_openai_image_rows(defect):
    gen = _load("gen_model_catalog_scope", "gen-model-catalog.py")
    control = _source(imageEditing=True)
    assert gen.build_catalog(control)["models"][1]["imageEditing"] is True
    broken = copy.deepcopy(control)
    broken["catalog"][1].update(defect)
    with pytest.raises(ValueError, match="imageEditing requires an Azure OpenAI image row"):
        gen.build_catalog(broken)
    with pytest.raises(ValidationError, match="imageEditing requires"):
        ModelCatalog.model_validate(_transform_infra_models(broken))


def test_default_requires_editing():
    gen = _load("gen_model_catalog_default", "gen-model-catalog.py")
    gen.build_catalog(_source(imageEditing=True, imageEditingDefault=True))
    broken = _source(imageEditingDefault=True)
    with pytest.raises(ValueError, match="imageEditingDefault requires imageEditing"):
        gen.build_catalog(broken)
    with pytest.raises(ValidationError, match="imageEditingDefault requires imageEditing"):
        ModelCatalog.model_validate(_transform_infra_models(broken))


def test_validate_catalog_allows_one_default_and_refuses_two(tmp_path, monkeypatch, capsys):
    validator = _load("validate_catalog_editing", "validate-catalog.py")
    source = json.loads(_MODELS.read_text(encoding="utf-8"))
    path = tmp_path / "models.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    monkeypatch.setattr(validator, "MODELS", path)
    assert validator.main() == 0
    flare = next(m for m in source["catalog"] if m["name"] == "gpt-image-2.5-flare")
    flare["imageEditingDefault"] = True
    path.write_text(json.dumps(source), encoding="utf-8")
    capsys.readouterr()
    assert validator.main() == 1
    assert "imageEditingDefault may mark at most one model" in capsys.readouterr().out
