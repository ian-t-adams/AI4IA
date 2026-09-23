"""Operator bootstrap and hold resolution against a stateful Cosmos-shaped fake."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from ai4ia_api.entitlements.models import DAY_SECONDS, MINUTE_SECONDS, EntitlementLimits
from ai4ia_api.hard_quota.cosmos_store import CosmosReservationStore
from ai4ia_api.hard_quota.factory import CosmosConnection
from ai4ia_api.hard_quota.models import (
    STATE_ID,
    Amounts,
    Bounds,
    QuotaError,
    QuotaState,
    canonical_digest,
    operation_id,
    state_document,
)
from ai4ia_api.hard_quota.operator import HOLD_MIN_AGE_SECONDS, MAX_OWNERS, RESOLUTION, main
from ai4ia_api.hard_quota.service import RequestCountScope, ReservationService
from tests.test_hard_quota_reservations import CosmosDocument, accounting_bounds

NOW = 1_800_000_000
START = NOW - 3 * DAY_SECONDS
ALICE = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://owners.example.test/alice"))
BOB = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://owners.example.test/bob"))
ENDPOINT = ["--endpoint", "https://ai4ia-test.documents.azure.com:443/", "--database", "ai4ia"]
EVIDENCE = "https://reviews.example.test/ai4ia/replicas-terminated"
REQUEST = Bounds(amounts=Amounts(), basis="request-v1")
SESSION_ACCOUNT = {
    "enableMultipleWriteLocations": False, "writableLocations": [{}],
    "consistencyPolicy": {"defaultConsistencyLevel": "Session"},
}


def owner_hash(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


class OperatorContainer:
    """ETags follow row state; every read and write is recorded."""

    def __init__(self, clock: list[int]) -> None:
        self.clock = clock
        self.rows: dict[tuple[str, str], dict] = {}
        self.layout = {"id": "usage", "partitionKey": {"paths": ["/userId"], "kind": "Hash"}}
        self.reads: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, object]] = []
        self.before_create = None
        self.before_replace = None

    def put(self, state: QuotaState, etag: str = "1") -> None:
        self.rows[(state.userId, STATE_ID)] = {**state_document(state), "_etag": etag}

    async def read(self):
        return CosmosDocument(copy.deepcopy(self.layout), self.clock[0])

    async def read_item(self, *, item, partition_key):
        self.reads.append((partition_key, item))
        row = self.rows.get((partition_key, item))
        if row is None:
            raise CosmosResourceNotFoundError()
        return CosmosDocument(copy.deepcopy(row), self.clock[0])

    async def create_item(self, body, **kwargs):
        self.writes.append(("create", body["userId"], kwargs.get("retry_write")))
        if self.before_create is not None:
            hook, self.before_create = self.before_create, None
            hook(body)
        key = (body["userId"], body["id"])
        if key in self.rows:
            raise CosmosResourceExistsError()
        self.rows[key] = {**copy.deepcopy(body), "_etag": "1"}
        return CosmosDocument(copy.deepcopy(self.rows[key]), self.clock[0])

    async def replace_item(self, *, item, body, etag=None, match_condition=None, **kwargs):
        self.writes.append(("replace", body["userId"], kwargs.get("retry_write")))
        if self.before_replace is not None:
            hook, self.before_replace = self.before_replace, None
            await hook()
        key = (body["userId"], item)
        current = self.rows.get(key)
        if current is None:
            raise CosmosResourceNotFoundError()
        if match_condition != MatchConditions.IfNotModified or etag != current["_etag"]:
            raise CosmosAccessConditionFailedError()
        self.rows[key] = {**copy.deepcopy(body), "_etag": str(int(current["_etag"]) + 1)}
        return CosmosDocument(copy.deepcopy(self.rows[key]), self.clock[0])

    async def upsert_item(self, *args, **kwargs):
        raise AssertionError("the operator never upserts")

    async def delete_item(self, *args, **kwargs):
        raise AssertionError("the operator never deletes")


class Deployment:
    def __init__(self) -> None:
        self.clock = [NOW]
        self.container = OperatorContainer(self.clock)
        self.account = copy.deepcopy(SESSION_ACCOUNT)
        self.connects: list[tuple[str, str]] = []
        self.closed = 0

    async def read_account(self):
        return copy.deepcopy(self.account)

    async def close(self) -> None:
        self.closed += 1

    async def connect(self, endpoint: str, database: str) -> CosmosConnection:
        self.connects.append((endpoint, database))
        return CosmosConnection(self.container, self.read_account, self.close)

    def runtime(self, coverage: int | None = START) -> ReservationService:
        store = CosmosReservationStore(self.container, read_account=self.read_account)
        return ReservationService(store, scope=None if coverage is None else RequestCountScope(coverage))

    def row(self, owner: str) -> dict:
        return copy.deepcopy(self.container.rows[(owner, STATE_ID)])


def invoke(deployment: Deployment, capsys, *argv: str) -> tuple[int, dict | None, str]:
    code = main(list(argv), connect=deployment.connect)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out else None), err


def bootstrap(deployment, capsys, *extra: str, owners=(ALICE, BOB)):
    args = ["bootstrap", *ENDPOINT]
    for owner in owners:
        args += ["--owner", owner]
    return invoke(deployment, capsys, *args, *extra)


def existing(owner: str = BOB, at: int = NOW - 100) -> QuotaState:
    return QuotaState(userId=owner, epoch="b" * 32, validAfter=at, replayFloor=at, observedAt=at)


def test_bootstrap_plan_is_read_only_and_reproducible(capsys):
    deployment = Deployment()
    deployment.container.put(existing())
    code, plan, _ = bootstrap(deployment, capsys)
    assert code == 0 and plan is not None
    assert (plan["mode"], plan["status"]) == ("plan", "ready")
    assert {entry["ownerHash"]: entry["action"] for entry in plan["owners"]} == {
        owner_hash(ALICE): "create", owner_hash(BOB): "keep",
    }
    assert deployment.container.writes == [] and deployment.closed == 1
    rendered = json.dumps(plan)
    assert ALICE not in rendered and BOB not in rendered
    # Ordinary admission churn in an existing document leaves the plan unchanged.
    deployment.container.put(existing().model_copy(update={"observedAt": NOW}), etag="7")
    deployment.clock[0] += 30
    code, again, _ = bootstrap(deployment, capsys)
    assert code == 0 and again is not None and again["plan_sha256"] == plan["plan_sha256"]


@pytest.mark.parametrize("extra", [
    ["--apply"], ["--approve-plan", "a" * 64], ["--apply", "--approve-plan", "not-a-digest"],
])
def test_apply_and_approval_are_only_valid_together(capsys, extra):
    deployment = Deployment()
    code, _, err = bootstrap(deployment, capsys, *extra)
    assert code == 2 and "--apply requires --approve-plan" in err
    assert deployment.connects == [] and deployment.container.writes == []


def test_apply_requires_the_digest_a_fresh_observation_reproduces(capsys):
    deployment = Deployment()
    code, plan, _ = bootstrap(deployment, capsys)
    assert code == 0 and plan is not None
    code, _, err = bootstrap(deployment, capsys, "--apply", "--approve-plan", "0" * 64)
    assert code == 2 and "Stale or wrong approved plan digest" in err
    # A scope change between review and apply also invalidates the approval.
    deployment.container.put(existing(ALICE))
    code, _, err = bootstrap(deployment, capsys, "--apply", "--approve-plan", plan["plan_sha256"])
    assert code == 2 and "Stale or wrong approved plan digest" in err
    assert deployment.container.writes == []


def test_apply_creates_only_absent_owners_with_store_clock_fences(capsys):
    deployment = Deployment()
    deployment.container.put(existing())
    kept = deployment.row(BOB)
    code, plan, _ = bootstrap(deployment, capsys)
    assert code == 0 and plan is not None
    deployment.clock[0] = NOW + 5
    code, applied, _ = bootstrap(deployment, capsys, "--apply", "--approve-plan", plan["plan_sha256"])
    assert code == 0 and applied is not None and applied["status"] == "applied"
    assert {entry["ownerHash"]: entry["result"] for entry in applied["results"]} == {
        owner_hash(ALICE): "created", owner_hash(BOB): "kept",
    }
    assert deployment.container.writes == [("create", ALICE, 0)]
    assert deployment.row(BOB) == kept
    created = QuotaState.model_validate(
        {key: value for key, value in deployment.row(ALICE).items() if not key.startswith("_")},
        context={"persisted_quota": True},
    )
    assert (created.validAfter, created.replayFloor, created.observedAt) == (NOW + 5,) * 3
    assert created.entries == {} and created.blocked is False and len(created.epoch) == 32
    # The runtime fences the new document at its creation even under an older cutover.
    service = deployment.runtime(coverage=NOW - 3600)
    request = dict(payload={}, surface="chat", bounds=REQUEST)
    capped = EntitlementLimits(requestsPerMinute=5)
    deployment.clock[0] = NOW + 35
    with pytest.raises(QuotaError, match="history before cutover"):
        await_reserve(service, created, deployment, capped, request)
    # Control: without the rollout scope the same created document would admit.
    assert await_reserve(deployment.runtime(coverage=None), created, deployment, capped, request)
    deployment.clock[0] = NOW + 5 + MINUTE_SECONDS
    assert await_reserve(service, created, deployment, capped, request)


def await_reserve(service, state, deployment, limits, request):
    return asyncio.run(service.reserve(
        state.userId, key=operation_id(state.epoch, deployment.clock[0], uuid.uuid4().hex),
        limits=limits, **request,
    ))


def test_existing_documents_are_never_repaired_or_replaced(capsys):
    deployment = Deployment()
    deployment.container.put(existing(ALICE))
    broken = deployment.row(ALICE)
    del broken["entries"]
    deployment.container.rows[(ALICE, STATE_ID)] = broken
    code, plan, _ = bootstrap(deployment, capsys)
    assert code == 2 and plan is not None and plan["status"] == "blocked"
    assert {entry["ownerHash"]: entry["action"] for entry in plan["owners"]}[owner_hash(ALICE)] == "blocked"
    code, _, err = bootstrap(deployment, capsys, "--apply", "--approve-plan", plan["plan_sha256"])
    assert code == 2 and "never repaired" in err
    assert deployment.container.writes == [] and deployment.row(ALICE) == broken


def test_create_race_is_partial_and_never_replaces(capsys):
    deployment = Deployment()
    code, plan, _ = bootstrap(deployment, capsys, owners=(ALICE,))
    assert code == 0 and plan is not None
    concurrent = existing(ALICE, at=NOW - 1)

    def race(_body):
        deployment.container.put(concurrent)

    deployment.container.before_create = race
    code, result, _ = bootstrap(
        deployment, capsys, "--apply", "--approve-plan", plan["plan_sha256"], owners=(ALICE,),
    )
    assert code == 2 and result is not None and result["status"] == "partial"
    assert result["results"] == [{"ownerHash": owner_hash(ALICE), "result": "refused"}]
    assert "appeared concurrently" in result["error"]
    assert deployment.container.writes == [("create", ALICE, 0)]
    assert deployment.row(ALICE) == {**state_document(concurrent), "_etag": "1"}


def test_ambiguous_create_is_reported_unknown_and_not_retried(capsys):
    deployment = Deployment()
    code, plan, _ = bootstrap(deployment, capsys, owners=(ALICE,))
    assert code == 0 and plan is not None

    def timeout(_body):
        raise TimeoutError("fixture lost acknowledgement")

    deployment.container.before_create = timeout
    code, result, _ = bootstrap(
        deployment, capsys, "--apply", "--approve-plan", plan["plan_sha256"], owners=(ALICE,),
    )
    assert code == 2 and result is not None and result["status"] == "partial"
    assert result["results"] == [{"ownerHash": owner_hash(ALICE), "result": "unknown"}]
    assert "do not retry blindly" in result["error"]
    assert deployment.container.writes == [("create", ALICE, 0)]


@pytest.mark.parametrize("argv", [
    ["bootstrap", "--endpoint", "http://ai4ia-test.documents.azure.com/", "--database", "ai4ia", "--owner", ALICE],
    ["bootstrap", "--endpoint", "https://ai4ia-test.documents.azure.com/?sig=x", "--database", "ai4ia", "--owner", ALICE],
    ["bootstrap", "--endpoint", "https://evil.example.test/", "--database", "ai4ia", "--owner", ALICE],
    ["bootstrap", "--endpoint", "https://user@ai4ia-test.documents.azure.com/", "--database", "ai4ia", "--owner", ALICE],
    ["bootstrap", *ENDPOINT[:2], "--database", "bad name", "--owner", ALICE],
    ["bootstrap", *ENDPOINT, "--owner", "alice"],
    ["bootstrap", *ENDPOINT, "--owner", "__ai4ia_hard_quota_control__"],
    ["bootstrap", *ENDPOINT, "--owner", ALICE.upper()],
    ["bootstrap", *ENDPOINT, "--owner", str(uuid.UUID(int=0))],
    ["bootstrap", *ENDPOINT, "--owner", ALICE, "--owner", ALICE],
    ["bootstrap", *ENDPOINT],
    ["resolve", *ENDPOINT, "--owner", ALICE, "--dispatched-before", "1", "--evidence-reference", "http://x.test/r"],
    ["resolve", *ENDPOINT, "--owner", ALICE, "--dispatched-before", "1",
     "--evidence-reference", "https://x.test/r?token=secret"],
    ["resolve", *ENDPOINT, "--owner", ALICE, "--dispatched-before", "-1", "--evidence-reference", EVIDENCE],
])
def test_unsafe_inputs_refuse_before_connecting(capsys, argv):
    deployment = Deployment()
    code, out, err = invoke(deployment, capsys, *argv)
    assert code == 2 and out is None and json.loads(err)["status"] == "refused"
    assert deployment.connects == []


def test_cohort_is_bounded_and_exact(tmp_path, capsys):
    deployment = Deployment()
    too_many = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"owner-{index}")) for index in range(MAX_OWNERS + 1)]
    for body, valid in (
        ({"schemaVersion": 1, "owners": [ALICE, BOB]}, True),
        ({"schemaVersion": 1, "owners": [ALICE], "note": "extra"}, False),
        ({"schemaVersion": True, "owners": [ALICE]}, False),
        ({"schemaVersion": 1, "owners": too_many}, False),
    ):
        path = tmp_path / f"cohort-{len(deployment.connects)}-{valid}-{len(body)}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        before = len(deployment.connects)
        code, _, _ = invoke(deployment, capsys, "bootstrap", *ENDPOINT, "--cohort", str(path))
        assert code == (0 if valid else 2)
        assert len(deployment.connects) == before + int(valid)


def test_report_file_is_new_only(tmp_path, capsys):
    deployment = Deployment()
    report = tmp_path / "plan.json"
    code, plan, _ = bootstrap(deployment, capsys, "--output", str(report))
    assert code == 0 and json.loads(report.read_text(encoding="ascii")) == plan
    code, _, err = bootstrap(deployment, capsys, "--output", str(report))
    assert code == 2 and "already exists" in err
    assert json.loads(report.read_text(encoding="ascii")) == plan and len(deployment.connects) == 1


def test_incompatible_layout_blocks_before_any_owner_read(capsys):
    deployment = Deployment()
    deployment.account["consistencyPolicy"]["defaultConsistencyLevel"] = "Eventual"
    code, _, err = bootstrap(deployment, capsys)
    assert code == 2 and "Session-consistency" in err
    assert deployment.container.reads == [] and deployment.container.writes == []
    deployment.account["consistencyPolicy"]["defaultConsistencyLevel"] = "Session"
    assert bootstrap(deployment, capsys)[0] == 0


def test_help_is_offline(capsys):
    deployment = Deployment()
    with pytest.raises(SystemExit) as caught:
        main(["--help"], connect=deployment.connect)
    assert caught.value.code == 0 and "bootstrap" in capsys.readouterr().out
    assert deployment.connects == []


# -- hold resolution --------------------------------------------------------------

def holds_fixture():
    """An owner with an aged hold, a recent hold, settled history and an unknown token hold."""
    deployment = Deployment()
    deployment.container.put(existing(ALICE, at=START).model_copy(update={"epoch": "a" * 32}))
    scoped = deployment.runtime()
    historical = deployment.runtime(coverage=None)
    records = {}

    async def admit(service, name, at, *, bounds=REQUEST, finish=None):
        deployment.clock[0] = at
        record = await service.reserve(
            ALICE, key=operation_id("a" * 32, at, name), payload={"n": name}, surface="chat",
            bounds=bounds, limits=EntitlementLimits(),
        )
        record = await service.dispatch(ALICE, record)
        if finish is not None:
            record = await service.settle(ALICE, record, **finish)
        records[name] = record

    async def build():
        await admit(scoped, "aged", START + 10)
        await admit(scoped, "settled", START + 20, finish={"outcome": "complete", "actual": Amounts()})
        await admit(historical, "token-unknown", START + 30,
                    bounds=accounting_bounds(Amounts(tokens=10)), finish={"outcome": "cancelled"})
        await admit(historical, "token-dispatched", START + 40, bounds=accounting_bounds(Amounts(tokens=10)))
        await admit(scoped, "recent", NOW - 3600)

    asyncio.run(build())
    deployment.clock[0] = NOW
    return deployment, records


def resolve(deployment, capsys, *extra: str, cutoff: int = NOW - HOLD_MIN_AGE_SECONDS - 1):
    return invoke(
        deployment, capsys, "resolve", *ENDPOINT, "--owner", ALICE,
        "--dispatched-before", str(cutoff), "--evidence-reference", EVIDENCE, *extra,
    )


def test_resolution_plans_only_aged_request_only_dispatched_holds(capsys):
    deployment, records = holds_fixture()
    before = deployment.row(ALICE)
    writes = len(deployment.container.writes)
    code, plan, _ = resolve(deployment, capsys)
    assert code == 0 and plan is not None and plan["status"] == "ready"
    actions = {hold["operationHash"]: hold["action"] for hold in plan["owners"][0]["holds"]}
    assert actions == {
        hashlib.sha256(records["aged"].operationId.encode()).hexdigest(): "resolve",
        hashlib.sha256(records["recent"].operationId.encode()).hexdigest(): "too_recent",
        hashlib.sha256(records["token-unknown"].operationId.encode()).hexdigest():
            "unsupported_unknown_outcome",
        hashlib.sha256(records["token-dispatched"].operationId.encode()).hexdigest():
            "unsupported_token_or_dollar_bound",
    }
    assert len(deployment.container.writes) == writes and deployment.row(ALICE) == before


def test_resolution_charges_the_full_bound_from_the_resolution_time(capsys):
    deployment, records = holds_fixture()
    before = deployment.row(ALICE)
    code, plan, _ = resolve(deployment, capsys)
    assert code == 0 and plan is not None
    code, applied, _ = resolve(deployment, capsys, "--apply", "--approve-plan", plan["plan_sha256"])
    assert code == 0 and applied is not None and applied["status"] == "applied"
    after = deployment.row(ALICE)
    aged = records["aged"].operationId
    resolved = after["entries"][aged]
    assert (resolved["phase"], resolved["outcome"], resolved["settledAt"]) == ("settled", "unknown", NOW)
    assert resolved["charged"] == resolved["bounds"]["amounts"]
    assert resolved["settlementDigest"] == canonical_digest({
        "resolution": RESOLUTION, "plan": plan["plan_sha256"],
        "evidence": hashlib.sha256(EVIDENCE.encode()).hexdigest(),
    })
    untouched = {key: value for key, value in after["entries"].items() if key != aged}
    assert untouched == {key: value for key, value in before["entries"].items() if key != aged}
    # Never below proven usage: the resolved attempt stays in the minute window
    # from the resolution time, alongside the three still-held operations.
    service = deployment.runtime()
    capped = EntitlementLimits(requestsPerMinute=4)
    state = QuotaState.model_validate(
        {key: value for key, value in after.items() if not key.startswith("_")},
        context={"persisted_quota": True},
    )
    request = dict(payload={}, surface="chat", bounds=REQUEST)
    deployment.clock[0] = NOW + 30
    with pytest.raises(QuotaError, match="would be exceeded"):
        await_reserve(service, state, deployment, capped, request)
    # A late genuine settlement can never double count the resolved attempt.
    with pytest.raises(QuotaError, match="settlement changed") as caught:
        asyncio.run(service.settle(ALICE, records["aged"], outcome="complete", actual=Amounts()))
    assert caught.value.code == 409
    deployment.clock[0] = NOW + MINUTE_SECONDS + 1
    assert await_reserve(service, state, deployment, capped, request)
    # Once the known charge has aged out, the pruned identity still cannot settle again.
    with pytest.raises(QuotaError, match="does not belong"):
        asyncio.run(service.settle(ALICE, records["aged"], outcome="complete", actual=Amounts()))


def test_resolution_retries_unrelated_writes_but_refuses_changed_holds(capsys):
    deployment, records = holds_fixture()
    code, plan, _ = resolve(deployment, capsys)
    assert code == 0 and plan is not None
    runtime = deployment.runtime()

    async def unrelated():
        await runtime.reserve(
            ALICE, key=operation_id("a" * 32, NOW, "concurrent"), payload={}, surface="chat",
            bounds=REQUEST, limits=EntitlementLimits(),
        )

    deployment.container.before_replace = unrelated
    code, applied, _ = resolve(deployment, capsys, "--apply", "--approve-plan", plan["plan_sha256"])
    assert code == 0 and applied is not None and applied["status"] == "applied"
    entries = deployment.row(ALICE)["entries"]
    assert entries[operation_id("a" * 32, NOW, "concurrent")]["phase"] == "reserved"
    assert entries[records["aged"].operationId]["outcome"] == "unknown"

    deployment, records = holds_fixture()
    code, plan, _ = resolve(deployment, capsys)
    assert code == 0 and plan is not None
    runtime = deployment.runtime()

    async def genuine_settlement():
        await runtime.settle(ALICE, records["aged"], outcome="complete", actual=Amounts())

    deployment.container.before_replace = genuine_settlement
    code, result, _ = resolve(deployment, capsys, "--apply", "--approve-plan", plan["plan_sha256"])
    assert code == 2 and result is not None and result["status"] == "partial"
    assert result["results"] == [{"ownerHash": owner_hash(ALICE), "result": "refused"}]
    settled = deployment.row(ALICE)["entries"][records["aged"].operationId]
    assert (settled["phase"], settled["outcome"]) == ("settled", "complete")


def test_resolution_cutoff_must_be_a_full_day_old(capsys):
    deployment, _records = holds_fixture()
    before = deployment.row(ALICE)
    code, _, err = resolve(deployment, capsys, cutoff=NOW - HOLD_MIN_AGE_SECONDS + 1)
    assert code == 2 and "at least 24h" in err
    assert deployment.row(ALICE) == before
    code, plan, _ = resolve(deployment, capsys, cutoff=NOW - HOLD_MIN_AGE_SECONDS)
    assert code == 0 and plan is not None


def test_resolution_requires_evidence_argument():
    deployment = Deployment()
    with pytest.raises(SystemExit) as caught:
        main(["resolve", *ENDPOINT, "--owner", ALICE, "--dispatched-before", "1"], connect=deployment.connect)
    assert caught.value.code == 2 and deployment.connects == []
