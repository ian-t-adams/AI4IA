"""Operator-invoked hard-admission bootstrap and unknown-hold resolution.

    python -m ai4ia_api.hard_quota.operator bootstrap --endpoint URL --database NAME --owner ID ...
    python -m ai4ia_api.hard_quota.operator resolve --endpoint URL --database NAME --owner ID ...
        --dispatched-before EPOCH --evidence-reference HTTPS_URL

Default is a read-only plan. Writes require ``--apply --approve-plan <sha256>``
for the exact plan a fresh observation reproduces. There is no API route, azd
hook, migration, upsert, delete or automatic sweep. Output carries owner and
operation hashes, never raw ids, payloads or provider data. Exit 0 means a
ready plan or a fully applied change; 2 means refused, blocked, partial or unknown.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

from ..entitlements.models import DAY_SECONDS
from .cosmos_store import USAGE_CONTAINER, check_account_layout, check_container_layout, coordination_time
from .factory import CosmosConnection, connect_cosmos
from .models import (
    MAX_QUANTITY, POLICY_VERSION, STATE_ID, STATE_KIND, QuotaError, QuotaState, canonical_digest,
    state_document,
)
from .rollout import evidence_reference

TOOL = "ai4ia-hard-quota-operator"
VERSION = 1
MAX_OWNERS = 256
MAX_COHORT_BYTES = 64 * 1024
CALL_SECONDS = 15
RUN_SECONDS = 600
HOLD_MIN_AGE_SECONDS = DAY_SECONDS
MAX_RESOLVE_ATTEMPTS = 8
RESOLUTION = "operator-resolve-v1"
_ENDPOINT = re.compile(r"https://([a-z0-9](?:[a-z0-9-]{1,42}[a-z0-9]))\.documents\.azure\.com(?::443)?/?")
_DATABASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,254}")
_SOURCES = ("operator.py", "models.py", "cosmos_store.py", "rollout.py")
NOTICE = (
    "Plan approval binds scope; it is not proof of human authority or of the referenced "
    "evidence. Documents are created only when absent and never replaced or deleted. "
    "Resolution settles only planned request-only holds at their full bound from the "
    "resolution time. No rollout record, flag, RBAC or deployment is changed by this tool."
)


class OperatorError(ValueError):
    """A fixed diagnostic, never an upstream error body."""


class OutcomeUnknown(OperatorError):
    """A write may have been applied; the operator must re-plan, never retry blindly."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, f"{TOOL}: invalid_arguments (use --help)\n")


@dataclass(frozen=True)
class Target:
    endpoint: str
    host: str
    database: str
    owners: tuple[str, ...]


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_sha256() -> str:
    here = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in _SOURCES:
        digest.update(name.encode("ascii") + b"\0" + (here / name).read_bytes() + b"\0")
    return digest.hexdigest()


def owner_id(value: object) -> str:
    # Internal owner ids are canonical UUIDv5 strings; control partitions are not.
    if not isinstance(value, str):
        raise OperatorError("Owner ids must be canonical internal UUIDs.")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise OperatorError("Owner ids must be canonical internal UUIDs.") from None
    if str(parsed) != value or parsed.int == 0:
        raise OperatorError("Owner ids must be canonical internal UUIDs.")
    return value


def cohort(owners: Sequence[str], path: Path | None) -> tuple[str, ...]:
    values: list[object] = list(owners)
    if path is not None:
        with path.open("rb") as stream:
            raw = stream.read(MAX_COHORT_BYTES + 1)
        if len(raw) > MAX_COHORT_BYTES:
            raise OperatorError("Cohort file is too large.")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OperatorError("Cohort file must be JSON.") from None
        if (
            not isinstance(document, dict) or set(document) != {"schemaVersion", "owners"}
            or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
            or not isinstance(document["owners"], list)
        ):
            raise OperatorError("Cohort file must be exactly {schemaVersion: 1, owners: [...]}.")
        values.extend(document["owners"])
    if not 1 <= len(values) <= MAX_OWNERS:
        raise OperatorError(f"Provide between 1 and {MAX_OWNERS} owner ids.")
    parsed = [owner_id(value) for value in values]
    if len(set(parsed)) != len(parsed):
        raise OperatorError("Duplicate owner ids.")
    return tuple(sorted(parsed))


