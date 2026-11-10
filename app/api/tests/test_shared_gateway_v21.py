"""Pinned, synthetic 2.1 metadata only: no route or model activation."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from ai4ia_api.shared_gateway_contract import (
    SharedGatewayManifestError,
    load_shared_gateway_manifest,
)
from tests.conftest import make_settings
from tests.test_shared_gateway_staging import CALLER_CLIENT_ID, _manifest


EXAMPLE = Path(__file__).with_name("shared_gateway_v21.example.json")


def example() -> dict:
    return json.loads(EXAMPLE.read_bytes())


def resolved() -> dict:
    value = example()
    value["status"] = "resolved"
    value["auth"] = _manifest()["auth"]
    value["gateway"]["allowedHosts"] = ["gateway.test"]
    for field in ("baseUrl", "anthropicBaseUrl", "videoBaseUrl"):
        value["gateway"][field] = value["gateway"][field].replace(
            "gateway.example.invalid", "gateway.test",
        )
    return value


def load(value: dict):
    return load_shared_gateway_manifest(
        json.dumps(value), api_identity_client_id=value["auth"]["callerClientId"],
    )


def test_pinned_example_bytes_and_disabled_metadata():
    # Gateway PR #3 c3bbcc88; example blob 01471e5011380ba1a76af713cfbe1841a66bfb9f.
    assert hashlib.sha256(EXAMPLE.read_bytes()).hexdigest() == (
        "fb9760da2c9928c1be722e07bcf5b505169c2ebf79f8dd8d628e6f70472c0d99"
    )
    manifest = load(example())
    assert manifest.contract_version == "2.1.0"
    assert manifest.status == "schema-only"
    assert set(manifest.capabilities) == {"claudeMessages", "videoJobs"}
    assert all(not family.enabled for family in manifest.capabilities.values())


@pytest.mark.parametrize("version", ["2.2.0", "3.0.0", None, 2])
def test_unknown_versions_refuse(version):
    value = example()
    value["contractVersion"] = version
    with pytest.raises(SharedGatewayManifestError):
        load(value)


@pytest.mark.parametrize("version", ["2.0.0", "2.1.0"])
def test_duplicate_keys_only_rejected_for_v21(version):
    value = _manifest() if version == "2.0.0" else example()
    raw = json.dumps(value).replace(
        '"clientSdkMaxRetries": 0', '"clientSdkMaxRetries": 1, "clientSdkMaxRetries": 0',
    )
    if version == "2.1.0":
        with pytest.raises(SharedGatewayManifestError, match="duplicate"):
            load_shared_gateway_manifest(raw, api_identity_client_id=CALLER_CLIENT_ID)
    else:
        assert load_shared_gateway_manifest(
            raw, api_identity_client_id=CALLER_CLIENT_ID,
        ).contract_version == "2.0.0"


@pytest.mark.parametrize("legacy_case", ["alias-equals-deployment", "empty-forbidden-headers"])
def test_v20_preserves_legacy_acceptances(legacy_case):
    value = _manifest()
    if legacy_case == "alias-equals-deployment":
        for model in value["models"].values():
            model["kind"] = "alias"
    else:
        value["privacy"]["forbiddenRequestHeaders"] = []
    assert load(value).contract_version == "2.0.0"


@pytest.mark.parametrize("family", ["claudeMessages", "videoJobs"])
def test_complete_resolved_family_and_exact_port(family):
    value = resolved()
    value["capabilities"][family]["enabled"] = True
    for field in ("baseUrl", "anthropicBaseUrl", "videoBaseUrl"):
        value["gateway"][field] = value["gateway"][field].replace(".test/", ".test:8443/")
    parsed = load(value)
    assert parsed.capabilities[family].enabled
    assert ":8443/" in (parsed.capabilities[family].base_url or "")


@pytest.mark.parametrize("status", ["schema-only", "resolved"])
@pytest.mark.parametrize("synthetic_host", ["example.invalid", "gateway.example.invalid"])
def test_mixed_synthetic_hosts_refuse_in_both_states(status, synthetic_host):
    value = resolved() if status == "resolved" else example()
    if status == "schema-only":
        value["gateway"]["allowedHosts"] = [synthetic_host]
        for field in ("baseUrl", "anthropicBaseUrl", "videoBaseUrl"):
            value["gateway"][field] = value["gateway"][field].replace(
                "gateway.example.invalid", synthetic_host,
            )
    assert load(value).status == status
    value["gateway"]["allowedHosts"].append(
        synthetic_host if status == "resolved" else "gateway.test",
    )
    with pytest.raises(SharedGatewayManifestError):
        load(value)


@pytest.mark.parametrize(
    ("host", "synthetic"),
    [
        ("example.invalid", True),
        ("nested.gateway.example.invalid", True),
        ("notexample.invalid", False),
        ("example.invalid.test", False),
    ],
)
@pytest.mark.parametrize("status", ["schema-only", "resolved"])
def test_synthetic_host_boundary(host, synthetic, status):
    value = example() if status == "schema-only" else resolved()
    value["gateway"]["allowedHosts"] = [host]
    original = "gateway.example.invalid" if status == "schema-only" else "gateway.test"
    for field in ("baseUrl", "anthropicBaseUrl", "videoBaseUrl"):
        value["gateway"][field] = value["gateway"][field].replace(original, host)
    if synthetic == (status == "schema-only"):
        assert load(value).status == status
    else:
        with pytest.raises(SharedGatewayManifestError):
            load(value)


@pytest.mark.parametrize("family", ["claudeMessages", "videoJobs"])
def test_v21_duplicate_nested_family_keys_refuse(family):
    value = example()
    raw = json.dumps(value).replace(
        f'"{family}": {{', f'"{family}": {{}}, "{family}": {{',
    )
    with pytest.raises(SharedGatewayManifestError, match="duplicate"):
        load_shared_gateway_manifest(raw, api_identity_client_id=value["auth"]["callerClientId"])


def test_v21_does_not_expand_existing_openai_operations():
    value = example()
    value["operations"].append({"method": "POST", "path": "/embeddings", "stream": "forbidden"})
    with pytest.raises(SharedGatewayManifestError, match="operation"):
        load(value)


@pytest.mark.parametrize(
    ("section", "key", "invalid"),
    [
        ("auth", "accessTokenVersion", 2.0),
        ("auth", "accessTokenVersion", True),
        ("retry", "clientSdkMaxRetries", 0.0),
        ("retry", "clientSdkMaxRetries", False),
    ],
)
def test_exact_integer_contract(section, key, invalid):
    value = resolved()
    value[section][key] = invalid
    with pytest.raises(SharedGatewayManifestError):
        load(value)


@pytest.mark.parametrize("family", ["claudeMessages", "videoJobs"])
@pytest.mark.parametrize("invalid", [True, 1, None, "true", [], {}])
def test_schema_only_enablement_and_non_boolean_refuse(family, invalid):
    value = example()
    value["capabilities"][family]["enabled"] = invalid
    with pytest.raises(SharedGatewayManifestError):
        load(value)


@pytest.mark.parametrize("field", ["baseUrl", "anthropicBaseUrl", "videoBaseUrl"])
@pytest.mark.parametrize("suffix", ["\n", "\r", "\t", "/", "?key=secret", "#fragment"])
def test_urls_are_exact_original_strings(field, suffix):
    value = example()
    value["gateway"][field] += suffix
    with pytest.raises(SharedGatewayManifestError):
        load(value)


@pytest.mark.parametrize("field", ["anthropicBaseUrl", "videoBaseUrl"])
@pytest.mark.parametrize(
    "authority", ["other.example.invalid", "gateway.example.invalid:443",
                  "user:secret@gateway.example.invalid", "gateway.example.invalid:0"],
)
def test_family_urls_cannot_change_authority_even_while_disabled(field, authority):
    value = example()
    value["gateway"][field] = value["gateway"][field].replace(
        "gateway.example.invalid", authority,
    )
    with pytest.raises(SharedGatewayManifestError):
        load(value)


@pytest.mark.parametrize("family", ["claudeMessages", "videoJobs"])
@pytest.mark.parametrize("missing", ["url", "models", "operations", "enabled"])
def test_enabled_requires_complete_family(family, missing):
    value = resolved()
    value["capabilities"][family]["enabled"] = True
    if missing == "url":
        del value["gateway"]["anthropicBaseUrl" if family == "claudeMessages" else "videoBaseUrl"]
    elif missing == "models":
        value["capabilities"][family]["models"] = {}
    else:
        del value["capabilities"][family][missing]
    with pytest.raises(SharedGatewayManifestError):
        load(value)


@pytest.mark.parametrize("family", ["claudeMessages", "videoJobs"])
def test_disabled_can_omit_url_and_models(family):
    value = example()
    del value["gateway"]["anthropicBaseUrl" if family == "claudeMessages" else "videoBaseUrl"]
    value["capabilities"][family]["models"] = {}
    assert load(value).capabilities[family].base_url is None


@pytest.mark.parametrize(
    "mutation",
    [
        "model-key", "backend-newline", "backend-url", "deployment-control", "target-extra",
        "missing-backend", "duplicate-target", "cross-deployment", "alias-equals-deployment",
        "deployment-key-mismatch", "duplicate-operation", "wrong-operation", "extra-family",
        "extra-family-field", "wrong-method", "extra-operation-field", "empty-privacy",
    ],
)
@pytest.mark.parametrize("family", ["claudeMessages", "videoJobs"])
def test_capability_shape_and_binding_mutations(family, mutation):
    value = example()
    capability = value["capabilities"][family]
    key = next(iter(capability["models"]))
    model = capability["models"][key]
    target = model["targets"][0]
    if mutation == "model-key":
        capability["models"][key + "\n"] = capability["models"].pop(key)
    elif mutation == "backend-newline":
        target["backend"] += "\n"
    elif mutation == "backend-url":
        target["backend"] = "https://provider.test"
    elif mutation == "deployment-control":
        target["deployment"] += "\x00"
    elif mutation == "target-extra":
        target["url"] = "https://provider.test"
    elif mutation == "missing-backend":
        del target["backend"]
    elif mutation == "duplicate-target":
        model["targets"].append(deepcopy(target))
    elif mutation == "cross-deployment":
        model["targets"].append({**target, "deployment": "other-synthetic"})
    elif mutation == "alias-equals-deployment":
        target["deployment"] = key
    elif mutation == "deployment-key-mismatch":
        model["kind"] = "deployment-name"
    elif mutation == "duplicate-operation":
        model["operations"].append(model["operations"][0])
    elif mutation == "wrong-operation":
        model["operations"][0] = "/responses"
    elif mutation == "extra-family":
        value["capabilities"]["other"] = {}
    elif mutation == "extra-family-field":
        capability["fallback"] = True
    elif mutation == "wrong-method":
        next(iter(capability["operations"].values()))["method"] = "DELETE"
    elif mutation == "extra-operation-field":
        next(iter(capability["operations"].values()))["retry"] = True
    elif mutation == "empty-privacy":
        value["privacy"]["forbiddenRequestHeaders"] = []
    with pytest.raises(SharedGatewayManifestError):
        load(value)


def test_video_operation_order_is_not_significant():
    value = example()
    value["capabilities"]["videoJobs"]["models"]["example-video"]["operations"].reverse()
    assert not load(value).capabilities["videoJobs"].enabled


@pytest.mark.parametrize("family", ["claudeMessages", "videoJobs"])
@pytest.mark.parametrize("state", ["missing", "v2", "schema-only", "disabled", "eligible"])
def test_local_opt_in_validation_only(family, state):
    value = resolved()
    value["capabilities"][family]["enabled"] = state == "eligible"
    if state == "v2":
        value = _manifest()
    elif state == "schema-only":
        value["status"] = "schema-only"
    elif state == "missing":
        value = None
    settings = make_settings(
        env="dev", auth_provider="entra", AZURE_CLIENT_ID=CALLER_CLIENT_ID,
        shared_gateway_activation_manifest=json.dumps(value) if value else None,
        **{
            "shared_gateway_claude_messages_enabled" if family == "claudeMessages"
            else "shared_gateway_video_jobs_enabled": True,
        },
    )
    if state == "eligible":
        settings.validate_shared_gateway()
    else:
        with pytest.raises(RuntimeError):
            settings.validate_shared_gateway()


def test_local_flags_default_off_ignore_unselected_manifest():
    settings = make_settings(shared_gateway_activation_manifest="not JSON")
    assert not settings.shared_gateway_claude_messages_enabled
    assert not settings.shared_gateway_video_jobs_enabled
    settings.validate_shared_gateway()


def test_openai_selector_cannot_admit_schema_only_v21():
    settings = make_settings(
        env="dev", auth_provider="entra", shared_gateway_enabled=True,
        AZURE_CLIENT_ID=example()["auth"]["callerClientId"],
        shared_gateway_activation_manifest=json.dumps(example()),
    )
    with pytest.raises(RuntimeError, match="resolved"):
        settings.validate_shared_gateway()
