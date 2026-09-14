"""Shared sentinel, catalog candidates, and application Voice Live setup acknowledgements."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
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