def target(endpoint: str, database: str, owners: Sequence[str], path: Path | None) -> Target:
    match = _ENDPOINT.fullmatch(endpoint)
    if match is None:
        raise OperatorError("Endpoint must be exactly https://<account>.documents.azure.com/.")
    if _DATABASE.fullmatch(database) is None:
        raise OperatorError("Invalid database name.")
    host = urlsplit(endpoint).hostname
    assert host is not None
    return Target(endpoint, host, database, cohort(owners, path))


async def bounded(awaitable: Awaitable[Any]) -> Any:
    async with asyncio.timeout(CALL_SECONDS):
        return await awaitable


def body_only(raw: Any) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def strict_state(raw: Any, owner: str) -> QuotaState:
    """The same persisted decoder the runtime uses; malformed state is never repaired."""
    if not isinstance(raw, dict) or (raw.get("id"), raw.get("kind"), raw.get("policyVersion")) != (
        STATE_ID, STATE_KIND, POLICY_VERSION,
    ):
        raise OperatorError("Incompatible hard quota document.")
    try:
        state = QuotaState.model_validate(body_only(raw), context={"persisted_quota": True})
        state_document(state)
    except (QuotaError, ValueError) as exc:
        raise OperatorError("Incompatible hard quota document.") from exc
    if state.userId != owner:
        raise OperatorError("Incompatible hard quota document.")
    return state


async def before_write(awaitable: Awaitable[Any]) -> Any:
    """Any failure before a write is a clean refusal, not an unknown outcome."""
    try:
        return await bounded(awaitable)
    except OperatorError:
        raise
    except Exception as exc:  # noqa: BLE001 - fixed diagnostic only
        raise OperatorError("Cosmos read unavailable before writing; nothing was written.") from exc


def store_time(response: Any) -> int:
    try:
        return coordination_time(response)
    except (QuotaError, ValueError) as exc:
        raise OperatorError("Store clock unavailable; nothing was written.") from exc


async def observe_layout(connection: CosmosConnection) -> tuple[dict[str, Any], int]:
    try:
        check_account_layout(await bounded(connection.read_account()))
        properties = await bounded(connection.container.read())
        check_container_layout(properties)
        now = coordination_time(properties)
    except QuotaError as exc:
        raise OperatorError(str(exc)) from exc
    return {
        "container": USAGE_CONTAINER, "partitionKey": "/userId", "singleWriteRegion": True,
        "consistency": "Session", "defaultTtl": properties.get("defaultTtl"),
        "analyticalStorageTtl": properties.get("analyticalStorageTtl"),
    }, now


async def read_owner(connection: CosmosConnection, owner: str) -> tuple[str, QuotaState | None]:
    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    try:
        raw = await bounded(connection.container.read_item(item=STATE_ID, partition_key=owner))
    except CosmosResourceNotFoundError:
        return "absent", None
    try:
        return "present", strict_state(raw, owner)
    except OperatorError:
        return "incompatible", None


def scope(target_: Target) -> dict[str, str]:
    return {"endpointHost": target_.host, "database": target_.database, "container": USAGE_CONTAINER}


def plan_header(command: str, target_: Target, layout: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": TOOL, "version": VERSION, "command": command, "source": source_sha256(),
        "scope": scope(target_), "layout": layout,
    }


