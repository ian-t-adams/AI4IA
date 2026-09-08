"""Recheck automatic memory at the last await before model delivery."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .preferences import MemoryPreference, MemoryPreferenceUnavailable
from .service import MemoryServiceProtocol

logger = logging.getLogger(__name__)


class MemoryContextGuard:
    def __init__(self, memory: MemoryServiceProtocol | None, user_id: str) -> None:
        self._memory = memory
        self._user_id = user_id
        self._preference: MemoryPreference | None = None
        self._blocked = False
        self._supplied = False
        self.block = ""
        self.on_withheld: Callable[[], None] | None = None

    async def allowed(self) -> bool:
        if self._blocked:
            return False
        reader = getattr(self._memory, "get_preference", None)
        if self._memory is None or not getattr(self._memory, "enabled", False):
            self._blocked = True
            return False
        if reader is None:
            logger.warning("automatic memory withheld: preference reader unavailable")
            self._blocked = True
            return False
        try:
            current = await reader(self._user_id)
        except MemoryPreferenceUnavailable:
            logger.warning("automatic memory withheld: preference unavailable")
            self._blocked = True
            return False
        if not isinstance(current, MemoryPreference):
            raise MemoryPreferenceUnavailable("Invalid memory preference response.")
        if self._preference is None:
            self._preference = current
        if not current.automatic_enabled or current != self._preference:
            self._blocked = True
        return not self._blocked

    async def prepare(self, messages: list[dict[str, Any]]) -> None:
        has_block = bool(self.block) and any(
            message.get("role") == "system" and message.get("content") == self.block
            for message in messages
        )
        if await self.allowed():
            self._supplied = self._supplied or has_block
            return
        if has_block:
            messages[:] = [
                message for message in messages
                if not (
                    message.get("role") == "system"
                    and message.get("content") == self.block
                )
            ]
            if not self._supplied and self.on_withheld is not None:
                self.on_withheld()
        # Only turn-local tool exchanges are changed, never stored chat history.
        recall_ids = {
            call.get("id")
            for message in messages if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if (call.get("function") or {}).get("name") == "recall_memory"
        }
        for message in messages:
            if message.get("role") == "tool" and message.get("tool_call_id") in recall_ids:
                message["content"] = json.dumps({
                    "results": "", "count": 0,
                    "note": "Automatic memory is off or unavailable; recalled context withheld.",
                })
