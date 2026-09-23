"""The production store exists only for an exactly selected, approved rollout."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.entitlements.models import EntitlementLimits
from ai4ia_api.hard_quota import factory
from ai4ia_api.hard_quota.cosmos_store import CosmosReservationStore
from ai4ia_api.hard_quota.factory import (
    AdmissionBinding,
    CosmosConnection,
    HardQuotaActivationError,
    account_observation,
    build_admission_binding,
)
from ai4ia_api.hard_quota.models import (
    CONTROL_PARTITION,
    ROLLOUT_KIND,
    STATE_ID,
    Amounts,
    Bounds,
    QuotaError,
    QuotaState,
    operation_id,
)
from ai4ia_api.hard_quota.rollout import HardQuotaRollout, evidence_reference
from ai4ia_api.hard_quota.service import RequestCountScope, ReservationService
from ai4ia_api.hard_quota.store import LocalReservationStore
from ai4ia_api.main import create_app
from tests.conftest import make_settings
from tests.test_hard_quota_reservations import StatefulContainer

NOW = 1_800_000_000
ROLLOUT_ID = "reviewed-request-count-1"
SESSION_ACCOUNT = {
    "enableMultipleWriteLocations": False, "writableLocations": [{}],
    "consistencyPolicy": {"defaultConsistencyLevel": "Session"},
}


def reference(name: str) -> str:
    return f"https://reviews.example.test/ai4ia/hard-quota/{name}"


def approved(**overrides) -> dict:
    return {
        "id": ROLLOUT_ID, "userId": CONTROL_PARTITION, "kind": ROLLOUT_KIND, "protocol": 1,
        "state": "approved", "policyVersion": "rolling-dispatch-v1",
        "scope": "request_count_only", "singleWriteRegion": True, "noCoordinationExpiry": True,
        "coverageStart": NOW - 30,
        "writerCutoverEvidence": reference("writer-drain"),
        "bootstrapEvidence": reference("bootstrap"),
        "recoveryRetentionEvidence": reference("recovery-retention"),
        **overrides,
    }


class DatelessDocument(dict):
    def get_response_headers(self):
        return {}


class Deployment:
    """One fake Cosmos account; records every item read and every construction."""

    def __init__(self) -> None:
        self.clock = [NOW]
        self.container = StatefulContainer(lambda: self.clock[0])
        self.account = copy.deepcopy(SESSION_ACCOUNT)
        self.connects = 0
        self.closed = 0
        self.reads: list[tuple[str, str]] = []
        self.dateless = False
        self.put(approved())
        original = self.container.read_item

        async def read_item(*, item, partition_key):
            self.reads.append((partition_key, item))
            document = await original(item=item, partition_key=partition_key)
            return DatelessDocument(document) if self.dateless else document

        self.container.read_item = read_item

    def put(self, body: dict) -> None:
        # System properties are part of a real read and must be ignored, not rejected.
        self.container.rows[(CONTROL_PARTITION, ROLLOUT_ID)] = {
            **body, "_etag": "1", "_rid": "rid", "_ts": NOW - 60,
        }

    async def read_account(self):
        return copy.deepcopy(self.account)

    async def close(self) -> None:
        self.closed += 1

    async def connect(self, endpoint: str, database: str) -> CosmosConnection:
        self.connects += 1
        assert (endpoint, database) == ("https://cosmos.test", "ai4ia")
        return CosmosConnection(self.container, self.read_account, self.close)


def settings(**overrides):
    values = dict(
        session_store="cosmos", cosmos_endpoint="https://cosmos.test",
        hard_quota_enabled=True, hard_quota_rollout_id=ROLLOUT_ID,
    )
    values.update(overrides)
    return make_settings(**values)


@pytest.fixture
def constructed(monkeypatch):
    stores = []

    class SpyStore(CosmosReservationStore):
        def __init__(self, *args, **kwargs):
            stores.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(factory, "CosmosReservationStore", SpyStore)
    return stores


async def test_approved_record_and_layout_construct_the_scoped_store(constructed):
    deployment = Deployment()
    binding = await build_admission_binding(settings(), connect=deployment.connect)
    assert binding is not None and isinstance(binding.store, CosmosReservationStore)
    assert binding.scope == RequestCountScope(NOW - 30)
    assert constructed == [{
        "read_account": deployment.read_account, "layout_ttl_seconds": factory.LAYOUT_REVALIDATE_SECONDS,
    }]
    assert deployment.reads == [(CONTROL_PARTITION, ROLLOUT_ID)] and deployment.closed == 0
    # A bootstrap-shaped owner document is fenced at the approved cutover.
    deployment.container.seed(QuotaState(
        userId="alice", epoch="e" * 32, validAfter=NOW - 300, replayFloor=NOW - 300,
        observedAt=NOW - 300,
    ))
    service = ReservationService(binding.store, scope=binding.scope)
    request = dict(
        key=operation_id("e" * 32, NOW, "first"), payload={}, surface="chat",
        bounds=Bounds(amounts=Amounts(), basis="request-v1"),
    )
    with pytest.raises(QuotaError, match="history before cutover"):
        await service.reserve("alice", limits=EntitlementLimits(requestsPerMinute=5), **request)
    assert (await service.reserve("alice", limits=EntitlementLimits(), **request)).phase == "reserved"
    await binding.close()
    assert deployment.closed == 1


def mutate_record(change):
    def apply(deployment: Deployment) -> None:
        deployment.put(change(approved()))
    return apply


def without(field):
    return mutate_record(lambda body: {key: value for key, value in body.items() if key != field})


RECORD_CASES = {
    "missing": lambda deployment: deployment.container.rows.clear(),
    "id-mismatch": mutate_record(lambda body: {**body, "id": "another-rollout"}),
    "control-owner": mutate_record(lambda body: {**body, "userId": "__ai4ia_deletion_control__"}),
    "kind": mutate_record(lambda body: {**body, "kind": "session_deletion_rollout_v1"}),
    "protocol-bool": mutate_record(lambda body: {**body, "protocol": True}),
    "protocol-float": mutate_record(lambda body: {**body, "protocol": 1.0}),
    "protocol-2": mutate_record(lambda body: {**body, "protocol": 2}),
    "state": mutate_record(lambda body: {**body, "state": "pending"}),
    "scope": mutate_record(lambda body: {**body, "scope": "all_meters"}),
    "policy": mutate_record(lambda body: {**body, "policyVersion": "rolling-dispatch-v2"}),
    "single-write-int": mutate_record(lambda body: {**body, "singleWriteRegion": 1}),
    "expiry-false": mutate_record(lambda body: {**body, "noCoordinationExpiry": False}),
    "coverage-bool": mutate_record(lambda body: {**body, "coverageStart": True}),
    "coverage-float": mutate_record(lambda body: {**body, "coverageStart": float(NOW - 30)}),
    "coverage-negative": mutate_record(lambda body: {**body, "coverageStart": -1}),
    "coverage-future": mutate_record(lambda body: {**body, "coverageStart": NOW + 1}),
    "extra-field": mutate_record(lambda body: {**body, "approvedBy": "someone"}),
    "missing-bootstrap-evidence": without("bootstrapEvidence"),
    "missing-coverage": without("coverageStart"),
    "evidence-blank": mutate_record(lambda body: {**body, "writerCutoverEvidence": " "}),
    "evidence-http": mutate_record(lambda body: {
        **body, "writerCutoverEvidence": "http://reviews.example.test/drain",
    }),
    "evidence-query": mutate_record(lambda body: {
        **body, "bootstrapEvidence": "https://reviews.example.test/b?sig=secret",
    }),
    "evidence-fragment": mutate_record(lambda body: {
        **body, "recoveryRetentionEvidence": "https://reviews.example.test/r#part",
    }),
    "evidence-userinfo": mutate_record(lambda body: {
        **body, "bootstrapEvidence": "https://user:pw@reviews.example.test/b",
    }),
    "evidence-port": mutate_record(lambda body: {
        **body, "bootstrapEvidence": "https://reviews.example.test:8443/b",
    }),
    "evidence-space": mutate_record(lambda body: {
        **body, "bootstrapEvidence": "https://reviews.example.test/a b",
    }),
    "evidence-oversized": mutate_record(lambda body: {
        **body, "bootstrapEvidence": "https://reviews.example.test/" + "a" * 1000,
    }),
    "missing-date": lambda deployment: setattr(deployment, "dateless", True),
    "multiwrite": lambda deployment: deployment.account.update(enableMultipleWriteLocations=True),
    "two-regions": lambda deployment: deployment.account["writableLocations"].append({}),
    "eventual": lambda deployment: deployment.account["consistencyPolicy"].update(
        defaultConsistencyLevel="Eventual",
    ),
    "container-ttl": lambda deployment: deployment.container.layout.update(defaultTtl=3600),
    "analytical-ttl": lambda deployment: deployment.container.layout.update(analyticalStorageTtl=-1),
    "partition": lambda deployment: deployment.container.layout["partitionKey"].update(
        paths=["/sessionId"],
    ),
    "container-id": lambda deployment: deployment.container.layout.update(id="sessions"),
}


@pytest.mark.parametrize("case", sorted(RECORD_CASES))
async def test_every_record_or_layout_defect_refuses_before_construction(constructed, case):
    deployment = Deployment()
    RECORD_CASES[case](deployment)
    with pytest.raises(HardQuotaActivationError):
        await build_admission_binding(settings(), connect=deployment.connect)
    assert constructed == [] and deployment.closed == 1
    # No owner coordination document is ever read by activation.
    assert all(item != STATE_ID for _partition, item in deployment.reads)
    # Control: the identical fake with only the defect removed constructs the store.
    control = Deployment()
    binding = await build_admission_binding(settings(), connect=control.connect)
    assert binding is not None and binding.scope == RequestCountScope(NOW - 30)


@pytest.mark.parametrize("rollout_id", ["", "-leading", "has space", "a" * 129])
async def test_invalid_selector_refuses_before_connecting(constructed, rollout_id):
    deployment = Deployment()
    with pytest.raises(HardQuotaActivationError, match="rollout id is invalid"):
        await build_admission_binding(
            settings(hard_quota_rollout_id=rollout_id), connect=deployment.connect,
        )
    assert deployment.connects == 0 and deployment.reads == [] and constructed == []


async def test_slow_activation_checks_time_out_and_close(constructed, monkeypatch):
    deployment = Deployment()

    async def hang(*, item, partition_key):
        await asyncio.Event().wait()

    deployment.container.read_item = hang
    monkeypatch.setattr(factory, "STARTUP_SECONDS", 0.01)
    with pytest.raises(HardQuotaActivationError, match="timed out"):
        await build_admission_binding(settings(), connect=deployment.connect)
    assert constructed == [] and deployment.closed == 1


async def test_flag_off_and_local_fake_never_connect_to_cosmos():
    deployment = Deployment()
    assert await build_admission_binding(
        settings(hard_quota_enabled=False), connect=deployment.connect,
    ) is None
    local = await build_admission_binding(
        make_settings(hard_quota_enabled=True), connect=deployment.connect,
    )
    assert local is not None and isinstance(local.store, LocalReservationStore)
    assert local.scope is None
    with pytest.raises(QuotaError, match="absent"):
        await local.store.read("anyone")  # Never seeded: no empty balance.
    with pytest.raises(HardQuotaActivationError, match="requires the Cosmos store"):
        await build_admission_binding(
            make_settings(env="dev", hard_quota_enabled=True), connect=deployment.connect,
        )
    assert deployment.connects == 0


def test_account_projection_keeps_missing_sdk_attributes_missing():
    complete = SimpleNamespace(
        _EnableMultipleWritableLocations=False, WritableLocations=[{"name": "r"}],
        ConsistencyPolicy={"defaultConsistencyLevel": "Session"},
    )
    assert account_observation(complete) == {
        "enableMultipleWriteLocations": False, "writableLocations": [{"name": "r"}],
        "consistencyPolicy": {"defaultConsistencyLevel": "Session"},
    }
    assert account_observation(SimpleNamespace()) == {
        "enableMultipleWriteLocations": None, "writableLocations": None, "consistencyPolicy": None,
    }


@pytest.mark.parametrize("value,valid", [
    (reference("ok"), True),
    ("https://reviews.example.test:443/ok", True),
    ("https://reviews.example.test", True),
    ("https:///no-host", False),
    ("ftp://reviews.example.test/r", False),
    ("", False),
    ("https://reviews.example.test/\u00e9", False),
])
def test_evidence_reference_is_a_credential_free_https_locator(value, valid):
    if valid:
        assert evidence_reference(value) == value
    else:
        with pytest.raises(ValueError):
            evidence_reference(value)


def test_rollout_schema_is_exactly_the_documented_record():
    assert set(HardQuotaRollout.model_fields) == set(approved())


def test_app_wires_the_factory_binding_scope_and_closes_it(monkeypatch):
    closed = []
    store = LocalReservationStore()
    scope = RequestCountScope(NOW)

    async def close():
        closed.append(True)

    async def binding(received):
        assert received.hard_quota_enabled is True
        return AdmissionBinding(store, scope, close)

    monkeypatch.setattr("ai4ia_api.main.build_admission_binding", binding)
    app = create_app(make_settings(hard_quota_enabled=True))
    with TestClient(app):
        reservations = app.state.hard_quota.reservations
        assert reservations.store is store and reservations.scope is scope
        assert closed == []
    assert closed == [True]


def test_app_startup_fails_closed_when_activation_is_refused(monkeypatch):
    async def refused(_settings):
        raise HardQuotaActivationError("An approved hard quota rollout record is required.")

    monkeypatch.setattr("ai4ia_api.main.build_admission_binding", refused)
    app = create_app(make_settings(hard_quota_enabled=True))
    with pytest.raises(HardQuotaActivationError):
        with TestClient(app):
            pass


def test_app_without_hard_mode_constructs_no_coordination():
    app = create_app(make_settings())
    with TestClient(app):
        assert app.state.hard_quota_binding is None
        assert app.state.hard_quota.reservations is None