def view(
    plan: dict[str, Any], *, mode: str, status: str, observed_at: int,
    owners: list[dict[str, Any]], results: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    result = {
        "tool": TOOL, "command": plan["command"], "mode": mode, "status": status,
        "plan_sha256": canonical_digest(plan), "source_sha256": plan["source"],
        "scope": plan["scope"], "layout": plan["layout"], "observedAt": observed_at,
        "owners": owners, "results": results or [], "notice": NOTICE,
    }
    if error is not None:
        result["error"] = error
    return result


# -- bootstrap -----------------------------------------------------------------

async def observe_bootstrap(
    connection: CosmosConnection, target_: Target,
) -> tuple[dict[str, Any], int]:
    layout, now = await observe_layout(connection)
    observations: list[dict[str, Any]] = []
    for owner in target_.owners:
        observed, state = await read_owner(connection, owner)
        entry: dict[str, Any] = {"owner": owner, "observed": observed}
        if state is not None:
            # Immutable identity fields only: ordinary admission churn in an
            # existing document never invalidates an approved bootstrap plan.
            entry.update(epoch=state.epoch, validAfter=state.validAfter)
        observations.append(entry)
    return {**plan_header("bootstrap", target_, layout), "owners": observations}, now


def bootstrap_owners(plan: dict[str, Any]) -> list[dict[str, Any]]:
    actions = {"absent": "create", "present": "keep", "incompatible": "blocked"}
    return [
        {
            "ownerHash": digest_text(entry["owner"]), "observed": entry["observed"],
            "action": actions[entry["observed"]],
            **({"validAfter": entry["validAfter"]} if "validAfter" in entry else {}),
        }
        for entry in plan["owners"]
    ]


async def create_owner(connection: CosmosConnection, owner: str) -> QuotaState:
    from azure.cosmos.exceptions import CosmosResourceExistsError

    now = store_time(await before_write(connection.container.read()))
    # Every fence is the store clock at creation: history before it is unknown and
    # the runtime scope treats it as consumed. Nothing is backdated or seeded.
    state = QuotaState(
        userId=owner, epoch=uuid.uuid4().hex, validAfter=now, replayFloor=now, observedAt=now,
    )
    try:
        await bounded(connection.container.create_item(state_document(state), retry_write=0))
    except CosmosResourceExistsError:
        raise OperatorError("A document appeared concurrently; it was not replaced.") from None
    except Exception as exc:  # noqa: BLE001 - never retried automatically
        raise OutcomeUnknown("Create outcome unknown; do not retry blindly. Re-plan.") from exc
    try:
        raw = await bounded(connection.container.read_item(item=STATE_ID, partition_key=owner))
        matches = strict_state(raw, owner) == state
    except Exception as exc:  # noqa: BLE001 - the create may still have been applied
        raise OutcomeUnknown("Create readback unavailable; state is unknown. Re-plan.") from exc
    if not matches:
        raise OutcomeUnknown("Create readback does not match; state is unknown. Re-plan.")
    return state


async def bootstrap(connection: CosmosConnection, target_: Target, approve: str | None) -> dict[str, Any]:
    plan, now = await observe_bootstrap(connection, target_)
    owners = bootstrap_owners(plan)
    blocked = any(entry["observed"] == "incompatible" for entry in plan["owners"])
    if approve is None:
        return view(plan, mode="plan", status="blocked" if blocked else "ready",
                    observed_at=now, owners=owners)
    if canonical_digest(plan) != approve:
        raise OperatorError("Stale or wrong approved plan digest; review a fresh plan. Nothing was written.")
    if blocked:
        raise OperatorError("An incompatible existing document blocks bootstrap; it is never repaired.")
    results: list[dict[str, Any]] = []
    for entry in plan["owners"]:
        owner_hash = digest_text(entry["owner"])
        if entry["observed"] == "present":
            results.append({"ownerHash": owner_hash, "result": "kept"})
            continue
        try:
            state = await create_owner(connection, entry["owner"])
        except OperatorError as exc:
            results.append({
                "ownerHash": owner_hash,
                "result": "unknown" if isinstance(exc, OutcomeUnknown) else "refused",
            })
            return view(plan, mode="apply", status="partial", observed_at=now, owners=owners,
                        results=results, error=str(exc))
        results.append({
            "ownerHash": owner_hash, "result": "created", "validAfter": state.validAfter,
            "documentSha256": canonical_digest(state_document(state)),
        })
    return view(plan, mode="apply", status="applied", observed_at=now, owners=owners, results=results)


# -- hold resolution -------------------------------------------------------------

def classify_holds(state: QuotaState, cutoff: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    listed: list[dict[str, Any]] = []
    for key in sorted(state.entries):
        record = state.entries[key]
        if record.phase not in {"dispatched", "unknown"}:
            continue
        if record.phase != "dispatched":
            action = "unsupported_unknown_outcome"
        elif not record.request_only:
            action = "unsupported_token_or_dollar_bound"
        elif state.blocked:
            action = "unsupported_blocked_owner"
        elif record.dispatchedAt is None or record.dispatchedAt >= cutoff:
            action = "too_recent"
        else:
            action = "resolve"
            eligible.append({"operationId": key, "record": record.model_dump(mode="json")})
        listed.append({
            "operationHash": digest_text(key), "surface": record.surface, "phase": record.phase,
            "dispatchedAt": record.dispatchedAt, "action": action,
        })
    return eligible, listed


def check_cutoff(cutoff: int, now: int) -> None:
    # A mechanical margin in addition to the fleet evidence: no hold younger than
    # a day can be resolved, and the cutoff cannot move with the clock.
    if not 0 <= cutoff <= now - HOLD_MIN_AGE_SECONDS:
        raise OperatorError("--dispatched-before must be at least 24h before the store clock.")


async def observe_resolution(
    connection: CosmosConnection, target_: Target, cutoff: int, evidence_sha256: str,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    layout, now = await observe_layout(connection)
    check_cutoff(cutoff, now)
    observations: list[dict[str, Any]] = []
    owners: list[dict[str, Any]] = []
    for owner in target_.owners:
        observed, state = await read_owner(connection, owner)
        eligible: list[dict[str, Any]] = []
        listed: list[dict[str, Any]] = []
        if state is not None:
            eligible, listed = classify_holds(state, cutoff)
        observations.append({"owner": owner, "observed": observed, "holds": eligible})
        owners.append({"ownerHash": digest_text(owner), "observed": observed, "holds": listed})
    plan = {
        **plan_header("resolve", target_, layout), "dispatchedBefore": cutoff,
        "evidenceSha256": evidence_sha256, "owners": observations,
    }
    return plan, now, owners


async def resolve_owner(
    connection: CosmosConnection, owner: str, holds: list[dict[str, Any]], cutoff: int,
    settlement: str,
) -> int:
    from azure.core import MatchConditions
    from azure.cosmos.exceptions import CosmosAccessConditionFailedError

    for _attempt in range(MAX_RESOLVE_ATTEMPTS):
        raw = await before_write(connection.container.read_item(item=STATE_ID, partition_key=owner))
        state = strict_state(raw, owner)
        etag = raw.get("_etag")
        if not isinstance(etag, str) or not etag:
            raise OperatorError("Owner document has no ETag; nothing was written.")
        now = store_time(raw)
        check_cutoff(cutoff, now)
        entries = dict(state.entries)
        for hold in holds:
            current = entries.get(hold["operationId"])
            if current is None or current.model_dump(mode="json") != hold["record"]:
                raise OperatorError("A planned hold changed or settled; nothing was overwritten. Re-plan.")
            # Full frozen bound, charged from the resolution time: strictly after
            # the proven dispatch, so never below proven usage.
            entries[hold["operationId"]] = current.model_copy(update={
                "phase": "settled", "outcome": "unknown", "settledAt": now,
                "settlementDigest": settlement, "charged": current.bounds.amounts,
            })
        updated = state.model_copy(update={"entries": entries, "observedAt": max(state.observedAt, now)})
        try:
            body = state_document(updated)
        except (QuotaError, ValueError) as exc:
            raise OperatorError("Resolution would not be valid accounting; nothing was written.") from exc
        try:
            await bounded(connection.container.replace_item(
                item=STATE_ID, body=body, etag=etag,
                match_condition=MatchConditions.IfNotModified, retry_write=0,
            ))
        except CosmosAccessConditionFailedError:
            continue  # Another writer won; re-read and re-verify every planned hold.
        except Exception as exc:  # noqa: BLE001 - never retried automatically
            raise OutcomeUnknown("Resolution outcome unknown; do not retry blindly. Re-plan.") from exc
        try:
            readback = strict_state(await bounded(
                connection.container.read_item(item=STATE_ID, partition_key=owner),
            ), owner)
        except Exception as exc:  # noqa: BLE001 - the replacement may still have been applied
            raise OutcomeUnknown("Resolution readback unavailable; state is unknown. Re-plan.") from exc
        if any(
            readback.entries.get(hold["operationId"]) != updated.entries[hold["operationId"]]
            for hold in holds
        ):
            raise OutcomeUnknown("Resolution readback does not match; state is unknown. Re-plan.")
        return now
    raise OperatorError("The owner document kept changing; nothing further was attempted. Re-plan.")


async def resolve(
    connection: CosmosConnection, target_: Target, approve: str | None, cutoff: int, evidence: str,
) -> dict[str, Any]:
    plan, now, owners = await observe_resolution(connection, target_, cutoff, digest_text(evidence))
    blocked = any(entry["observed"] == "incompatible" for entry in plan["owners"])
    if approve is None:
        return view(plan, mode="plan", status="blocked" if blocked else "ready",
                    observed_at=now, owners=owners)
    plan_sha256 = canonical_digest(plan)
    if plan_sha256 != approve:
        raise OperatorError("Stale or wrong approved plan digest; review a fresh plan. Nothing was written.")
    if blocked:
        raise OperatorError("An incompatible owner document blocks resolution; it is never repaired.")
    settlement = canonical_digest({
        "resolution": RESOLUTION, "plan": plan_sha256, "evidence": plan["evidenceSha256"],
    })
    results: list[dict[str, Any]] = []
    for entry in plan["owners"]:
        if not entry["holds"]:
            continue
        owner_hash = digest_text(entry["owner"])
        try:
            resolved_at = await resolve_owner(
                connection, entry["owner"], entry["holds"], cutoff, settlement,
            )
        except OperatorError as exc:
            results.append({
                "ownerHash": owner_hash,
                "result": "unknown" if isinstance(exc, OutcomeUnknown) else "refused",
            })
            return view(plan, mode="apply", status="partial", observed_at=now, owners=owners,
                        results=results, error=str(exc))
        results.append({
            "ownerHash": owner_hash, "result": "resolved", "resolvedAt": resolved_at,
            "operationHashes": [digest_text(hold["operationId"]) for hold in entry["holds"]],
        })
    return view(plan, mode="apply", status="applied", observed_at=now, owners=owners, results=results)


# -- command line ----------------------------------------------------------------

def build_parser() -> Parser:
    parser = Parser(prog="python -m ai4ia_api.hard_quota.operator", description=__doc__,
                    formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("bootstrap", "Create absent owner documents, create-only."),
        ("resolve", "Settle aged request-only dispatched holds with evidence."),
    ):
        command = commands.add_parser(name, help=text, allow_abbrev=False)
        command.add_argument("--endpoint", required=True, help="https://<account>.documents.azure.com/")
        command.add_argument("--database", required=True)
        command.add_argument("--owner", action="append", default=[], help="Internal owner UUID (repeatable).")
        command.add_argument("--cohort", type=Path, help='Private JSON {"schemaVersion":1,"owners":[...]}.')
        command.add_argument("--output", type=Path, help="NEW local report file; never overwritten.")
        command.add_argument("--apply", action="store_true")
        command.add_argument("--approve-plan", help="SHA-256 printed by a reviewed, unchanged plan.")
        if name == "resolve":
            command.add_argument("--dispatched-before", type=int, required=True,
                                 help="Store-clock epoch seconds; every replica alive then has terminated.")
            command.add_argument("--evidence-reference", required=True,
                                 help="https reference to the reviewed replica-termination evidence.")
    return parser


async def execute(
    args: argparse.Namespace, target_: Target,
    connect: Callable[[str, str], Awaitable[CosmosConnection]],
) -> dict[str, Any]:
    connection = await connect(target_.endpoint, target_.database)
    try:
        async with asyncio.timeout(RUN_SECONDS):
            if args.command == "bootstrap":
                return await bootstrap(connection, target_, args.approve_plan)
            return await resolve(
                connection, target_, args.approve_plan, args.dispatched_before, args.evidence_reference,
            )
    finally:
        await connection.close()


def main(
    argv: Sequence[str] | None = None, *,
    connect: Callable[[str, str], Awaitable[CosmosConnection]] = connect_cosmos,
) -> int:
    args = build_parser().parse_args(argv)
    stream = None
    try:
        if args.apply != (args.approve_plan is not None) or (
            args.approve_plan is not None and re.fullmatch("[0-9a-f]{64}", args.approve_plan) is None
        ):
            raise OperatorError("--apply requires --approve-plan <sha256>; approval is invalid without --apply.")
        target_ = target(args.endpoint, args.database, args.owner, args.cohort)
        if args.command == "resolve":
            try:
                evidence_reference(args.evidence_reference)
            except ValueError:
                raise OperatorError("--evidence-reference must be a credential-free https URL.") from None
            if not 0 <= args.dispatched_before <= MAX_QUANTITY:
                raise OperatorError("--dispatched-before must be non-negative epoch seconds.")
        # Reserve the report before connecting: an existing file is never rewritten.
        stream = args.output.open("xb") if args.output is not None else None
        result = asyncio.run(execute(args, target_, connect))
        body = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        if stream is not None:
            stream.write(body.encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        sys.stdout.write(body)
        return 0 if result["status"] in {"ready", "applied"} else 2
    except OperatorError as exc:
        message = str(exc)
    except FileExistsError:
        message = "The output file already exists; it was not overwritten."
    except KeyboardInterrupt:
        message = "Interrupted; any --apply writes may be partial or unknown. Re-plan before retrying."
    except Exception:  # noqa: BLE001 - fixed diagnostics only, never upstream bodies
        message = (
            "Cosmos or local input unavailable; coverage unknown and any --apply writes may be "
            "partial. Re-plan before retrying."
        )
    finally:
        if stream is not None:
            stream.close()
    sys.stderr.write(json.dumps({"tool": TOOL, "status": "refused", "error": message}) + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
