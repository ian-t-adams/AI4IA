from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from azure.storage.blob import ExponentialRetry
from fastapi.testclient import TestClient

from ai4ia_api.documents.ephemeral_store import EphemeralAttachmentStore, session_prefix
from ai4ia_api.library.blob_store import AzureBlobStore, InMemoryBlobStore
from ai4ia_api.main import create_app
from ai4ia_api.sessions.deletion_service import ConversationDeletionService
from tests.conftest import make_settings


def new_session(client):
    response = client.post("/api/sessions", json={"title": "Private conversation"})
    assert response.status_code == 201
    return response.json()


def finish(client, sid):
    for _ in range(10):
        response = client.post(f"/api/sessions/{sid}/deletion/reconcile")
        assert response.status_code in {200, 202}, response.text
        if response.status_code == 200:
            return response.json()
    pytest.fail("Bounded cleanup did not converge")


def test_accepted_intent_denies_normal_access_and_status_is_owner_scoped():
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        session = new_session(client)
        sid = session["id"]
        assert app.state.inline_attachment_store._session_repo is app.state.session_repo
        turn = {"turns": [{"role": "user", "text": "hello"}]}
        assert client.post(f"/api/sessions/{sid}/voice-turns", json=turn).status_code == 201
        response = client.delete(f"/api/sessions/{sid}")
        assert response.status_code == 202 and response.json()["state"] == "pending"
        assert client.get("/api/sessions").json() == []
        for suffix in ("", "/messages", "/documents"):
            assert client.get(f"/api/sessions/{sid}{suffix}").status_code == 404
        assert client.patch(f"/api/sessions/{sid}", json={"title": "revive"}).status_code == 404
        assert client.post(f"/api/sessions/{sid}/voice-turns", json=turn).status_code == 404
        assert client.post(
            f"/api/sessions/{sid}/tool-consent", json={"enabled": False}
        ).status_code == 404
        before = copy.deepcopy(app.state.session_repo._deletions)
        status = client.get(f"/api/sessions/{sid}/deletion")
        assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
        assert client.get("/api/sessions/deletions").json()["items"][0]["sessionId"] == sid
        assert app.state.session_repo._deletions == before
        headers = {"X-Dev-User": "someone-else"}
        for method, url in (
            ("get", f"/api/sessions/{sid}/deletion"),
            ("delete", f"/api/sessions/{sid}"),
            ("post", f"/api/sessions/{sid}/deletion/reconcile"),
        ):
            assert getattr(client, method)(url, headers=headers).status_code == 404
        assert client.get("/api/sessions/deletions", headers=headers).json()["items"] == []
        result = finish(client, sid)
        assert result["lastVerifiedAt"] and result["coordinationRetained"]
        assert not result["backupsErased"] and not result["autonomousCleanup"]
        assert client.delete(f"/api/sessions/{sid}").status_code == 200


def test_flag_off_keeps_legacy_204_but_enabled_legacy_explicitly_requires_migration():
    app = create_app(make_settings())
    with TestClient(app) as client:
        sid = new_session(client)["id"]
        app.state.settings.session_deletion_enabled = True
        app.state.session_repo._deletion_enabled = True
        response = client.delete(f"/api/sessions/{sid}")
        assert response.status_code == 409 and response.json()["code"] == "migration_required"
        assert client.get(f"/api/sessions/{sid}").status_code == 200
        app.state.settings.session_deletion_enabled = False
        app.state.session_repo._deletion_enabled = False
        response = client.delete(f"/api/sessions/{sid}")
        assert response.status_code == 204 and response.content == b""


def test_pausing_gate_never_uses_legacy_hard_delete_for_v1():
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        sid = new_session(client)["id"]
        app.state.settings.session_deletion_enabled = False
        app.state.session_repo._deletion_enabled = False
        assert client.delete(f"/api/sessions/{sid}").json()["code"] == "deletion_disabled"
        assert client.get(f"/api/sessions/{sid}").status_code == 200


@pytest.mark.parametrize("delete_during_put", [False, True])
def test_delayed_upload_put_and_completion_use_ticket_even_after_tombstone(delete_during_put):
    app = create_app(make_settings(
        session_deletion_enabled=True, inline_document_compute_enabled=True
    ))
    with TestClient(app) as client:
        session = new_session(client)
        sid, uid = session["id"], session["userId"]
        repo = app.state.session_repo

        class DelayedBlob(InMemoryBlobStore):
            observed = None

            async def put(self, path, data, content_type=None, *, single_attempt=False):
                assert single_attempt
                assert len(repo._uploads) == 1
                assert not next(iter(repo._uploads.values())).settled
                if delete_during_put:
                    await repo.begin_deletion(uid, sid)
                    self.observed = await ConversationDeletionService(repo, store).reconcile(uid, sid)
                    assert self.observed.state == "pending"
                    assert self.observed.retryReason == "uploads_unresolved"
                return await super().put(path, data, content_type, single_attempt=single_attempt)

        blob = DelayedBlob()
        store = EphemeralAttachmentStore(blob)
        app.state.inline_attachment_store = store
        response = client.post(
            f"/api/sessions/{sid}/documents",
            files={"file": ("data.csv", b"name,value\na,1\n", "text/csv")},
        )
        assert response.status_code == (404 if delete_during_put else 201)
        assert len(blob._data) == 1
        assert next(iter(repo._uploads.values())).settled
        if not delete_during_put:
            assert len(client.get(f"/api/sessions/{sid}/documents").json()) == 1
            assert client.delete(f"/api/sessions/{sid}").status_code == 202
        else:
            assert repo._documents.get(sid, []) == []
        assert finish(client, sid)["attachmentsVerified"]
        assert blob._data == {}


