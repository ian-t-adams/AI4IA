"""Ownership / access resolution for the document library (Phase 11A).

v1 is **owner-only**. The resolver is written against the sharing-ready manifest
fields (``visibility`` + ``acl``) so enabling sharing later is an additive flip
here, not a change at every call site. In v1 those fields keep their inert
defaults (``private`` / empty), so this is equivalent to ``doc.userId == user``.
"""
from __future__ import annotations

from .models import UserDocument, Visibility


def can_access(user_id: str, doc: UserDocument) -> bool:
    """True if ``user_id`` may read ``doc``.

    Owner always wins. The non-owner branches are reserved for the sharing
    enablement and are unreachable in v1 (visibility is always ``private`` and
    ``acl`` is always empty), but encoding them now keeps the contract stable.
    """
    if doc.userId == user_id:
        return True
    # --- Reserved sharing paths (inert in v1) ---
    if doc.visibility == Visibility.public:
        return True
    if doc.visibility == Visibility.shared and user_id in doc.acl:
        return True
    return False


def require_owner(user_id: str, doc: UserDocument) -> bool:
    """True only for the owner. Mutations (delete/update) stay owner-only even
    after read-sharing is enabled, so they use this rather than ``can_access``."""
    return doc.userId == user_id
