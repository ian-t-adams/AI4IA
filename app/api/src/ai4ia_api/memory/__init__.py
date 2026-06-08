"""Per-user semantic memory (Phase 5).

Recall relevant snippets before a model call and remember durable user
utterances after a successful turn. Disabled by default — the factory returns a
:class:`NoopMemoryService` so chat code calls memory unconditionally.
"""

from .factory import build_memory_service
from .models import MemoryRecord
from .service import MemoryService, MemoryServiceProtocol, NoopMemoryService

__all__ = [
    "MemoryRecord",
    "MemoryService",
    "MemoryServiceProtocol",
    "NoopMemoryService",
    "build_memory_service",
]
