"""Synthetic controls for read-only scope, truthful unknowns, and SDK requests."""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import httpx
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import HttpRequest

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import _deletion_assessment as assessment  # noqa: E402
import _deletion_assessment_sdk as sdk  # noqa: E402

cli = load_script("assess_conversation_deletion", ROOT / "scripts" / "assess-conversation-deletion.py")
FIXTURE = ROOT / "scripts" / "fixtures" / "conversation-deletion-assessment.json"
NOW = datetime(2026, 9, 10, 16, tzinfo=UTC)
SECRET = "SYNTHETIC-PRIVATE-CONTENT-DO-NOT-EXPORT"


def fixture() -> dict:
    return assessment.read_json(FIXTURE, assessment.MAX_FIXTURE_BYTES)[0]


class RecordingReader(assessment.FixtureReader):
    def __init__(self, document: dict):
        super().__init__(assessment.Scope.parse(document["scope"]), document["observations"])
        self.calls = []
        self.writes = []
        self.overrides = {}
        self.after_read = None

    def metadata(self, source):
        self.calls.append(("metadata", source))
        if source in self.overrides:
            raise assessment.AssessmentError(self.overrides[source])
        return super().metadata(source)

    def page(self, query, continuation):
        query.validate(self.scope)
        self.calls.append(("page", query.surface, query.pair, continuation))
        key = (query.surface, query.pair.session, continuation)
        if key in self.overrides:
            override = self.overrides[key]
            if isinstance(override, Exception):
                raise override
            return override
        page = copy.deepcopy(super().page(query, continuation))
        if self.after_read is not None:
            self.after_read(query, page)
        return page

    def forbidden(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        raise AssertionError("The read-only assessment invoked a mutation")

    create_item = upsert_item = replace_item = patch_item = delete_item = forbidden
    execute_item_batch = create_database_if_not_exists = delete_blob = forbidden


def observe(reader: RecordingReader, **kwargs):
    return assessment.collect(reader.scope, reader, mode="synthetic", input_sha256="a" * 64, now=NOW, **kwargs)


def set_state(document: dict, state: str) -> None:
    entry = document["observations"]["partitions"][0]
    parent = entry["sessions"][0]
    parent["kind"] = "session_initializing_v1" if state == "initializing" else "session_tombstone_v1"
    if state in ("initializing", "verified"):
        entry["messages"] = entry["messages"][:1]
        entry["documents"] = entry["documents"][:1]
    if state != "initializing":
        parent.update({
            "statusSessionId": parent["id"],
            "deletionState": "cleanup_verified" if state == "verified" else "pending",
            "deletionPhase": "complete" if state == "verified" else "messages",
            "lastVerifiedAt": "2026-09-09T12:00:00Z" if state == "verified" else None,
            "messagesVerified": state == "verified", "documentsVerified": state == "verified",
            "attachmentsVerified": state == "verified",
            "cleanupScope": "conversation_content_and_inline_originals",
            "backupsErased": False, "coordinationRetained": True, "autonomousCleanup": False,
            "pendingUploadCount": 0, "pendingUploadsTruncated": False,
        })
        for surface in ("messages", "documents"):
            entry[surface][0]["closed"] = True


def add_blob(document: dict) -> None:
    blob_id = (
        "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-deletion-fixture"
        "/providers/Microsoft.Storage/storageAccounts/fixturestorage"
        "/blobServices/default/containers/ephemeral-attachments"
    )
    document["scope"]["inlineBlobContainerResourceId"] = blob_id
    metadata = document["observations"]["metadata"]
    metadata["blob_service"] = {
        "id": blob_id.rsplit("/containers/", 1)[0],
        "properties": {
            "isVersioningEnabled": True,
            "deleteRetentionPolicy": {"enabled": True, "days": 7},
            "containerDeleteRetentionPolicy": {"enabled": False},
        },
    }
    metadata["blob_container"] = {
        "id": blob_id, "properties": {"hasLegalHold": False, "hasImmutabilityPolicy": False},
    }
    parent = document["observations"]["partitions"][0]["sessions"][0]
    parent["attachmentStorageRequired"] = True
    parent["attachmentStorageId"] = assessment.Scope.parse(document["scope"]).storage_identity


def add_ticket(document: dict, *, settled: bool) -> None:
    add_blob(document)
    entry = document["observations"]["partitions"][0]
    entry["documents"].append({
        "id": assessment.UPLOAD_PREFIX + "fixture-ticket", "sessionId": entry["sessionId"],
        "userId": entry["ownerId"], "epoch": "fixture-generation-a", "kind": "session_upload_v1",
        "documentId": "fixture-document-a", "startedAt": "2000-01-01T00:00:00Z",
        "settled": settled, "ttl": -1, "_etag": '"fixture-ticket"', "_ts": 1789000000,
    })


class AssessmentTests(unittest.TestCase):
    def assert_complete(self, report):
        self.assertEqual("complete", report["status"], report["cohort"])
        self.assertEqual(2, report["summary"]["declared"])
        self.assertEqual(2, report["summary"]["inventoryComplete"])
        assessment.validate_report(report)

    def test_selected_cohort_reads_all_metadata_and_no_writes_or_neighbors(self):
        document = fixture()
        reader = RecordingReader(document)
        original = copy.deepcopy(document)
        report = observe(reader)
        self.assert_complete(report)
        self.assertEqual(["v1_active", "legacy"], [row["classification"] for row in report["cohort"]])
        self.assertEqual(1, report["cohort"][0]["messages"]["ordinary"])
        self.assertEqual(1, report["cohort"][0]["documents"]["ordinary"])
        self.assertEqual(1, report["cohort"][1]["messages"]["ordinary"])
        self.assertEqual(8, sum(call[0] == "page" for call in reader.calls))
        self.assertEqual(set(reader.scope.cohort), {call[2] for call in reader.calls if call[0] == "page"})
        self.assertEqual([], reader.writes)
        self.assertEqual(original, document)
        for pair in (
            assessment.Pair("fixture-owner-a", "unselected-session"),
            assessment.Pair("unselected-owner", "fixture-session-a"),
        ):
            before = list(reader.calls)
            with self.assertRaisesRegex(assessment.AssessmentError, "out_of_scope"):
                assessment.scan(reader, reader.scope, assessment.Query("sessions", pair), assessment.Budget())
            self.assertEqual(before, reader.calls)
        body = assessment.render(report).decode()
        for value in ("fixture-owner", "fixture-session", "fixture-message", "fixture-generation", "documents.azure.com", "/subscriptions/"):
            self.assertNotIn(value, body)
        self.assertEqual(assessment.CLAIMS, report["claims"])

    def test_all_current_parent_states_are_observations_not_approval(self):
        for state in ("initializing", "deleting", "verified"):
            with self.subTest(state=state):
                document = fixture()
                set_state(document, state)
                reader = RecordingReader(document)
                report = observe(reader)
                self.assert_complete(report)
                row = report["cohort"][0]
                self.assertEqual(state, row["classification"])
                self.assertEqual(state, row["parentObservation"])
                self.assertFalse(report["claims"]["writerDrainProven"])
                self.assertFalse(report["claims"]["backupsErased"])
                self.assertFalse(report["claims"]["cleanupAuthorized"])
                self.assertEqual([], reader.writes)
                if state == "verified":
                    self.assertEqual("2026-09-09T12:00:00Z", row["lastRecordedVerifiedAt"])

    def test_scope_refusals_before_any_facade_construction(self):
        mutations = (
            lambda s: s.update(allOwners=True),
            lambda s: s.update(endpoint="https://evil.invalid"),
            lambda s: s.update(accountResourceId="https://management.azure.com/delegated"),
            lambda s: s.update(database="../other"),
            lambda s: s.update(cohort=[]),
            lambda s: s["cohort"].append(copy.deepcopy(s["cohort"][0])),
            lambda s: s["cohort"][0].update(ownerId="*"),
            lambda s: s["cohort"][0].update(sessionId="*"),
            lambda s: s["cohort"][0].update(ownerId="__ai4ia_deletion_control__"),
            lambda s: s["cohort"][0].update(sessionId="__ai4ia_session_fence_v1__"),
            lambda s: s["cohort"][1].update(sessionId=s["cohort"][0]["sessionId"]),
            lambda s: s.update(inlineBlobContainerResourceId="/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg-deletion-fixture/providers/Microsoft.Storage/storageAccounts/fixturestorage/blobServices/default/containers/ephemeral-attachments"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                scope = fixture()["scope"]
                mutate(scope)
                with self.assertRaises(assessment.AssessmentError):
                    assessment.Scope.parse(scope)
        self.assertEqual(2, len(assessment.Scope.parse(fixture()["scope"]).cohort))

    def test_protocol_constants_serializers_and_target_hash_match_runtime_without_factory(self):
        sys.path.insert(0, str(ROOT / "app" / "api" / "src"))
        from ai4ia_api.documents.ephemeral_store import inline_attachment_storage_id
        from ai4ia_api.sessions import deletion_models
        from ai4ia_api.sessions.cosmos_repo import CosmosSessionRepository
        from ai4ia_api.sessions.models import Session

        self.assertEqual(deletion_models.FENCE_ID, assessment.FENCE_ID)
        self.assertEqual(deletion_models.UPLOAD_ID_PREFIX, assessment.UPLOAD_PREFIX)
        self.assertEqual(deletion_models.PROTOCOL_VERSION, assessment.VERSION)
        session = Session(id="fixture-session", userId="fixture-owner")
        for versioned in (False, True):
            if versioned:
                session.deletionProtocol = 1
                session.deletionEpoch = "fixture-generation"
            body = CosmosSessionRepository._to_doc(session)
            body.update(_etag='"synthetic-etag"', _ts=1789000000)
            projected = CosmosTransportFixture.project(body, assessment.PARENT_SQL)
            _, state = assessment.parent_record(projected, assessment.Pair(session.userId, session.id), NOW)
            self.assertEqual("v1_active" if versioned else "legacy", state)
        document = fixture()
        add_blob(document)
        expected = inline_attachment_storage_id(SimpleNamespace(
            document_blob_account_url="https://fixturestorage.blob.core.windows.net/",
            inline_attachment_blob_container="ephemeral-attachments",
        ))
        self.assertEqual(expected, assessment.Scope.parse(document["scope"]).storage_identity)

    def test_foreign_parent_and_malformed_protocol_never_admit_child_reads(self):
        mutations = (
            ("userId", "neighbor-owner"), ("id", "neighbor-session"), ("kind", "other_kind"),
            ("kind", None), ("deletionProtocol", True), ("deletionEpoch", ""),
            ("ttl", 60), ("ttl", True), ("_etag", None), ("_ts", False),
        )
        self.assert_complete(observe(RecordingReader(fixture())))
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                document = fixture()
                document["observations"]["partitions"][0]["sessions"][0][field] = value
                reader = RecordingReader(document)
                report = observe(reader)
                self.assertEqual("unknown", report["status"])
                self.assertEqual("malformed", report["cohort"][0]["classification"])
                self.assertEqual("legacy", report["cohort"][1]["classification"])
                self.assertFalse(any(c[0] == "page" and c[1] != "sessions" and c[2] == reader.scope.cohort[0] for c in reader.calls))
                self.assertEqual([], reader.writes)

    def test_fence_identity_generation_kind_and_ttl_are_not_optional(self):
        self.assert_complete(observe(RecordingReader(fixture())))
        for field, value in (
            ("userId", "neighbor-owner"), ("sessionId", "neighbor-session"),
            ("epoch", "other-generation"), ("kind", "other_kind"),
            ("closed", "false"), ("ttl", 60), ("ttl", False),
            ("id", "ordinary-id"), ("_etag", ""),
        ):
            with self.subTest(field=field, value=value):
                document = fixture()
                document["observations"]["partitions"][0]["messages"][0][field] = value
                reader = RecordingReader(document)
                report = observe(reader)
                self.assertEqual("unknown", report["status"])
                self.assertEqual("malformed", report["cohort"][0]["classification"])
                self.assertEqual(2, report["cohort"][0]["messages"]["rowsReceived"])
                self.assertEqual(1, report["cohort"][0]["messages"]["invalidRows"])
                self.assertEqual("not_read", report["cohort"][0]["documents"]["coverage"])
                self.assertTrue(report["cohort"][1]["inventoryComplete"])
                self.assertEqual([], reader.writes)

    def test_ordinary_records_also_require_owner_and_no_hidden_protocol(self):
        for field, value in (("userId", "neighbor-owner"), ("deletionEpoch", "other"), ("kind", None), ("ttl", 1)):
            document = fixture()
            document["observations"]["partitions"][0]["messages"][1][field] = value
            report = observe(RecordingReader(document))
            self.assertEqual("malformed", report["cohort"][0]["classification"])
        self.assert_complete(observe(RecordingReader(fixture())))

    def test_layout_failure_keeps_whole_cohort_and_reads_no_data(self):
        changes = (
            lambda m: m["account"]["properties"].update(enableMultipleWriteLocations=True),
            lambda m: m["account"]["properties"]["writeLocations"].append({"locationName": "East US"}),
            lambda m: m["account"]["properties"]["consistencyPolicy"].update(defaultConsistencyLevel="Eventual"),
            lambda m: m["messages"]["partitionKey"].update(paths=["/userId"]),
            lambda m: m["documents"].pop("partitionKey"),
            lambda m: m["sessions"].update(defaultTtl=60),
            lambda m: m["documents"].update(analyticalStorageTtl=-1),
            lambda m: m["documents"].update(analyticalStorageTtl=False),
            lambda m: m["account"].update(id="/unrelated-account"),
        )
        self.assert_complete(observe(RecordingReader(fixture())))
        for change in changes:
            document = fixture()
            change(document["observations"]["metadata"])
            reader = RecordingReader(document)
            report = observe(reader)
            self.assertEqual("unknown", report["status"])
            self.assertEqual({"unavailable": 2}, report["summary"]["classifications"])
            self.assertFalse(any(c[0] == "page" for c in reader.calls))
            self.assertEqual([], reader.writes)
        reader = RecordingReader(fixture())
        reader.overrides["messages"] = "source_not_found"
        report = observe(reader)
        self.assertEqual("unknown", report["status"])
        self.assertEqual("source_not_found", report["layout"]["messages"]["issue"])
        self.assertEqual(2, report["summary"]["declared"])

    def test_unsettled_ticket_never_expires_or_becomes_clear_from_empty_children(self):
        for settled in (True, False):
            document = fixture()
            add_ticket(document, settled=settled)
            reader = RecordingReader(document)
            report = observe(reader)
            if settled:
                self.assert_complete(report)
            else:
                self.assertEqual("unknown", report["status"])
                self.assertEqual("unresolved", report["cohort"][0]["uploadTerminalState"])
                self.assertEqual(1, report["cohort"][0]["documents"]["unresolvedUploads"])
                self.assertIn("uploads_unresolved", report["cohort"][0]["issues"])
            self.assertEqual([], reader.writes)
            self.assertFalse(any("blob" in c[0] for c in reader.calls))
            self.assertEqual("matched", report["cohort"][0]["attachmentTargetBinding"])
        document = fixture()
        set_state(document, "deleting")
        document["observations"]["partitions"][0]["sessions"][0]["pendingUploadCount"] = 1
        report = observe(RecordingReader(document))
        self.assertEqual("unresolved", report["cohort"][0]["uploadTerminalState"])
        self.assertIn("recorded_uploads_unresolved", report["cohort"][0]["issues"])

    def test_upload_and_retention_target_mismatches_stay_unknown(self):
        document = fixture()
        add_ticket(document, settled=True)
        self.assert_complete(observe(RecordingReader(document)))
        for field, value in (("epoch", "other"), ("userId", "other"), ("settled", None), ("ttl", 1), ("kind", "other")):
            changed = copy.deepcopy(document)
            changed["observations"]["partitions"][0]["documents"][-1][field] = value
            report = observe(RecordingReader(changed))
            self.assertEqual("malformed", report["cohort"][0]["classification"])
        changed = copy.deepcopy(document)
        changed["observations"]["partitions"][0]["sessions"][0]["attachmentStorageId"] = "azure:" + "b" * 64
        report = observe(RecordingReader(changed))
        self.assertEqual("unknown", report["status"])
        self.assertEqual("unknown", report["cohort"][0]["attachmentTargetBinding"])

    def test_unresolved_samples_are_not_hidden_behind_ordinary_document_sample_limit(self):
        document = fixture()
        add_ticket(document, settled=False)
        rows = document["observations"]["partitions"][0]["documents"]
        rows[1:1] = [dict(rows[1], id=f"ordinary-document-{i}") for i in range(20)]
        report = observe(RecordingReader(document))
        samples = report["cohort"][0]["documents"]["samples"]
        self.assertEqual(8, len(samples))
        self.assertEqual("fence", samples[0]["kind"])
        self.assertEqual("upload", samples[1]["kind"])
        self.assertFalse(samples[1]["settled"])
        self.assertEqual("2000-01-01T00:00:00Z", samples[1]["uploadStartedAt"])
        self.assertRegex(samples[1]["documentIdentitySha256"], r"^[a-f0-9]{64}$")
        self.assertEqual("unknown", report["status"])

    def test_retention_unknown_is_not_an_erasure_promise(self):
        document = fixture()
        add_blob(document)
        self.assert_complete(observe(RecordingReader(document)))
        for source, key in (("blob_service", "isVersioningEnabled"), ("blob_container", "hasLegalHold")):
            changed = copy.deepcopy(document)
            changed["observations"]["metadata"][source]["properties"].pop(key)
            report = observe(RecordingReader(changed))
            self.assertEqual("unknown", report["retention"]["inlineBlob"]["status"])
            self.assertEqual("unknown", report["status"])
        changed = copy.deepcopy(document)
        changed["observations"]["metadata"]["account"]["properties"].pop("backupPolicy")
        report = observe(RecordingReader(changed))
        self.assertEqual("unknown", report["retention"]["cosmosBackup"]["status"])
        self.assertEqual("unknown", report["status"])
        self.assertFalse(report["claims"]["backupsErased"])

    def test_missing_parent_and_missing_fence_are_never_repaired_or_completed_deletes(self):
        self.assert_complete(observe(RecordingReader(fixture())))
        for surface in ("sessions", "messages"):
            document = fixture()
            document["observations"]["partitions"][0][surface] = []
            reader = RecordingReader(document)
            report = observe(reader)
            self.assertEqual("unknown", report["status"])
            self.assertEqual([], reader.writes)
            self.assertEqual(2, report["summary"]["declared"])
            self.assertTrue(report["cohort"][1]["inventoryComplete"])
            if surface == "sessions":
                self.assertEqual("unavailable", report["cohort"][0]["classification"])
                self.assertIn("parent_not_observed", report["cohort"][0]["issues"])
            else:
                self.assertEqual("incomplete_or_conflicting", report["cohort"][0]["fenceAgreement"])

    def test_parent_change_during_scan_preserves_previous_observation_without_absence_inference(self):
        reader = RecordingReader(fixture())
        parent_reads = 0

        def remove_after_first(query, _page):
            nonlocal parent_reads
            if query.surface == "sessions" and query.pair == reader.scope.cohort[0]:
                parent_reads += 1
                if parent_reads == 1:
                    reader.partitions[0]["sessions"] = []

        reader.after_read = remove_after_first
        report = observe(reader)
        self.assertEqual("v1_active", report["cohort"][0]["parentObservation"])
        self.assertFalse(report["cohort"][0]["inventoryComplete"])
        self.assertIn("parent_changed_or_unavailable", report["cohort"][0]["issues"])
        self.assertEqual(1, report["cohort"][0]["messages"]["ordinary"])
        self.assertEqual("unknown", report["status"])
        self.assert_complete(observe(RecordingReader(fixture())))

    def test_verified_parent_conflicting_children_is_not_current_cleanup_proof(self):
        document = fixture()
        set_state(document, "verified")
        self.assert_complete(observe(RecordingReader(document)))
        document["observations"]["partitions"][0]["messages"].append(fixture()["observations"]["partitions"][0]["messages"][1])
        report = observe(RecordingReader(document))
        self.assertEqual("verified", report["cohort"][0]["parentObservation"])
        self.assertEqual("malformed", report["cohort"][0]["classification"])
        self.assertEqual("unknown", report["status"])

    def test_all_pages_and_rows_are_counted_but_only_bounded_identity_samples_escape(self):
        document = fixture()
        entry = document["observations"]["partitions"][0]
        template = entry["messages"][1]
        entry["messages"] = [entry["messages"][0]] + [dict(template, id=f"message-{i}") for i in range(99)]
        reader = RecordingReader(document)
        report = observe(reader)
        self.assert_complete(report)
        surface = report["cohort"][0]["messages"]
        self.assertEqual(4, surface["pages"])
        self.assertEqual(100, surface["rowsReceived"])
        self.assertEqual(99, surface["ordinary"])
        self.assertEqual(8, len(surface["samples"]))
        self.assertTrue(surface["samplesTruncated"])
        changed = copy.deepcopy(document)
        changed["observations"]["partitions"][0]["messages"][-1]["_etag"] = '"changed-last-row"'
        later = observe(RecordingReader(changed))
        self.assertEqual(surface["samples"], later["cohort"][0]["messages"]["samples"])
        self.assertNotEqual(surface["metadataChainSha256"], later["cohort"][0]["messages"]["metadataChainSha256"])
        reader = RecordingReader(document)
        reader.overrides[("messages", reader.scope.cohort[0].session, "75")] = assessment.Page(entry["messages"][75:], "100")
        partial = observe(reader)
        self.assertEqual("unknown", partial["status"])
        self.assertEqual("partial", partial["cohort"][0]["messages"]["coverage"])
        self.assertEqual(100, partial["cohort"][0]["messages"]["rowsReceived"])
        self.assertIn("page_limit", partial["cohort"][0]["messages"]["issues"])
        self.assertEqual(2, partial["summary"]["declared"])

    def test_partial_continuation_timeout_duplicate_and_oversized_pages_fail_closed(self):
        self.assert_complete(observe(RecordingReader(fixture())))
        for failure in ("source_timeout", "source_read_failed"):
            reader = RecordingReader(fixture())
            pair = reader.scope.cohort[0]
            rows = reader.partitions[0]["messages"]
            reader.overrides[("messages", pair.session, None)] = assessment.Page(rows, "next")
            reader.overrides[("messages", pair.session, "next")] = assessment.AssessmentError(failure)
            report = observe(reader)
            self.assertEqual("unknown", report["status"])
            self.assertEqual(2, report["cohort"][0]["messages"]["rowsReceived"])
            self.assertEqual(1, report["cohort"][0]["messages"]["ordinary"])
            self.assertIn(failure, report["cohort"][0]["messages"]["issues"])
            self.assertTrue(report["cohort"][1]["inventoryComplete"])
        reader = RecordingReader(fixture())
        pair = reader.scope.cohort[0]
        reader.overrides[("messages", pair.session, None)] = assessment.Page([], "same")
        reader.overrides[("messages", pair.session, "same")] = assessment.Page([], "same")
        self.assertIn("repeated_continuation", observe(reader)["cohort"][0]["messages"]["issues"])
        for page, code in (
            (assessment.Page([{}] * 26, None), "row_limit"),
            (assessment.Page([{"_etag": "x" * assessment.MAX_PAGE_BYTES}], None), "response_byte_limit"),
            (assessment.Page([], "x" * (assessment.MAX_CURSOR_BYTES + 1)), "invalid_continuation"),
        ):
            reader = RecordingReader(fixture())
            reader.overrides[("messages", pair.session, None)] = page
            report = observe(reader)
            self.assertIn(code, report["cohort"][0]["messages"]["issues"])
            self.assertEqual("unknown", report["status"])

    def test_global_call_byte_and_time_budgets_keep_denominator(self):
        self.assert_complete(observe(RecordingReader(fixture())))
        for budget in (
            assessment.Budget(calls=assessment.MAX_CALLS),
            assessment.Budget(received_bytes=assessment.MAX_TOTAL_BYTES),
        ):
            report = observe(RecordingReader(fixture()), budget=budget)
            self.assertEqual("unknown", report["status"])
            self.assertEqual(2, report["summary"]["declared"])
        clock = [0.0]
        budget = assessment.Budget(clock=lambda: clock[0])
        clock[0] = assessment.COLLECTION_SECONDS
        reader = RecordingReader(fixture())
        report = observe(reader, budget=budget)
        self.assertEqual("unknown", report["status"])
        self.assertEqual([], reader.calls)
        self.assertEqual(2, report["summary"]["declared"])

    def test_report_rejects_modified_identity_version_claims_digest_and_coverage(self):
        report = observe(RecordingReader(fixture()))
        self.assert_complete(report)
        changes = (
            lambda r: r.update(schemaVersion=2),
            lambda r: r["claims"].update(cleanupAuthorized=True),
            lambda r: r["cohort"][0].update(sessionId=SECRET),
            lambda r: r["source"].update(assessorSha256="b" * 64),
            lambda r: r["cohort"].pop(),
            lambda r: r.update(reportSha256="b" * 64),
        )
        for change in changes:
            changed = copy.deepcopy(report)
            change(changed)
            with self.assertRaises(assessment.AssessmentError):
                assessment.validate_report(changed)
        changed = copy.deepcopy(report)
        changed["summary"]["declared"] = 1
        changed["reportSha256"] = assessment.digest({k: v for k, v in changed.items() if k != "reportSha256"})
        with self.assertRaisesRegex(assessment.AssessmentError, "report_coverage_mismatch"):
            assessment.validate_report(changed)
        reader = RecordingReader(fixture())
        reader.overrides["messages"] = "source_read_failed"
        changed = observe(reader)
        changed["status"] = "complete"
        changed["reportSha256"] = assessment.digest({k: v for k, v in changed.items() if k != "reportSha256"})
        with self.assertRaisesRegex(assessment.AssessmentError, "report_completeness_mismatch"):
            assessment.validate_report(changed)

    def test_human_references_are_hashed_unverified_and_do_not_clear_unknown(self):
        reader = RecordingReader(fixture())
        raw = {
            "schemaVersion": 1, "accountResourceId": reader.scope.account_id, "database": reader.scope.database,
            "observedAt": "2026-09-10T15:00:00Z",
            "references": [{"kind": "writer_fleet", "reference": "https://example.invalid/private/fleet"}],
        }
        references = assessment.reference_evidence(raw, reader.scope, NOW)
        self.assertEqual("human_reference_not_verified", references["basis"])
        self.assertTrue(references["freshWithin24Hours"])
        self.assertNotIn("example.invalid", json.dumps(references))
        reader.overrides["messages"] = "source_read_failed"
        report = observe(reader, references=references, reference_sha256="c" * 64)
        self.assertEqual("unknown", report["status"])
        self.assertFalse(report["claims"]["writerDrainProven"])
        for url in ("https://a:b@example.invalid/path", "https://example.invalid/path?secret=1", "file:///private", "http://example.invalid"):
            invalid = copy.deepcopy(raw)
            invalid["references"][0]["reference"] = url
            with self.assertRaises(assessment.AssessmentError):
                assessment.reference_evidence(invalid, reader.scope, NOW)

    def test_help_rehearsal_and_report_check_are_offline_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with patch.object(sdk, "IsolatedReader", side_effect=AssertionError("Azure reader constructed")):
                # Run with a text wrapper supporting .buffer, like actual stdout.
                buffer = io.BytesIO()
                stdout = io.TextIOWrapper(buffer, encoding="utf-8", write_through=True)
                with redirect_stdout(stdout):
                    self.assertEqual(0, cli.main(["rehearse", "--fixture", str(FIXTURE), "--output", str(output)]))
                    original = output.read_bytes()
                    self.assertEqual(0, cli.main(["check", "--report", str(output)]))
                    self.assertEqual(2, cli.main(["rehearse", "--fixture", str(FIXTURE), "--output", str(output)]))
                self.assertEqual(original, output.read_bytes())
            with patch.object(sdk, "IsolatedReader", side_effect=AssertionError("Azure reader constructed")):
                cohort = Path(directory) / "cohort.json"
                cohort.write_text(json.dumps(fixture()["scope"]), encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(2, cli.main(["collect", "--cohort", str(cohort), "--output", str(output)]))
            for args in (["--help"], ["collect", "--help"], ["rehearse", "--help"]):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "assess-conversation-deletion.py"), *args],
                    capture_output=True, timeout=15,
                )
                self.assertEqual(0, result.returncode)
                self.assertNotIn(b"DefaultAzureCredential", result.stderr)

    def test_no_apply_or_arbitrary_argument_echo(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "assess-conversation-deletion.py"), "collect", "--apply", SECRET],
            capture_output=True, timeout=15,
        )
        self.assertEqual(2, result.returncode)
        self.assertNotIn(SECRET.encode(), result.stdout + result.stderr)

    def test_fresh_help_and_rehearsal_cannot_import_any_azure_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fresh-report.json"
            script = ROOT / "scripts" / "assess-conversation-deletion.py"
            for arguments in (["--help"], ["rehearse", "--fixture", str(FIXTURE), "--output", str(output)]):
                code = (
                    "import sys, runpy;"
                    f"sys.path.insert(0, {str(script.parent)!r});"
                    "sys.modules['azure'] = None; sys.modules['httpx'] = None;"
                    f"sys.argv = {[str(script), *arguments]!r};"
                    f"runpy.run_path({str(script)!r}, run_name='__main__')"
                )
                result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=15)
                self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(output.exists())

    def test_strict_bounded_inputs_and_fixture_scope_do_not_get_normalized_to_success(self):
        for body in (b'{"a":1,"a":2}', b'{"a":NaN}', b"not-json"):
            with self.assertRaises(assessment.AssessmentError):
                assessment.strict_json(body)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.json"
            path.write_bytes(b" " * (assessment.MAX_INPUT_BYTES + 1))
            with self.assertRaisesRegex(assessment.AssessmentError, "input_too_large"):
                assessment.read_json(path, assessment.MAX_INPUT_BYTES)
            self.assertEqual({"a": 1}, assessment.strict_json(b'{"a":1}'))
        document = fixture()
        document["observations"]["partitions"][0]["sessions"][0]["content"] = SECRET
        with self.assertRaisesRegex(assessment.AssessmentError, "unexpected_projection_column"):
            RecordingReader(document)
        document = fixture()
        document["observations"]["partitions"][0]["ownerId"] = "foreign"
        with self.assertRaisesRegex(assessment.AssessmentError, "out_of_scope"):
            RecordingReader(document)


