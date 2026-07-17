"""Ownership / access resolution for the document library.

Reads are governed by :func:`can_access`; mutations stay owner-only via
:func:`require_owner`. Sharing is keyed on the grantee's *email*: a
``shared`` document grants read access to every email in its ``acl``, while a
``public`` document grants read access to every authenticated user (the app
authenticates against a single tenant, so "public" is tenant-walled — there is no
unauthenticated path). Ownership and storage partitioning stay keyed on the
owner's ``userId``; only the *grant* dimension is email-based.
"""
from __future__ import annotations

from .models import UserDocument, Visibility
from .repository import DocumentLibraryRepository, DocumentNotFoundError


def normalize_principal(email: str | None) -> str:
    """Canonical form of a grantee/viewer email for ACL storage and comparison.

    Lowercased and trimmed; ``None``/blank collapses to ``""`` (never a valid
    grant), so a viewer with no email claim can only reach owner/public documents.
    """
    return (email or "").strip().lower()


def can_access(user_id: str, doc: UserDocument, *, email: str | None = None) -> bool:
    """True if the caller may *read* ``doc``.

    Owner always wins (by ``userId``). A ``public`` document is readable by any
    authenticated caller (tenant-walled). A ``shared`` document is readable when
    the caller's normalized ``email`` is in the document's ``acl``. Everything
    else is denied.
    """
    if doc.userId == user_id:
        return True
    if doc.visibility == Visibility.public:
        return True
    if doc.visibility == Visibility.shared:
        principal = normalize_principal(email)
        return bool(principal) and principal in doc.acl
    return False


def require_owner(user_id: str, doc: UserDocument) -> bool:
    """True only for the owner. Mutations (delete/update/share/annotate) and the
    owner-private features (annotations, save-to-memory) stay owner-only even with
    read-sharing enabled, so they use this rather than ``can_access``."""
    return doc.userId == user_id


async def get_accessible_document(
    repository: DocumentLibraryRepository,
    user_id: str,
    document_id: str,
    *,
    email: str | None = None,
) -> UserDocument:
    """Resolve one owned/shared document without leaking inaccessible records."""
    try:
        return await repository.get_document(user_id, document_id)
    except DocumentNotFoundError:
        document = await repository.get_by_id(document_id)
        if document is None or not can_access(user_id, document, email=email):
            raise DocumentNotFoundError(document_id) from None
        return document


async def list_accessible_documents(
    repository: DocumentLibraryRepository,
    user_id: str,
    *,
    email: str | None = None,
) -> list[UserDocument]:
    """Return the caller's owned and explicitly shared documents, de-duplicated."""
    owned = await repository.list_documents(user_id)
    principal = normalize_principal(email)
    shared = await repository.list_shared_with(principal) if principal else []
    seen = {document.id for document in owned}
    return [*owned, *(document for document in shared if document.id not in seen)]
