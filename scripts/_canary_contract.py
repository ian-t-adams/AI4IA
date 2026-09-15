"""Shared sentinel, catalog candidates, cleanup proof, and Voice Live acknowledgements."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# The runtime's reduction module is stdlib-only. Share the exact sentinel with
# dispatch validation even when an operator executes this script by file path.
_API_SOURCE = str(Path(__file__).resolve().parents[1] / "app" / "api" / "src")
if _API_SOURCE not in sys.path:
    sys.path.insert(0, _API_SOURCE)
from ai4ia_api.request_constraints import CANARY_SENTINEL as CANARY_PROMPT  # noqa: E402

CANARY_TITLE = "post-deploy canary"
CANARY_CATEGORY_ORDER = ("chat-fast", "chat", "reasoning")
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SESSION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_DELETION_STATUS_FIELDS = {
    "sessionId", "state", "phase", "requestedAt", "updatedAt", "lastVerifiedAt",
    "messagesVerified", "documentsVerified", "attachmentsVerified", "pendingUploads",
    "pendingUploadsTruncated", "retryReason", "attempts", "scope", "backupsErased",
    "coordinationRetained", "autonomousCleanup",
}
_VERIFICATION_FIELDS = ("messagesVerified", "documentsVerified", "attachmentsVerified")


def _cleanup_timestamp(raw: object, now: datetime) -> datetime | None:
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.utcoffset() != timezone.utc.utcoffset(value) or value > now:
        return None
    return value


def deletion_status_valid(status: object, session_id: str, *, now: datetime) -> bool:
    """Validate the public DeletionStatus, without inventing hidden epoch fields."""
    if not isinstance(status, dict) or set(status) != _DELETION_STATUS_FIELDS:
        return False
    if (
        status["sessionId"] != session_id
        or status["state"] not in ("pending", "retryable", "cleanup_verified")
        or status["phase"] not in (
            "fences", "messages", "documents", "attachments", "uploads", "complete",
        )
        or status["retryReason"] not in (
            None, "storage_unavailable", "cleanup_timeout", "concurrent_change",
            "integrity_mismatch", "uploads_unresolved", "artifact_store_required",
        )
        or status["scope"] != "conversation_content_and_inline_originals"
        or status["backupsErased"] is not False or status["coordinationRetained"] is not True
        or status["autonomousCleanup"] is not False
        or type(status["attempts"]) is not int or not 0 <= status["attempts"] <= 1000
        or any(type(status[key]) is not bool for key in (
            *_VERIFICATION_FIELDS, "pendingUploadsTruncated",
        ))
    ):
        return False
    requested = _cleanup_timestamp(status["requestedAt"], now)
    updated = _cleanup_timestamp(status["updatedAt"], now)
    if requested is None or updated is None or requested > updated:
        return False
    if status["lastVerifiedAt"] is not None:
        verified = _cleanup_timestamp(status["lastVerifiedAt"], now)
        if verified is None or not requested <= verified <= updated:
            return False
    uploads = status["pendingUploads"]
    if not isinstance(uploads, list) or len(uploads) > 25:
        return False
    for upload in uploads:
        if (
            not isinstance(upload, dict) or set(upload) != {"id", "documentId", "startedAt"}
            or any(
                not isinstance(upload[key], str) or not 1 <= len(upload[key]) <= 128
                for key in ("id", "documentId")
            )
            or _cleanup_timestamp(upload["startedAt"], updated) is None
        ):
            return False
    if status["state"] != "cleanup_verified" and (
        status["lastVerifiedAt"] is not None or status["phase"] == "complete"
    ):
        return False
    return True


def verified_cleanup(status: object, session_id: str, *, now: datetime) -> bool:
    """Last recorded scoped v1 proof, not physical erasure or enrollment authority."""
    if not isinstance(status, dict) or not deletion_status_valid(status, session_id, now=now):
        return False
    return (
        status["state"] == "cleanup_verified" and status["phase"] == "complete"
        and status["lastVerifiedAt"] is not None and status["attempts"] >= 1
        and status["pendingUploads"] == [] and status["pendingUploadsTruncated"] is False
        and status["retryReason"] is None
        and all(status[key] is True for key in _VERIFICATION_FIELDS)
    )


def catalog_model_preferences(catalog_doc: Any) -> list[str]:
    catalog = catalog_doc.get("catalog") if isinstance(catalog_doc, dict) else None
    if not isinstance(catalog, list):
        return []
    by_category: dict[str, list[str]] = {}
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        category = entry.get("category")
        if not isinstance(name, str) or not _MODEL_ID_RE.fullmatch(name):
            continue
        if category not in CANARY_CATEGORY_ORDER:
            continue
        if not isinstance(entry.get("deployments"), list) or not entry["deployments"]:
            continue
        by_category.setdefault(category, []).append(name)
    return [
        name for category in CANARY_CATEGORY_ORDER
        for name in sorted(set(by_category.get(category, [])))
    ]


def session_payload(model: str) -> dict[str, Any]:
    return {"title": CANARY_TITLE, "model": model}


def chat_payload(session_id: str, model: str) -> dict[str, Any]:
    return {
        "sessionId": session_id, "content": CANARY_PROMPT, "model": model, "stream": False,
    }


class SetupOrderError(ValueError):
    pass


@dataclass
class SetupState:
    expected_history_item_ids: Sequence[str] = ()
    created: bool = False
    updated: bool = False
    correlation: str | None = None
    received_frames: int = 0
    acknowledged_history_item_ids: set[str] = field(default_factory=set)


def acknowledge_setup(payload: dict[str, Any], state: SetupState) -> bool:
    event_type = payload.get("type")
    if event_type == "session.created":
        if state.updated:
            raise SetupOrderError("session.created arrived after session.updated.")
        state.created = True
    elif event_type == "session.updated":
        if not state.created:
            raise SetupOrderError("session.updated arrived before session.created.")
        state.updated = True
    elif event_type == "conversation.item.created":
        raw_item = payload.get("item")
        item = raw_item if isinstance(raw_item, dict) else {}
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id in state.expected_history_item_ids:
            state.acknowledged_history_item_ids.add(item_id)
    return (
        state.created and state.updated
        and state.acknowledged_history_item_ids == set(state.expected_history_item_ids)
    )