class Credential:
    def __init__(self):
        self.calls = []

    def get_token(self, *scopes, **_kwargs):
        self.calls.append(scopes)
        return AccessToken("synthetic-not-a-credential", 4102444800)


class CosmosTransportFixture:
    """Interpret the actual SDK SQL projection over synthetic full records."""

    def __init__(self):
        self.document = fixture()
        self.scope = assessment.Scope.parse(self.document["scope"])
        self.requests = []
        self.outbound = []
        self.fail = None
        self.pages = {}
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.credential = Credential()
        self.reader = sdk.SdkReader(self.scope, self.credential, sdk.Wire(self.client))
        self.records = {}
        for entry in self.document["observations"]["partitions"]:
            for surface in assessment.SURFACES:
                self.records[(surface, entry["sessionId"])] = [
                    self.full_record(row, parent=surface == "sessions") for row in entry[surface]
                ]

    @staticmethod
    def full_record(projected, *, parent):
        row = copy.deepcopy(projected)
        row.update(content=SECRET, title=SECRET, instructions=SECRET, grants=[SECRET], rawRef=SECRET)
        if parent:
            for column in assessment.PARENT_COLUMNS:
                if " AS " not in column:
                    continue
                expression, name = column.split(" AS ")
                if name in row:
                    value = row.pop(name)
                    if expression.startswith("ARRAY_LENGTH"):
                        row.setdefault("status", {})["pendingUploads"] = [{}] * value
                    else:
                        row.setdefault("status", {})[expression.rsplit(".", 1)[1]] = value
            row.setdefault("status", {})["unprojectedFixtureContent"] = SECRET
        return row

    @staticmethod
    def project(row, sql):
        selected = sql.removeprefix("SELECT ").split(" FROM c WHERE ", 1)[0]
        if selected == "*":
            return copy.deepcopy(row)
        output = {}
        for column in selected.split(", "):
            expression, _, alias = column.partition(" AS ")
            value = row
            path = expression
            array_length = expression.startswith("ARRAY_LENGTH(")
            if array_length:
                path = expression[len("ARRAY_LENGTH("):-1]
            for part in path.removeprefix("c.").split("."):
                if not isinstance(value, dict) or part not in value:
                    break
                value = value[part]
            else:
                output[alias or path.removeprefix("c.")] = len(value) if array_length else value
        return output

    def handle(self, request):
        self.requests.append(request)
        if self.fail is not None:
            return self.fail(request)
        if request.url.host == "management.azure.com":
            self.assert_get(request)
            self.outbound.append("account")
            return self.response(self.document["observations"]["metadata"]["account"])
        if request.url.path == "/":
            self.assert_get(request)
            return self.response({
                "id": "fixture", "_rid": "ZGF0YQ==",
                "writableLocations": [{"name": "West US", "databaseAccountEndpoint": self.scope.endpoint + "/"}],
                "readableLocations": [{"name": "West US", "databaseAccountEndpoint": self.scope.endpoint + "/"}],
                "userConsistencyPolicy": {"defaultConsistencyLevel": "Session"},
                "enableMultipleWriteLocations": False,
            })
        pieces = request.url.path.rstrip("/").split("/")
        surface = pieces[4]
        if request.method == "GET":
            return self.response({
                **self.document["observations"]["metadata"][surface],
                "_rid": "ZGF0YWJhc2U=", "_self": f"dbs/db/colls/{surface}/",
                "_etag": '"collection"', "_ts": 1789000000,
            })
        payload = json.loads(request.content)
        parameters = {p["name"]: p["value"] for p in payload["parameters"]}
        session = parameters.get("@id", parameters.get("@session"))
        page_key = (surface, session, request.headers.get("x-ms-continuation"))
        if page_key in self.pages:
            return self.pages[page_key](request)
        rows = self.records.get((surface, session), [])
        if surface == "sessions":
            rows = [r for r in rows if r["id"] == parameters["@id"] and r["userId"] == parameters["@owner"]]
        else:
            rows = [r for r in rows if r["sessionId"] == session]
        projected = [self.project(row, payload["query"]) for row in rows]
        self.outbound.append((surface, session, copy.deepcopy(projected)))
        return self.response({"Documents": projected, "_count": len(projected), "_rid": "ZGF0YQ=="}, {"x-ms-session-token": "0:-1#1"})

    @staticmethod
    def assert_get(request):
        if request.method != "GET":
            raise AssertionError("Management mutation")

    @staticmethod
    def response(value, headers=None):
        return httpx.Response(
            200, headers={"content-type": "application/json", **(headers or {})},
            stream=httpx.ByteStream(json.dumps(value).encode()),
        )

    def close(self):
        self.reader.close()
        self.client.close()


