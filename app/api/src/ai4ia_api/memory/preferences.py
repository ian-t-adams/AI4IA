"""Per-user automatic memory policy, independent of tool consent and deletion."""
from __future__ import annotations

from dataclasses import dataclass


class MemoryPreferenceConflict(Exception):
    """The preference changed while an operation was in flight."""


class MemoryPreferenceUnavailable(Exception):
    """The current preference could not be established."""


@dataclass(frozen=True)
class MemoryPreference:
    automatic_enabled: bool = True
    version: int = 0

    @property
    def etag(self) -> str:
        # Ordinary memory writes change the state ETag, not this preference ETag.
        return f'"memory-preference-{self.version}"'
