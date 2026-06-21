"""Content-hash dedupe helpers for the document library.

Re-uploading identical bytes should reuse the existing manifest rather than
re-running the (slow, metered) Content Understanding crack. The dedupe key is the
sha256 of the raw bytes combined with the analyzer id: the same file cracked by a
*different* analyzer is a legitimately different result and must not collide.
"""
from __future__ import annotations

import hashlib


def content_hash(data: bytes) -> str:
    """sha256 hex digest of the raw upload bytes."""
    return hashlib.sha256(data).hexdigest()


def dedupe_key(content_hash_hex: str, analyzer_id: str | None) -> str:
    """Stable cache key for (bytes, analyzer). ``None`` analyzer is its own
    bucket so a later explicit-analyzer crack doesn't alias the default one."""
    return f"{content_hash_hex}:{analyzer_id or ''}"