class SdkTransportTests(unittest.TestCase):
    def test_real_sdk_serializes_selected_partition_and_only_scalar_projection(self):
        server = CosmosTransportFixture()
        self.addCleanup(server.close)
        report = assessment.collect(server.scope, server.reader, mode="synthetic", input_sha256="d" * 64, now=NOW)
        self.assertEqual("complete", report["status"], report)
        posts = [r for r in server.requests if r.method == "POST"]
        self.assertEqual(8, len(posts))
        for request in posts:
            body = json.loads(request.content)
            partition = json.loads(request.headers["x-ms-documentdb-partitionkey"])
            surface = request.url.path.split("/")[4]
            params = {p["name"]: p["value"] for p in body["parameters"]}
            expected = params["@owner"] if surface == "sessions" else params["@session"]
            self.assertEqual([expected], partition)
            self.assertIn(expected, {p.owner if surface == "sessions" else p.session for p in server.scope.cohort})
            self.assertEqual("25", request.headers["x-ms-max-item-count"])
            self.assertNotEqual("true", request.headers.get("x-ms-documentdb-query-enablecrosspartition"))
            self.assertNotIn("SELECT *", body["query"])
            for excluded in ("c.content", "c.title", "c.instructions", "c.grants", "c.rawRef", "c.status,"):
                self.assertNotIn(excluded, body["query"])
        returned = [p[2] for p in server.outbound if isinstance(p, tuple)]
        self.assertNotIn(SECRET, json.dumps(returned))
        self.assertNotIn(SECRET, assessment.render(report).decode())
        self.assertEqual(1, report["cohort"][0]["messages"]["ordinary"])
        self.assertTrue(server.credential.calls)

    def test_real_sdk_empty_and_nonempty_continuations_are_request_scoped(self):
        server = CosmosTransportFixture()
        self.addCleanup(server.close)
        pair = server.scope.cohort[0]
        first = server.document["observations"]["partitions"][0]["messages"][0]
        second = server.document["observations"]["partitions"][0]["messages"][1]
        server.pages = {
            ("messages", pair.session, None): lambda _: server.response(
                {"Documents": [first], "_count": 1}, {"x-ms-continuation": "opaque-first"}
            ),
            ("messages", pair.session, "opaque-first"): lambda _: server.response(
                {"Documents": [], "_count": 0}, {"x-ms-continuation": "opaque-second"}
            ),
            ("messages", pair.session, "opaque-second"): lambda _: server.response(
                {"Documents": [second], "_count": 1}
            ),
        }
        report = assessment.collect(server.scope, server.reader, mode="synthetic", input_sha256="d" * 64, now=NOW)
        self.assertEqual("complete", report["status"], report["cohort"][0]["issues"])
        self.assertEqual(3, report["cohort"][0]["messages"]["pages"])
        self.assertEqual(2, report["cohort"][0]["messages"]["rowsReceived"])
        posts = [r for r in server.requests if r.method == "POST" and "/messages/" in r.url.path]
        selected_posts = [r for r in posts if json.loads(r.headers["x-ms-documentdb-partitionkey"]) == [pair.session]]
        self.assertEqual([None, "opaque-first", "opaque-second"], [r.headers.get("x-ms-continuation") for r in selected_posts])
        self.assertNotIn("opaque-", assessment.render(report).decode())

    def test_data_plane_topology_disagreement_and_bad_page_counts_fail_closed(self):
        server = CosmosTransportFixture()
        self.addCleanup(server.close)
        base_handler = server.handle

        def incompatible(request):
            if request.url.host != "management.azure.com" and request.url.path == "/":
                server.requests.append(request)
                return server.response({
                    "writableLocations": [{}, {}], "enableMultipleWriteLocations": True,
                    "userConsistencyPolicy": {"defaultConsistencyLevel": "Session"},
                })
            return base_handler(request)

        server.client.close()
        server.client = httpx.Client(transport=httpx.MockTransport(incompatible))
        server.reader.wire.client = server.client
        report = assessment.collect(server.scope, server.reader, mode="synthetic", input_sha256="d" * 64, now=NOW)
        self.assertEqual("unknown", report["status"])
        self.assertFalse(any(r.method == "POST" for r in server.requests))
        control = CosmosTransportFixture()
        self.addCleanup(control.close)
        self.assertEqual("complete", assessment.collect(control.scope, control.reader, mode="synthetic", input_sha256="d" * 64, now=NOW)["status"])
        for count in (True, 2, None):
            other = CosmosTransportFixture()
            self.addCleanup(other.close)
            pair = other.scope.cohort[0]
            other.pages[("sessions", pair.session, None)] = lambda _, count=count: other.response({"Documents": [], "_count": count})
            report = assessment.collect(other.scope, other.reader, mode="synthetic", input_sha256="d" * 64, now=NOW)
            self.assertEqual("unknown", report["status"])
            self.assertIn("invalid_query_count", report["cohort"][0]["issues"])

    def test_transport_refuses_foreign_scope_point_reads_writes_projection_and_partition(self):
        server = CosmosTransportFixture()
        self.addCleanup(server.close)
        server.reader.metadata("account")
        pair = server.scope.cohort[0]
        query = assessment.Query("sessions", pair)
        self.assertEqual(1, len(server.reader.page(query, None).rows))
        base = server.scope.endpoint + "/dbs/ai4ia/colls/sessions/docs"
        valid_headers = {
            "x-ms-version": sdk.COSMOS_DATA_API, "x-ms-documentdb-isquery": "true",
            "x-ms-documentdb-partitionkey": json.dumps([pair.owner]),
            "x-ms-max-item-count": "25",
        }
        valid_body = assessment.canonical({"query": query.sql, "parameters": query.parameters})
        variants = [
            ("POST", base, {**valid_headers, "x-ms-documentdb-partitionkey": '["foreign-owner"]'}, valid_body),
            ("POST", base, {**valid_headers, "x-ms-documentdb-query-enablecrosspartition": "true"}, valid_body),
            ("POST", base, valid_headers, assessment.canonical({"query": "SELECT * FROM c WHERE c.id = @id AND c.userId = @owner", "parameters": query.parameters})),
            ("POST", base, {**valid_headers, "x-ms-max-item-count": "-1"}, valid_body),
            ("POST", base, {**valid_headers, "x-ms-continuation": "caller-supplied"}, valid_body),
            ("POST", base.replace(".documents.azure.com", ".evil.invalid"), valid_headers, valid_body),
            ("GET", base + "/fixture-session-a", valid_headers, None),
            ("DELETE", base, valid_headers, None),
            ("PATCH", base, valid_headers, b"{}"),
            ("PUT", base, valid_headers, b"{}"),
            ("POST", base + "/../other", valid_headers, valid_body),
        ]
        for method, url, headers, body in variants:
            with self.subTest(method=method, url=url, headers=headers):
                before = len(server.requests)
                transport = server.reader.transport
                transport.expected, transport.continuation, transport.posts = query, None, 0
                with self.assertRaises(assessment.AssessmentError):
                    transport.send(HttpRequest(method, url, headers=headers, data=body))
                self.assertEqual(before, len(server.requests))
        before = len(server.requests)
        with self.assertRaisesRegex(assessment.AssessmentError, "out_of_scope"):
            server.reader.page(assessment.Query("sessions", assessment.Pair("neighbor", "unselected")), None)
        self.assertEqual(before, len(server.requests))

    def test_wire_byte_time_redirect_and_encoding_limits_precede_json_parsing(self):
        for kind in ("bytes", "redirect", "encoding", "timeout"):
            def respond(request):
                if kind == "bytes":
                    return httpx.Response(200, stream=httpx.ByteStream(b"x" * (assessment.MAX_PAGE_BYTES + 1)))
                if kind == "redirect":
                    return httpx.Response(302, headers={"location": "https://evil.invalid"}, stream=httpx.ByteStream(b""))
                if kind == "encoding":
                    return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not-gzip"))
                raise httpx.ReadTimeout(SECRET)

            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                wire = sdk.Wire(client)
                with self.assertRaises(assessment.AssessmentError) as error:
                    wire.request("GET", "https://example.invalid/fixture", {})
                self.assertNotIn(SECRET, str(error.exception))
                self.assertEqual(1, wire.calls)
        with httpx.Client(transport=httpx.MockTransport(lambda r: CosmosTransportFixture.response({"ok": True}))) as client:
            wire = sdk.Wire(client)
            self.assertEqual({"ok": True}, json.loads(wire.request("GET", "https://example.invalid/fixture", {})[0]))

    def test_isolated_timeout_terminates_only_its_worker_and_retains_prior_rows(self):
        class Process:
            def __init__(self):
                self.alive = True
                self.terminated = 0

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.terminated += 1
                self.alive = False

            def join(self, timeout):
                pass

        class Pipe:
            def __init__(self, available):
                self.available = available
                self.requests = []

            def send_bytes(self, body):
                self.requests.append(json.loads(body))

            def poll(self, timeout):
                return self.available

            def recv_bytes(self, maximum):
                return b'{"result":{"rows":[],"continuation":null}}'

        scope = assessment.Scope.parse(fixture()["scope"])
        for available in (True, False):
            reader = sdk.IsolatedReader(scope)
            reader.connection.close()
            reader.child.close()
            reader.connection = Pipe(available)
            reader.process = Process()
            if available:
                self.assertEqual([], reader.page(assessment.Query("messages", scope.cohort[0]), None).rows)
                self.assertEqual(0, reader.process.terminated)
            else:
                with self.assertRaisesRegex(assessment.AssessmentError, "source_timeout"):
                    reader.page(assessment.Query("messages", scope.cohort[0]), None)
                self.assertEqual(1, reader.process.terminated)
                with self.assertRaisesRegex(assessment.AssessmentError, "reader_unavailable"):
                    reader.page(assessment.Query("messages", scope.cohort[1]), None)
                self.assertEqual(1, len(reader.connection.requests))


if __name__ == "__main__":
    unittest.main()
