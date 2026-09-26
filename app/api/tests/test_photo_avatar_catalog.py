"""The packaged photoAvatars block: typed, strict and residency-aware."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai4ia_api.photo_avatars.catalog import ATTRIBUTE_NAMES, load_photo_avatar_catalog

REPO = Path(__file__).resolve().parents[3]


def test_packaged_block_matches_the_infra_source():
    catalog = load_photo_avatar_catalog()
    source = json.loads((REPO / "infra" / "voice-providers.json").read_text(encoding="utf-8"))
    assert catalog.model_dump(mode="json") == json.loads(json.dumps(source["photoAvatars"]))
    assert catalog.requiredFeature == "CustomAvatar"
    assert catalog.projectSuffix == "_PhotoAvatar"
    for name in ATTRIBUTE_NAMES:
        assert catalog.attributes.options(name)


@pytest.mark.parametrize(
    "policy, allowed", [("global", True), ("zonal", True), ("us", True), ("eu", False)],
)
def test_regional_home_processing_follows_the_catalog_residency_rules(policy, allowed):
    catalog = load_photo_avatar_catalog()
    assert catalog.homeDataZone == "US"
    assert catalog.satisfies_residency(policy) is allowed
    moved = catalog.model_copy(update={"homeRegion": "swedencentral", "homeDataZone": "EU"})
    assert moved.satisfies_residency(policy) is (policy != "us")


def _write(tmp_path: Path, block: object) -> str:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"defaultProviderId": "azure_openai", "providers": [], "photoAvatars": block}))
    return str(path)


def test_missing_or_malformed_blocks_are_refused(tmp_path):
    good = json.loads((REPO / "infra" / "voice-providers.json").read_text(encoding="utf-8"))["photoAvatars"]
    assert load_photo_avatar_catalog(_write(tmp_path, good)).homeRegion == good["homeRegion"]
    load_photo_avatar_catalog.cache_clear()
    with pytest.raises(ValueError, match="no photoAvatars"):
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"providers": []}))
        load_photo_avatar_catalog(str(path))
    for mutation in (
        {"preview": {**good["preview"], "host": "attacker.example.com"}},
        {"requiredFeature": ""},
        {"promptMaxChars": 0},
        {"projectSuffix": "_Other"},
        {"endpoint": "https://example.invalid"},
    ):
        load_photo_avatar_catalog.cache_clear()
        with pytest.raises(ValidationError):
            load_photo_avatar_catalog(_write(tmp_path, {**good, **mutation}))
    load_photo_avatar_catalog.cache_clear()
