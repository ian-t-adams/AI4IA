"""Ownership / access resolution for the document library.

Reads are governed by :func:`can_access`; mutations stay owner-only via
:func:`require_owner`. Sharing is keyed on the grantee's *email*: a
``shared`` document grants read access to every email in its ``acl``, while a
``public`` document grants read access to every authenticated user (the app
authenticates against a single tenant, so "public" is tenant-walled — there is no
unauthenticated path). Ownership and storage partitioning stay keyed on the
owner's ``userId``; only the *grant* dimension is email-based.

The single-tenant premise is load-bearing and is **enforced**, not assumed:
``Settings._validate_library_sharing_is_tenant_walled`` refuses to start when
``AI4IA_ENTRA_ALLOWED_TENANTS`` names more than one tenant. Without that, adding a
second tenant would retroactively turn every existing ``public`` document into
cross-tenant readable, since those documents were shared under the old meaning of
the word. Making this genuinely multi-tenant means persisting the owner's tenant
and comparing it in :func:`can_access`, or renaming the visibility to
``application_public`` so the name stops implying a wall that is not there
(audit finding P1-10).
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from .models import UserDocument, Visibility
from .repository import DocumentLibraryRepository, DocumentNotFoundError


def normalize_principal(email: str | None) -> str:
    """Canonical form of a grantee/viewer email for ACL storage and comparison.

    Lowercased and trimmed; ``None``/blank collapses to ``""`` (never a valid
    grant), so a viewer with no email claim can only reach owner/public documents.
    """
    return (email or "").strip().lower()


class ShareableRecord(Protocol):
    @property
    def userId(self) -> str: ...
    @property
    def visibility(self) -> Visibility: ...
    @property
    def acl(self) -> Sequence[str]: ...


@runtime_checkable
class TenantShareableRecord(ShareableRecord, Protocol):
    @property
    def tenantId(self) -> str: ...
    @property
    def groupAcl(self) -> Sequence[str]: ...


def valid_grantee_email(value: str) -> bool:
    if not value or " " in value or value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


def can_access(
    user_id: str, doc: ShareableRecord, *, email: str | None = None,
    tenant_id: str | None = None, groups: Iterable[str] = (),
) -> bool:
    """True if the caller may *read* ``doc``.

    Owner always wins (by ``userId``). A ``public`` document is readable by any
    authenticated caller (tenant-walled). A ``shared`` document is readable when
    the caller's normalized ``email`` is in the document's ``acl``. Everything
    else is denied.
    """
    tenant_bound = isinstance(doc, TenantShareableRecord)
    if tenant_bound and (not tenant_id or doc.tenantId != tenant_id):
        return False
    if doc.userId == user_id:
        return True
    if doc.visibility == Visibility.public:
        return True
    if doc.visibility == Visibility.shared:
        principal = normalize_principal(email)
        return (
            bool(principal) and principal in doc.acl
        ) or (
            isinstance(doc, TenantShareableRecord) and bool(set(groups) & set(doc.groupAcl))
        )
    return False


def require_owner(user_id: str, doc: ShareableRecord) -> bool:
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
