"""Content-free observations of an explicitly selected conversation cohort.

This module has no Azure imports, repository factory, mutation interface, or
approval model. Session-consistent reads are observations, not absence fences.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
VERSION = 1
PROTOCOL_SOURCE = "a077e094172819e2ab299e573370936f49f5732c"
COSMOS_ARM_API = "2024-11-15"
STORAGE_ARM_API = "2023-05-01"
COSMOS_DATA_API = "2020-07-15"
FENCE_ID = "__ai4ia_session_fence_v1__"
UPLOAD_PREFIX = "__ai4ia_upload_v1__:"
MAX_COHORT = 12
PAGE_ITEMS = 25
MAX_PAGES = 4
MAX_ROWS = 100
MAX_SAMPLES = 8
MAX_CURSOR_BYTES = 4096
MAX_PAGE_BYTES = 128 * 1024
MAX_INPUT_BYTES = 64 * 1024
MAX_FIXTURE_BYTES = 512 * 1024
MAX_REPORT_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CALLS = 160
SOURCE_SECONDS = 15
COLLECTION_SECONDS = 120
IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
ARM_ID = re.compile(
    r"/subscriptions/([a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})"
    r"/resourceGroups/([A-Za-z0-9][A-Za-z0-9_.-]{0,89})"
    r"/providers/Microsoft\.DocumentDB/databaseAccounts/([a-z0-9][a-z0-9-]{1,42}[a-z0-9])\Z",
    re.IGNORECASE | re.ASCII,
)
BLOB_ID = re.compile(
    r"/subscriptions/([a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})"
    r"/resourceGroups/([A-Za-z0-9][A-Za-z0-9_.-]{0,89})"
    r"/providers/Microsoft\.Storage/storageAccounts/([a-z0-9]{3,24})"
    r"/blobServices/default/containers/([a-z0-9][a-z0-9-]{1,61}[a-z0-9])\Z",
    re.IGNORECASE | re.ASCII,
)
Surface = Literal["sessions", "messages", "documents"]
MetadataSource = Literal["account", "sessions", "messages", "documents", "blob_service", "blob_container"]
SURFACES: tuple[Surface, ...] = ("sessions", "messages", "documents")
PARTITIONS = {"sessions": "/userId", "messages": "/sessionId", "documents": "/sessionId"}

# Only named scalar metadata is projected. In particular, never select status,
# pendingUploads, a whole record, or a message/document body.
PARENT_COLUMNS = (
    "c.id", "c.userId", "c.kind", "c.deletionProtocol", "c.deletionEpoch",
    "c._etag", "c._ts", "c.ttl", "c.attachmentStorageRequired", "c.attachmentStorageId",
    "c.status.sessionId AS statusSessionId", "c.status.state AS deletionState",
    "c.status.phase AS deletionPhase", "c.status.lastVerifiedAt AS lastVerifiedAt",
    "c.status.messagesVerified AS messagesVerified",
    "c.status.documentsVerified AS documentsVerified",
    "c.status.attachmentsVerified AS attachmentsVerified",
    "c.status.scope AS cleanupScope", "c.status.backupsErased AS backupsErased",
    "c.status.coordinationRetained AS coordinationRetained",
    "c.status.autonomousCleanup AS autonomousCleanup",
    "ARRAY_LENGTH(c.status.pendingUploads) AS pendingUploadCount",
    "c.status.pendingUploadsTruncated AS pendingUploadsTruncated",
)
CHILD_COLUMNS = (
    "c.id", "c.userId", "c.sessionId", "c.kind", "c.epoch", "c.deletionEpoch",
    "c.deletionProtocol", "c._etag", "c._ts", "c.ttl", "c.closed", "c.settled",
    "c.documentId", "c.startedAt",
)
PARENT_SQL = "SELECT " + ", ".join(PARENT_COLUMNS) + " FROM c WHERE c.id = @id AND c.userId = @owner"
CHILD_SQL = "SELECT " + ", ".join(CHILD_COLUMNS) + " FROM c WHERE c.sessionId = @session"
PARENT_FIELDS = frozenset(column.split(" AS ")[-1].removeprefix("c.") for column in PARENT_COLUMNS)
CHILD_FIELDS = frozenset(column.removeprefix("c.") for column in CHILD_COLUMNS)
CLAIMS = {
    "readOnly": True,
    "writerDrainProven": False,
    "absenceProven": False,
    "enrollmentApproved": False,
    "cleanupAuthorized": False,
    "backupsErased": False,
    "physicalErasureDeadline": None,
    "observation": "non_atomic_session_consistent_metadata_only",
}


class AssessmentError(ValueError):
    """Fixed, content-free failure codes; never provider exception text."""

    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[a-z_]{1,64}", code) else "invalid_metadata"
        super().__init__(self.code)


def canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        raise AssessmentError("invalid_json") from None


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(body: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise AssessmentError("duplicate_json_field")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise AssessmentError("invalid_json")

    try:
        return json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise AssessmentError("invalid_json") from None


def read_json(path: Path, limit: int) -> tuple[Any, str]:
    try:
        with path.open("rb") as stream:
            body = stream.read(limit + 1)
    except OSError:
        raise AssessmentError("input_unavailable") from None
    if len(body) > limit:
        raise AssessmentError("input_too_large")
    return strict_json(body), hashlib.sha256(body).hexdigest()


def obj(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssessmentError("invalid_metadata_shape")
    return value


def exact_fields(value: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    if not required <= value.keys() or value.keys() - required - (optional or set()):
        raise AssessmentError("invalid_fields")


def identity(value: object, *, reserved: bool = False) -> str:
    if (
        not isinstance(value, str) or not IDENTITY.fullmatch(value)
        or (not reserved and value.startswith("__ai4ia_"))
    ):
        raise AssessmentError("invalid_identity")
    return value


def integer(value: object, *, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise AssessmentError("invalid_numeric_metadata")
    return value


def utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value
    ):
        raise AssessmentError("invalid_timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AssessmentError("invalid_timestamp") from None


@dataclass(frozen=True)
class Pair:
    owner: str
    session: str

    @property
    def fingerprint(self) -> str:
        return digest(["ai4ia-deletion-cohort-v1", self.owner, self.session])


@dataclass(frozen=True)
class Scope:
    account_id: str
    database: str
    cohort: tuple[Pair, ...]
    blob_id: str | None = None

    @classmethod
    def parse(cls, value: object) -> Scope:
        raw = obj(value)
        exact_fields(raw, {"schemaVersion", "accountResourceId", "database", "cohort"}, {"inlineBlobContainerResourceId"})
        if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != VERSION:
            raise AssessmentError("unsupported_input_version")
        account_id = raw["accountResourceId"]
        match = ARM_ID.fullmatch(account_id) if isinstance(account_id, str) else None
        if match is None or match[3] != match[3].lower():
            raise AssessmentError("invalid_account_scope")
        database = identity(raw["database"])
        entries = raw["cohort"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_COHORT:
            raise AssessmentError("invalid_cohort_size")
        cohort = []
        for entry in entries:
            entry = obj(entry)
            exact_fields(entry, {"ownerId", "sessionId"})
            cohort.append(Pair(identity(entry["ownerId"]), identity(entry["sessionId"])))
        # Child partitions have only sessionId: an ambiguous owner alias is not
        # two independent cohorts and must not be read.
        if len({pair.session for pair in cohort}) != len(cohort):
            raise AssessmentError("duplicate_session_scope")
        blob_id = raw.get("inlineBlobContainerResourceId")
        if blob_id is not None:
            blob = BLOB_ID.fullmatch(blob_id) if isinstance(blob_id, str) else None
            if (
                blob is None or blob[1].lower() != match[1].lower()
                or blob[2].lower() != match[2].lower()
                or blob[3] != blob[3].lower() or blob[4] != blob[4].lower()
                or "--" in blob[4]
            ):
                raise AssessmentError("invalid_blob_scope")
        return cls(account_id, database, tuple(cohort), blob_id)

    def private(self) -> dict[str, Any]:
        return {
            "schemaVersion": VERSION, "accountResourceId": self.account_id, "database": self.database,
            "cohort": [{"ownerId": p.owner, "sessionId": p.session} for p in self.cohort],
            "inlineBlobContainerResourceId": self.blob_id,
        }

    @property
    def fingerprint(self) -> str:
        return digest({
            "account": self.account_id.lower(), "database": self.database,
            "cohort": sorted(p.fingerprint for p in self.cohort),
            "blob": self.blob_id.lower() if self.blob_id else None,
        })

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id.rsplit('/', 1)[1]}.documents.azure.com"

    @property
    def storage_identity(self) -> str | None:
        if self.blob_id is None:
            return None
        match = BLOB_ID.fullmatch(self.blob_id)
        if match is None:
            raise AssessmentError("invalid_blob_scope")
        target = f"https://{match[3]}.blob.core.windows.net/{match[4]}"
        return "azure:" + hashlib.sha256(target.encode("ascii")).hexdigest()

    def require_pair(self, pair: Pair) -> None:
        if pair not in self.cohort:
            raise AssessmentError("out_of_scope")


@dataclass(frozen=True)
class Query:
    surface: Surface
    pair: Pair

    @property
    def sql(self) -> str:
        return PARENT_SQL if self.surface == "sessions" else CHILD_SQL

    @property
    def partition(self) -> str:
        return self.pair.owner if self.surface == "sessions" else self.pair.session

    @property
    def parameters(self) -> list[dict[str, object]]:
        if self.surface == "sessions":
            return [{"name": "@id", "value": self.pair.session}, {"name": "@owner", "value": self.pair.owner}]
        return [{"name": "@session", "value": self.pair.session}]

    def validate(self, scope: Scope) -> None:
        scope.require_pair(self.pair)
        if self.surface not in SURFACES:
            raise AssessmentError("out_of_scope")


@dataclass(frozen=True)
class Page:
    rows: list[Any]
    continuation: str | None


class ReadFacade(Protocol):
    def metadata(self, source: MetadataSource) -> dict[str, Any]: ...

    def page(self, query: Query, continuation: str | None) -> Page: ...


@dataclass
class Budget:
    clock: Callable[[], float] = time.monotonic
    calls: int = 0
    received_bytes: int = 0
    started: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def before(self) -> None:
        if self.clock() - self.started >= COLLECTION_SECONDS:
            raise AssessmentError("collection_timeout")
        if self.calls >= MAX_CALLS:
            raise AssessmentError("call_limit")
        self.calls += 1

    def receive(self, value: object) -> None:
        size = len(canonical(value))
        self.received_bytes += size
        if size > MAX_PAGE_BYTES or self.received_bytes > MAX_TOTAL_BYTES:
            raise AssessmentError("response_byte_limit")
        if self.clock() - self.started >= COLLECTION_SECONDS:
            raise AssessmentError("collection_timeout")


def cursor(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str) or not value
        or len(value.encode("utf-8")) > MAX_CURSOR_BYTES
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise AssessmentError("invalid_continuation")
    return value


@dataclass
class Scan:
    rows: list[Any] = field(default_factory=list)
    pages: int = 0
    received: int = 0
    complete: bool = False
    issues: list[str] = field(default_factory=list)


def scan(reader: ReadFacade, scope: Scope, query: Query, budget: Budget) -> Scan:
    query.validate(scope)
    result = Scan()
    continuation = None
    seen: set[str] = set()
    try:
        for _ in range(MAX_PAGES):
            budget.before()
            page = reader.page(query, continuation)
            result.pages += 1
            if not isinstance(page.rows, list):
                raise AssessmentError("invalid_page")
            result.received += len(page.rows)
            budget.receive({"rows": page.rows, "continuation": page.continuation})
            if len(page.rows) > PAGE_ITEMS or result.received > MAX_ROWS:
                raise AssessmentError("row_limit")
            result.rows.extend(page.rows)
            continuation = cursor(page.continuation)
            if continuation is None:
                result.complete = True
                return result
            if continuation in seen:
                raise AssessmentError("repeated_continuation")
            seen.add(continuation)
        raise AssessmentError("page_limit")
    except AssessmentError as exc:
        result.issues.append(exc.code)
    return result


def metadata(reader: ReadFacade, source: MetadataSource, budget: Budget) -> dict[str, Any]:
    budget.before()
    raw = reader.metadata(source)
    budget.receive(raw)
    return obj(raw)


def account_layout(raw: dict[str, Any], scope: Scope) -> dict[str, Any]:
    properties = obj(raw.get("properties"))
    if (
        not isinstance(raw.get("id"), str) or raw["id"].lower() != scope.account_id.lower()
        or properties.get("documentEndpoint") not in (scope.endpoint, scope.endpoint + "/", scope.endpoint + ":443/")
    ):
        raise AssessmentError("account_identity_mismatch")
    writes = properties.get("writeLocations")
    if (
        not isinstance(writes, list) or not 1 <= len(writes) <= 16
        or any(
            not isinstance(w, dict) or not isinstance(w.get("locationName"), str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 ()-]{0,63}", w["locationName"])
            for w in writes
        )
        or len({w["locationName"] for w in writes}) != len(writes)
        or type(properties.get("enableMultipleWriteLocations")) is not bool
    ):
        raise AssessmentError("unknown_write_topology")
    consistency = obj(properties.get("consistencyPolicy")).get("defaultConsistencyLevel")
    if consistency not in ("Session", "Strong", "BoundedStaleness", "ConsistentPrefix", "Eventual"):
        raise AssessmentError("unknown_consistency")
    compatible = len(writes) == 1 and properties["enableMultipleWriteLocations"] is False and consistency == "Session"
    return {
        "status": "compatible" if compatible else "incompatible",
        "writeLocations": len(writes), "multipleWriteLocations": properties["enableMultipleWriteLocations"],
        "consistency": consistency, "issue": None if compatible else "incompatible_account_layout",
    }


def container_layout(raw: dict[str, Any], surface: Surface) -> dict[str, Any]:
    if (
        raw.get("id") != surface
        or obj(raw.get("partitionKey")).get("paths") != [PARTITIONS[surface]]
        or raw["partitionKey"].get("kind") != "Hash"
    ):
        raise AssessmentError("partition_layout_mismatch")
    ttl, analytical = raw.get("defaultTtl"), raw.get("analyticalStorageTtl")
    if (ttl is not None and type(ttl) is not int) or (analytical is not None and type(analytical) is not int):
        raise AssessmentError("invalid_container_retention")
    compatible = ttl in (None, -1) and analytical in (None, 0)
    return {
        "status": "compatible" if compatible else "incompatible", "partitionPath": PARTITIONS[surface],
        "defaultTtl": ttl, "analyticalStorageTtl": analytical,
        "issue": None if compatible else "expiring_or_analytical_container",
    }


def backup_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    policy = obj(obj(raw.get("properties")).get("backupPolicy"))
    mode = policy.get("type")
    if mode == "Continuous":
        tier = obj(policy.get("continuousModeProperties")).get("tier")
        if tier not in ("Continuous7Days", "Continuous30Days"):
            raise AssessmentError("unknown_backup_policy")
        return {"status": "observed", "mode": mode, "tier": tier, "retentionHours": None, "issue": None}
    if mode == "Periodic":
        hours = integer(obj(policy.get("periodicModeProperties")).get("backupRetentionIntervalInHours"), minimum=1, maximum=10000)
        return {"status": "observed", "mode": mode, "tier": None, "retentionHours": hours, "issue": None}
    raise AssessmentError("unknown_backup_policy")


def blob_metadata(service: dict[str, Any], container: dict[str, Any], scope: Scope) -> dict[str, Any]:
    if (
        scope.blob_id is None
        or str(service.get("id", "")).lower() != scope.blob_id.rsplit("/containers/", 1)[0].lower()
        or str(container.get("id", "")).lower() != scope.blob_id.lower()
    ):
        raise AssessmentError("blob_identity_mismatch")
    properties, container_properties = obj(service.get("properties")), obj(container.get("properties"))
    result: dict[str, Any] = {"status": "observed", "issue": None}
    for source, target in (("isVersioningEnabled", "versioning"),):
        value = properties.get(source)
        if type(value) is not bool:
            raise AssessmentError("unknown_blob_retention")
        result[target] = value
    for key in ("deleteRetentionPolicy", "containerDeleteRetentionPolicy"):
        policy = obj(properties.get(key))
        if type(policy.get("enabled")) is not bool:
            raise AssessmentError("unknown_blob_retention")
        result[key] = {
            "enabled": policy["enabled"],
            "days": integer(policy.get("days"), minimum=1, maximum=365) if policy["enabled"] else None,
        }
    for key in ("hasLegalHold", "hasImmutabilityPolicy"):
        if type(container_properties.get(key)) is not bool:
            raise AssessmentError("unknown_blob_retention")
        result[key] = container_properties[key]
    return result


def unknown(code: str) -> dict[str, Any]:
    return {"status": "unknown", "issue": code}


def common_record(raw: object, pair: Pair, *, parent: bool) -> dict[str, Any]:
    item = obj(raw)
    if item.keys() - (PARENT_FIELDS if parent else CHILD_FIELDS):
        raise AssessmentError("unexpected_projection_column")
    if item.get("userId") != pair.owner or item.get("id" if parent else "sessionId") != pair.session:
        raise AssessmentError("record_owner_or_partition_mismatch")
    item_id = item.get("id")
    if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,192}", item_id):
        raise AssessmentError("invalid_record_identity")
    etag = item.get("_etag")
    if not isinstance(etag, str) or not re.fullmatch(r'[\x21-\x7e]{1,256}', etag):
        raise AssessmentError("missing_or_invalid_etag")
    integer(item.get("_ts"))
    if "ttl" in item and (type(item["ttl"]) is not int or item["ttl"] != -1):
        raise AssessmentError("expiring_record")
    return item


def parent_record(raw: object, pair: Pair, observed: datetime) -> tuple[dict[str, Any], str]:
    item = common_record(raw, pair, parent=True)
    markers = ("kind", "deletionProtocol", "deletionEpoch")
    if not any(name in item for name in markers):
        if item.keys() - {"id", "userId", "_etag", "_ts", "ttl"}:
            raise AssessmentError("unversioned_coordination_metadata")
        return item, "legacy"
    if (
        item.get("kind") not in ("session_v1", "session_initializing_v1", "session_tombstone_v1")
        or type(item.get("deletionProtocol")) is not int or item["deletionProtocol"] != 1
        or type(item.get("ttl")) is not int or item["ttl"] != -1
        or type(item.get("attachmentStorageRequired")) is not bool
    ):
        raise AssessmentError("invalid_parent_protocol")
    identity(item.get("deletionEpoch"), reserved=True)
    target = item.get("attachmentStorageId")
    if target is not None and (
        not isinstance(target, str) or not re.fullmatch(r"azure:[a-f0-9]{64}|local", target)
    ):
        raise AssessmentError("invalid_attachment_target")
    if item["attachmentStorageRequired"] and target is None:
        raise AssessmentError("missing_attachment_target")
    if item["kind"] != "session_tombstone_v1":
        if item.keys() - {
            "id", "userId", "kind", "deletionProtocol", "deletionEpoch", "_etag", "_ts",
            "ttl", "attachmentStorageRequired", "attachmentStorageId",
        }:
            raise AssessmentError("unexpected_deletion_status")
        return item, "v1_active" if item["kind"] == "session_v1" else "initializing"
    if (
        item.get("statusSessionId") != pair.session
        or item.get("deletionState") not in ("pending", "retryable", "cleanup_verified")
        or item.get("deletionPhase") not in ("fences", "messages", "documents", "attachments", "uploads", "complete")
        or item.get("cleanupScope") != "conversation_content_and_inline_originals"
        or item.get("backupsErased") is not False or item.get("coordinationRetained") is not True
        or item.get("autonomousCleanup") is not False
        or any(type(item.get(key)) is not bool for key in (
            "messagesVerified", "documentsVerified", "attachmentsVerified", "pendingUploadsTruncated"
        ))
    ):
        raise AssessmentError("invalid_recorded_cleanup_status")
    integer(item.get("pendingUploadCount"), maximum=25)
    last_verified = item.get("lastVerifiedAt")
    if last_verified is not None and parse_time(last_verified) > observed:
        raise AssessmentError("future_verification_timestamp")
    if item["deletionState"] == "cleanup_verified":
        if (
            last_verified is None or item["deletionPhase"] != "complete"
            or not all(item[key] for key in ("messagesVerified", "documentsVerified", "attachmentsVerified"))
            or item["pendingUploadCount"] != 0 or item["pendingUploadsTruncated"]
        ):
            raise AssessmentError("contradictory_recorded_cleanup")
        return item, "verified"
    return item, "deleting"


def child_record(raw: object, pair: Pair, parent: dict[str, Any], surface: Surface, observed: datetime) -> tuple[dict[str, Any], str]:
    item = common_record(raw, pair, parent=False)
    kind = item.get("kind")
    if "kind" not in item:
        identity(item["id"])
        if item.keys() - {"id", "userId", "sessionId", "_etag", "_ts", "ttl"}:
            raise AssessmentError("unexpected_child_protocol")
        return item, "ordinary"
    if (
        kind not in ("session_fence_v1", "session_upload_v1")
        or parent.get("deletionProtocol") != 1
        or item.get("epoch") != parent.get("deletionEpoch")
        or type(item.get("ttl")) is not int or item["ttl"] != -1
        or "deletionProtocol" in item or "deletionEpoch" in item
    ):
        raise AssessmentError("child_protocol_or_generation_mismatch")
    if kind == "session_fence_v1":
        if (
            item["id"] != FENCE_ID or type(item.get("closed")) is not bool
            or any(key in item for key in ("settled", "documentId", "startedAt"))
        ):
            raise AssessmentError("invalid_fence")
        return item, "fence"
    if (
        surface != "documents" or not item["id"].startswith(UPLOAD_PREFIX)
        or type(item.get("settled")) is not bool or "closed" in item
        or parent.get("attachmentStorageRequired") is not True
    ):
        raise AssessmentError("invalid_upload_ticket")
    identity(item["id"][len(UPLOAD_PREFIX):])
    identity(item.get("documentId"))
    if parse_time(item.get("startedAt")) > observed:
        raise AssessmentError("invalid_upload_timestamp")
    return item, "upload"


def sample(item: dict[str, Any], pair: Pair, kind: str) -> dict[str, Any]:
    generation = item.get("epoch", item.get("deletionEpoch"))
    return {
        "identitySha256": digest([pair.fingerprint, item["id"]]), "kind": kind,
        "etagSha256": digest(item["_etag"]), "timestamp": item["_ts"],
        "generationSha256": digest(generation) if generation is not None else None,
        "ttl": item.get("ttl"), "closed": item.get("closed"), "settled": item.get("settled"),
        "uploadStartedAt": utc(parse_time(item["startedAt"])) if kind == "upload" else None,
        "documentIdentitySha256": digest([pair.fingerprint, item["documentId"]]) if kind == "upload" else None,
    }


def empty_surface() -> dict[str, Any]:
    return {
        "coverage": "not_read", "pages": 0, "rowsReceived": 0, "validRows": 0, "invalidRows": 0,
        "ordinary": 0, "settledUploads": 0, "unresolvedUploads": 0, "fence": "unknown",
        "samples": [], "samplesTruncated": False, "metadataChainSha256": digest([]), "issues": [],
    }


def summarize_scan(result: Scan, pair: Pair, parent: dict[str, Any], surface: Surface, observed: datetime) -> dict[str, Any]:
    output = empty_surface()
    output.update(
        coverage="complete" if result.complete else "partial", pages=result.pages,
        rowsReceived=result.received, issues=list(result.issues),
    )
    seen: set[str] = set()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    chain = output["metadataChainSha256"]
    fences = []
    for index, raw in enumerate(result.rows):
        try:
            item, kind = child_record(raw, pair, parent, surface, observed)
            if item["id"] in seen:
                raise AssessmentError("duplicate_record_identity")
            seen.add(item["id"])
            safe = sample(item, pair, kind)
            chain = digest([chain, safe])
            priority = 0 if kind == "fence" else 1 if kind == "upload" and not item["settled"] else 2
            candidates.append((priority, index, safe))
            candidates.sort(key=lambda candidate: candidate[:2])
            del candidates[MAX_SAMPLES:]
            if kind == "fence":
                fences.append("closed" if item["closed"] else "open")
            elif kind == "upload":
                output["settledUploads" if item["settled"] else "unresolvedUploads"] += 1
            else:
                output["ordinary"] += 1
            output["validRows"] += 1
        except AssessmentError as exc:
            output["invalidRows"] += 1
            output["issues"].append(exc.code)
    output["invalidRows"] += result.received - len(result.rows)
    output["samples"] = [candidate[2] for candidate in candidates]
    output["metadataChainSha256"] = chain
    output["samplesTruncated"] = output["validRows"] > len(candidates)
    output["fence"] = fences[0] if len(fences) == 1 else ("missing" if result.complete and not fences else "unknown")
    output["issues"] = sorted(set(output["issues"]))
    return output


def empty_row(pair: Pair) -> dict[str, Any]:
    return {
        "cohortSha256": pair.fingerprint, "classification": "unavailable",
        "parentObservation": "unavailable", "parentMetadata": None,
        "lastRecordedVerifiedAt": None, "parentReadStable": None, "inventoryComplete": False,
        "fenceAgreement": "unknown", "uploadTerminalState": "unknown",
        "attachmentTargetBinding": "unknown",
        "parentPages": 0, "parentRowsReceived": 0,
        "messages": empty_surface(), "documents": empty_surface(), "issues": [],
    }


def assess_pair(reader: ReadFacade, scope: Scope, pair: Pair, budget: Budget, observed: datetime, row: dict[str, Any]) -> None:
    initial = scan(reader, scope, Query("sessions", pair), budget)
    row["parentPages"], row["parentRowsReceived"] = initial.pages, initial.received
    row["issues"].extend(initial.issues)
    if not initial.complete:
        return
    if len(initial.rows) != 1:
        row["issues"].append("parent_not_observed" if not initial.rows else "ambiguous_parent")
        return
    try:
        parent, state = parent_record(initial.rows[0], pair, observed + timedelta(seconds=budget.clock() - budget.started))
    except AssessmentError as exc:
        row["classification"] = "malformed"
        row["issues"].append(exc.code)
        return
    row.update(
        classification=state, parentObservation=state, parentMetadata=sample(parent, pair, state),
        lastRecordedVerifiedAt=utc(parse_time(parent["lastVerifiedAt"])) if parent.get("lastVerifiedAt") else None,
    )
    required = parent.get("attachmentStorageRequired")
    row["attachmentTargetBinding"] = (
        "not_required" if required is False
        else "matched" if required is True and parent.get("attachmentStorageId") == scope.storage_identity
        else "unknown"
    )
    for surface in ("messages", "documents"):
        result = scan(reader, scope, Query(surface, pair), budget)
        row[surface] = summarize_scan(
            result, pair, parent, surface, observed + timedelta(seconds=budget.clock() - budget.started)
        )
        row["issues"].extend(row[surface]["issues"])
        if row[surface]["invalidRows"]:
            row["classification"] = "malformed"
            # An ownership/layout contradiction is not permission to read more.
            return
    final = scan(reader, scope, Query("sessions", pair), budget)
    row["parentPages"] += final.pages
    row["parentRowsReceived"] += final.received
    row["issues"].extend(final.issues)
    if final.complete and len(final.rows) == 1:
        try:
            after, _ = parent_record(final.rows[0], pair, observed + timedelta(seconds=budget.clock() - budget.started))
            row["parentReadStable"] = after == parent
        except AssessmentError as exc:
            row["issues"].append(exc.code)
    if row["parentReadStable"] is not True:
        row["issues"].append("parent_changed_or_unavailable")
    complete = (
        row["parentReadStable"] is True
        and all(row[s]["coverage"] == "complete" for s in ("messages", "documents"))
    )
    row["inventoryComplete"] = complete
    fences = [row[s]["fence"] for s in ("messages", "documents")]
    if state == "legacy":
        row["fenceAgreement"] = "legacy_no_fences" if complete and fences == ["missing", "missing"] else "unknown"
    elif fences == ["open", "open"] and state in ("v1_active", "initializing"):
        row["fenceAgreement"] = "matching_open"
    elif fences == ["closed", "closed"] and state in ("deleting", "verified"):
        row["fenceAgreement"] = "matching_closed"
    else:
        row["fenceAgreement"] = "incomplete_or_conflicting"
        row["issues"].append("fences_incomplete_or_conflicting")
    unresolved = row["documents"]["unresolvedUploads"]
    if unresolved:
        row["uploadTerminalState"] = "unresolved"
        row["issues"].append("uploads_unresolved")
    elif complete:
        row["uploadTerminalState"] = "none_observed_not_absence_proof"
    if state == "verified" and (
        any(row[s]["ordinary"] for s in ("messages", "documents"))
        or row["documents"]["settledUploads"] or unresolved
    ):
        row["issues"].append("recorded_cleanup_conflicts_with_children")
        row["classification"] = "malformed"
    if state == "initializing" and (
        any(row[s]["ordinary"] for s in ("messages", "documents"))
        or row["documents"]["settledUploads"] or unresolved
    ):
        row["issues"].append("initialization_has_persisted_children")
        row["classification"] = "malformed"
    if required is True and row["attachmentTargetBinding"] != "matched":
        row["issues"].append("attachment_target_not_observed")
    if parent.get("pendingUploadCount", 0) or parent.get("pendingUploadsTruncated", False):
        # Empty current observations cannot settle previously reported PUTs.
        row["uploadTerminalState"] = "unresolved"
        row["issues"].append("recorded_uploads_unresolved")


def source_fingerprint() -> str:
    result = []
    for name in (
        "_deletion_assessment.py", "_deletion_assessment_sdk.py",
        "assess-conversation-deletion.py", "conversation-deletion-assessment.schema.json",
    ):
        try:
            with (ROOT / "scripts" / name).open("rb") as stream:
                body = stream.read(MAX_REPORT_BYTES + 1)
        except OSError:
            raise AssessmentError("source_unavailable") from None
        if len(body) > MAX_REPORT_BYTES:
            raise AssessmentError("source_too_large")
        result.append([name, hashlib.sha256(body).hexdigest()])
    return digest(result)


def reference_evidence(value: object, scope: Scope, now: datetime) -> dict[str, Any]:
    raw = obj(value)
    exact_fields(raw, {"schemaVersion", "accountResourceId", "database", "observedAt", "references"})
    if (
        type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != VERSION
        or raw["accountResourceId"] != scope.account_id or raw["database"] != scope.database
    ):
        raise AssessmentError("reference_scope_mismatch")
    observed = parse_time(raw["observedAt"])
    if observed > now:
        raise AssessmentError("future_reference")
    entries = raw["references"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
        raise AssessmentError("invalid_references")
    references = []
    for entry in entries:
        entry = obj(entry)
        exact_fields(entry, {"kind", "reference"})
        if entry["kind"] not in ("writer_fleet", "recovery_retention"):
            raise AssessmentError("invalid_reference_kind")
        ref = entry["reference"]
        if not isinstance(ref, str) or not 1 <= len(ref) <= 1000 or any(ord(c) < 33 or ord(c) > 126 for c in ref):
            raise AssessmentError("invalid_reference")
        try:
            url = urlsplit(ref)
            valid = url.scheme == "https" and bool(url.hostname) and not (
                url.username or url.password or url.query or url.fragment or url.port not in (None, 443)
            )
        except ValueError:
            valid = False
        if not valid:
            raise AssessmentError("invalid_reference")
        references.append({"kind": entry["kind"], "referenceSha256": digest(ref)})
    return {
        "basis": "human_reference_not_verified", "observedAt": utc(observed),
        "freshWithin24Hours": now - observed <= timedelta(hours=24), "references": references,
    }


def collect(
    scope: Scope, reader: ReadFacade, *, mode: Literal["live", "synthetic"],
    input_sha256: str, now: datetime | None = None, budget: Budget | None = None,
    references: dict[str, Any] | None = None, reference_sha256: str | None = None,
) -> dict[str, Any]:
    observed = now or datetime.now(UTC)
    budget = budget or Budget()
    report: dict[str, Any] = {
        "schemaVersion": VERSION, "reportKind": "conversation_deletion_assessment",
        "source": {
            "protocolRevision": PROTOCOL_SOURCE, "assessorSha256": source_fingerprint(),
            "cosmosSdkSurface": "CosmosClient/ContainerProxy.read/query_items",
            "cosmosSdkVersion": "not_used" if mode == "synthetic" else "unknown",
            "cosmosDataApi": COSMOS_DATA_API,
            "cosmosArmApi": COSMOS_ARM_API, "storageArmApi": STORAGE_ARM_API,
            "querySha256": digest([PARENT_SQL, CHILD_SQL]),
        },
        "mode": mode, "startedAt": utc(observed), "observedAt": utc(observed), "scopeSha256": scope.fingerprint,
        "inputSha256": input_sha256, "referenceInputSha256": reference_sha256,
        "claims": dict(CLAIMS), "status": "unknown", "layout": {},
        "retention": {
            "cosmosBackup": unknown("not_observed"),
            "inlineBlob": unknown("not_requested"),
            "scope": "configuration_only_no_backup_or_blob_content_reads",
            "excluded": ["provider_backups", "blob_versions_and_soft_deleted_copies", "library", "memory", "generated_media", "telemetry"],
        },
        "humanReferences": references,
        "cohort": [empty_row(pair) for pair in scope.cohort],
        "summary": {}, "limits": {
            "cohort": MAX_COHORT, "pageItems": PAGE_ITEMS, "pagesPerScan": MAX_PAGES,
            "rowsPerScan": MAX_ROWS, "samplesPerSurface": MAX_SAMPLES,
            "pageBytes": MAX_PAGE_BYTES, "totalBytes": MAX_TOTAL_BYTES,
            "calls": MAX_CALLS, "sourceSeconds": SOURCE_SECONDS, "collectionSeconds": COLLECTION_SECONDS,
        },
        "collection": {}, "reportSha256": "",
    }
    try:
        raw_account = metadata(reader, "account", budget)
        sdk_version = raw_account.get("sdkVersion")
        if sdk_version is not None:
            if not isinstance(sdk_version, str) or not re.fullmatch(r"4\.[0-9]{1,3}\.[0-9]{1,3}", sdk_version):
                raise AssessmentError("unsupported_cosmos_sdk")
            report["source"]["cosmosSdkVersion"] = sdk_version
        elif mode == "live":
            raise AssessmentError("sdk_version_unavailable")
        report["layout"]["account"] = account_layout(raw_account, scope)
        try:
            report["retention"]["cosmosBackup"] = backup_metadata(raw_account)
        except AssessmentError as exc:
            report["retention"]["cosmosBackup"] = unknown(exc.code)
    except AssessmentError as exc:
        report["layout"]["account"] = unknown(exc.code)
    for surface in SURFACES:
        try:
            report["layout"][surface] = container_layout(metadata(reader, surface, budget), surface)
        except AssessmentError as exc:
            report["layout"][surface] = unknown(exc.code)
    if scope.blob_id:
        try:
            report["retention"]["inlineBlob"] = blob_metadata(
                metadata(reader, "blob_service", budget), metadata(reader, "blob_container", budget), scope
            )
        except AssessmentError as exc:
            report["retention"]["inlineBlob"] = unknown(exc.code)
    layout_ok = all(entry["status"] == "compatible" for entry in report["layout"].values())
    for pair, row in zip(scope.cohort, report["cohort"], strict=True):
        if layout_ok:
            assess_pair(reader, scope, pair, budget, observed, row)
        else:
            row["issues"].append("layout_not_compatible")
        row["issues"] = sorted(set(row["issues"]))
    rows = report["cohort"]
    report["summary"] = {
        "declared": len(rows), "inventoryComplete": sum(r["inventoryComplete"] for r in rows),
        "classifications": dict(sorted(Counter(r["classification"] for r in rows).items())),
        "withIssues": sum(bool(r["issues"]) for r in rows),
    }
    report["status"] = "complete" if complete_observations(report) else "unknown"
    report["observedAt"] = utc(observed + timedelta(seconds=budget.clock() - budget.started))
    report["collection"] = {"facadeCalls": budget.calls, "projectedMetadataBytes": budget.received_bytes}
    report["reportSha256"] = digest({k: v for k, v in report.items() if k != "reportSha256"})
    return report


def complete_observations(report: dict[str, Any]) -> bool:
    blob = report["retention"]["inlineBlob"]
    return (
        all(entry["status"] == "compatible" for entry in report["layout"].values())
        and report["retention"]["cosmosBackup"]["status"] == "observed"
        and (blob["status"] == "observed" or blob.get("issue") == "not_requested")
        and all(
            row["inventoryComplete"] and not row["issues"]
            and row["classification"] not in ("malformed", "unavailable")
            and row["parentReadStable"] is True
            and row["uploadTerminalState"] != "unresolved"
            and all(
                row[s]["coverage"] == "complete" and not row[s]["issues"]
                and not row[s]["invalidRows"] and not row[s]["unresolvedUploads"]
                for s in ("messages", "documents")
            )
            for row in report["cohort"]
        )
    )


def validate_report(report: object, *, same_source: bool = True) -> dict[str, Any]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError

    raw = obj(report)
    schema, _ = read_json(ROOT / "scripts" / "conversation-deletion-assessment.schema.json", MAX_REPORT_BYTES)
    try:
        Draft202012Validator(schema).validate(raw)
    except ValidationError:
        raise AssessmentError("invalid_report_contract") from None
    if raw["reportSha256"] != digest({k: v for k, v in raw.items() if k != "reportSha256"}):
        raise AssessmentError("report_digest_mismatch")
    if same_source and raw["source"]["assessorSha256"] != source_fingerprint():
        raise AssessmentError("report_source_mismatch")
    if raw["source"]["querySha256"] != digest([PARENT_SQL, CHILD_SQL]):
        raise AssessmentError("report_query_mismatch")
    if (
        raw["summary"]["declared"] != len(raw["cohort"])
        or len({r["cohortSha256"] for r in raw["cohort"]}) != len(raw["cohort"])
        or raw["summary"]["classifications"] != dict(Counter(r["classification"] for r in raw["cohort"]))
        or raw["summary"]["inventoryComplete"] != sum(r["inventoryComplete"] for r in raw["cohort"])
        or raw["summary"]["withIssues"] != sum(bool(r["issues"]) for r in raw["cohort"])
    ):
        raise AssessmentError("report_coverage_mismatch")
    for row in raw["cohort"]:
        for surface in ("messages", "documents"):
            values = row[surface]
            if values["validRows"] + values["invalidRows"] != values["rowsReceived"]:
                raise AssessmentError("report_coverage_mismatch")
    if (raw["status"] == "complete") != complete_observations(raw):
        raise AssessmentError("report_completeness_mismatch")
    if parse_time(raw["observedAt"]) < parse_time(raw["startedAt"]):
        raise AssessmentError("invalid_report_time_range")
    return raw


def render(report: dict[str, Any]) -> bytes:
    validate_report(report)
    body = canonical(report) + b"\n"
    if len(body) > MAX_REPORT_BYTES:
        raise AssessmentError("report_too_large")
    return body


class FixtureReader:
    """Strict projected-metadata fixtures; never a source of live routing."""

    def __init__(self, scope: Scope, fixture: object):
        self.scope = scope
        self.fixture = obj(fixture)
        exact_fields(self.fixture, {"metadata", "partitions"})
        self.sources = obj(self.fixture["metadata"])
        if self.sources.keys() - {"account", *SURFACES, "blob_service", "blob_container"}:
            raise AssessmentError("invalid_fixture_sources")
        self.partitions = self.fixture["partitions"]
        if not isinstance(self.partitions, list) or len(self.partitions) != len(scope.cohort):
            raise AssessmentError("fixture_cohort_mismatch")
        seen = set()
        for entry in self.partitions:
            entry = obj(entry)
            exact_fields(entry, {"ownerId", "sessionId", *SURFACES})
            pair = Pair(identity(entry["ownerId"]), identity(entry["sessionId"]))
            scope.require_pair(pair)
            if pair in seen:
                raise AssessmentError("fixture_cohort_mismatch")
            seen.add(pair)
            for surface in SURFACES:
                values = entry[surface]
                if not isinstance(values, list) or len(values) > MAX_ROWS:
                    raise AssessmentError("invalid_fixture_rows")
                fields = PARENT_FIELDS if surface == "sessions" else CHILD_FIELDS
                if any(not isinstance(row, dict) or row.keys() - fields for row in values):
                    raise AssessmentError("unexpected_projection_column")

    def metadata(self, source: MetadataSource) -> dict[str, Any]:
        if source not in self.sources:
            raise AssessmentError("metadata_unavailable")
        return obj(self.sources[source])

    def page(self, query: Query, continuation: str | None) -> Page:
        query.validate(self.scope)
        start = 0
        if continuation is not None:
            if not re.fullmatch(r"[0-9]{1,3}", continuation):
                raise AssessmentError("invalid_continuation")
            start = int(continuation)
        entry = next(p for p in self.partitions if p["ownerId"] == query.pair.owner and p["sessionId"] == query.pair.session)
        rows = entry[query.surface]
        end = start + PAGE_ITEMS
        return Page(rows[start:end], str(end) if end < len(rows) else None)
