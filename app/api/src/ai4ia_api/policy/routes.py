"""Explicit operation inventory; route identity never comes from client fields."""
from __future__ import annotations

from fastapi import Request

from .context import current_binding, require_policy
from .models import PolicyDecision, PolicyError, PolicyOperation, PolicyRequest

LIBRARY_OPERATIONS: dict[str, tuple[PolicyOperation, ...]] = {
    "library_summary": ("document.read",),
    "list_documents": ("document.read",),
    "upload_document": ("document.upload",),
    "get_document_analysis": ("document.read",),
    "get_document": ("document.read",),
    "list_document_versions": ("document.read",),
    "download_document_version": ("document.export",),
    "get_media_timeline": ("document.read",),
    "stream_document_media": ("document.read",),
    "get_document_index": ("document.index",),
    "reindex_document": ("document.index", "document.process"),
    "reindex_all_documents": ("document.index", "document.process"),
    "save_document_to_memory": ("document.memory",),
    "list_shared_with_me": ("document.read",),
    "get_document_shares": ("document.share",),
    "set_document_shares": ("document.share",),
    "list_annotations": ("document.annotate",),
    "create_annotation": ("document.annotate",),
    "update_annotation": ("document.annotate",),
    "list_analyzers": ("document.analyzers",),
    "get_analyzer": ("document.analyzers",),
    "create_analyzer": ("document.analyzers",),
}
LIBRARY_CLEANUP = frozenset({
    "purge_document_chunks", "delete_document", "forget_document_from_memory",
    "revoke_document_share", "delete_annotation", "delete_analyzer",
})
ADMIN_ROUTE_OPERATIONS: dict[str, tuple[PolicyOperation, ...]] = {
    "usage_summary": ("admin.usage.read",),
    "usage_by_model": ("admin.usage.read",),
    "usage_by_day": ("admin.usage.read",),
    "usage_agents": ("admin.usage.read",),
    "usage_user_agents": ("admin.usage.read",),
    "usage_distributions": ("admin.usage.read",),
    "usage_overview": ("admin.usage.read", "admin.entitlements.read"),
    "usage_by_user": ("admin.usage.read", "admin.entitlements.read"),
    "metrics_resources": ("admin.metrics.resources.read",),
    "metrics_operations": ("admin.metrics.operations.read",),
    "metrics_security": ("admin.metrics.security.read",),
    "metrics_web_search": ("admin.metrics.websearch.read",),
    "metrics_official_mcp": ("admin.mcp.inspect",),
    "list_overrides": ("admin.entitlements.read",),
    "get_user_entitlement": ("admin.entitlements.read",),
    "set_user_entitlement": ("admin.entitlements.write",),
    "clear_user_entitlement": ("admin.entitlements.write",),
}


def route_identity(request: Request) -> tuple[str, str]:
    endpoint = getattr(request.scope.get("route"), "endpoint", None)
    return (
        getattr(endpoint, "__module__", "").rsplit(".", 1)[-1],
        getattr(endpoint, "__name__", ""),
    )


def admin_operations(request: Request) -> tuple[PolicyOperation, ...]:
    module, name = route_identity(request)
    if module not in {"admin_usage", "entitlements"} or name not in ADMIN_ROUTE_OPERATIONS:
        raise PolicyError(PolicyDecision("deny", "policy_surface_unsupported"))
    operations = list(ADMIN_ROUTE_OPERATIONS[name])
    # Let FastAPI validate malformed Boolean query values; any true spelling
    # accepted by its Boolean parser needs the extra authority before enrichment.
    if request.query_params.get("identify", "").lower() in {"1", "true", "on", "yes"}:
        operations.append("admin.directory.read")
    if name == "metrics_official_mcp" and request.query_params.get("refresh", "").lower() in {
        "1", "true", "on", "yes",
    }:
        operations.append("admin.mcp.refresh")
    return tuple(operations)


async def authorize_http_operation(request: Request) -> None:
    binding = current_binding()
    if binding is None or not binding.service.enabled:
        return
    module, name = route_identity(request)
    operations: tuple[PolicyOperation, ...] = ()
    if module == "library":
        if name in LIBRARY_CLEANUP:
            return
        if name not in LIBRARY_OPERATIONS:
            raise PolicyError(PolicyDecision("deny", "policy_surface_unsupported"))
        operations = LIBRARY_OPERATIONS[name]
    elif module == "documents":
        if name == "upload_document":
            operations = ("document.upload",)
        elif name == "list_documents":
            operations = ("document.read",)
        elif name != "delete_document":
            raise PolicyError(PolicyDecision("deny", "policy_surface_unsupported"))
    elif module == "docprocessing":
        operations = ("document.export",)
    for operation in operations:
        await require_policy(PolicyRequest(operation), owner_id=binding.owner_id)