def test_later_success_cannot_settle_earlier_ambiguous_upload_or_force_complete():
    app = create_app(make_settings(
        session_deletion_enabled=True, inline_document_compute_enabled=True
    ))
    with TestClient(app) as client:
        sid = new_session(client)["id"]

        class AmbiguousBlob(InMemoryBlobStore):
            fail = True

            async def put(self, path, data, content_type=None, *, single_attempt=False):
                result = await super().put(path, data, content_type, single_attempt=single_attempt)
                if self.fail:
                    raise TimeoutError("uncertain PUT outcome")
                return result

        blob = AmbiguousBlob()
        app.state.inline_attachment_store = EphemeralAttachmentStore(blob)
        for failure in (True, False):
            blob.fail = failure
            response = client.post(
                f"/api/sessions/{sid}/documents", files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")}
            )
            assert response.status_code == 201, response.text
        intents = list(app.state.session_repo._uploads.values())
        assert len(intents) == 2 and [intent.settled for intent in intents] == [False, True]
        assert client.delete(f"/api/sessions/{sid}").status_code == 202
        for _ in range(3):
            response = client.post(
                f"/api/sessions/{sid}/deletion/reconcile", json={"forceComplete": True}
            )
            assert response.status_code == 202
            assert response.json()["retryReason"] == "uploads_unresolved"
            assert response.json()["pendingUploads"][0]["id"] == intents[0].id
            assert response.json()["lastVerifiedAt"] is None
        assert blob._data == {}


@pytest.mark.parametrize("enabled", [False, True])
def test_api_only_ticketed_v1_uploads_request_single_attempt(enabled):
    app = create_app(make_settings(
        session_deletion_enabled=enabled, inline_document_compute_enabled=True
    ))
    with TestClient(app) as client:
        sid = new_session(client)["id"]

        class RecordingBlob(InMemoryBlobStore):
            attempts = []

            async def put(self, path, data, content_type=None, *, single_attempt=False):
                self.attempts.append(single_attempt)
                return await super().put(path, data, content_type, single_attempt=single_attempt)

        blob = RecordingBlob()
        app.state.inline_attachment_store = EphemeralAttachmentStore(blob)
        assert client.post(
            f"/api/sessions/{sid}/documents", files={"file": ("a.csv", b"a\n1\n", "text/csv")}
        ).status_code == 201
        assert blob.attempts == [enabled]


async def test_real_storage_retry_policy_consumes_per_call_override_only_for_ticketed_put():
    policy = ExponentialRetry(retry_total=3)
    totals = []

    class Blob:
        async def upload_blob(self, data, **kwargs):
            # Use the installed public Azure Storage policy, not a fake that
            # assumes an arbitrary kwarg changes retries.
            request = SimpleNamespace(
                http_request=SimpleNamespace(body=data),
                context=SimpleNamespace(options=kwargs),
            )
            totals.append(policy.configure_retries(request)["total"])

    service = SimpleNamespace(get_blob_client=lambda **kwargs: Blob())
    store = AzureBlobStore("https://storage.test", "inline", service_client=service)
    await store.put("u1/s1/d1", b"legacy")
    await store.put("u1/s1/d2", b"ticketed", single_attempt=True)
    await store.put("u1/s1/d3", b"legacy")
    assert totals == [3, 0, 3]


@pytest.mark.parametrize("part", ["", "../", "u1/sibling", "u1\\sibling"])
def test_cleanup_prefix_cannot_escape_owner_or_session(part):
    assert session_prefix("u1", "s1") == "u1/s1/"
    with pytest.raises(ValueError):
        session_prefix(part, "s1")
    with pytest.raises(ValueError):
        session_prefix("u1", part)


def test_nonlocal_runtime_gate_requires_cosmos_entra_and_rollout_id():
    base = dict(
        env="dev", session_store="cosmos", cosmos_endpoint="https://cosmos.test",
        auth_provider="entra", entra_tenant_id="tenant", entra_audience="audience",
        model_gateway_url="https://gateway.test/openai", model_gateway_auth_mode="api_key",
        model_gateway_allowed_hosts="gateway.test", model_gateway_api_key_header="S7P-KEY",
        model_gateway_api_key="unit-test-only-key", session_deletion_enabled=True,
        session_deletion_rollout_id="reviewed-cutover",
    )
    make_settings(**base).validate_runtime()
    for changes in (
        {"session_store": "memory"},
        {"auth_provider": "dev"},
        {"session_deletion_rollout_id": ""},
        {"session_deletion_rollout_id": "../"},
    ):
        with pytest.raises(RuntimeError, match="SESSION_DELETION"):
            make_settings(**(base | changes)).validate_runtime()
        make_settings(**(base | changes | {"session_deletion_enabled": False})).validate_runtime()
