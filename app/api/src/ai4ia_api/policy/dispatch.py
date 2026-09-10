"""Application policy at the existing common pre-egress seam, independent of quota."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .context import current_binding, current_tool, require_policy
from .models import PolicyDecision, PolicyError, PolicyRequest

if TYPE_CHECKING:
    from ..hard_quota.models import Surface
    from .service import PolicyService


async def authorize_dispatch(
    surface: Surface, *, deployment: str | None, required: bool = False,
    expected_owner: str | None = None, service: PolicyService | None = None,
) -> None:
    binding = current_binding()
    if (required or (service is not None and service.enabled)) and (
        binding is None or (service is not None and binding.service is not service)
    ):
        raise PolicyError(PolicyDecision("unavailable", "reauthentication_required"))
    if binding is None:
        return
    if expected_owner is not None and binding.owner_id != expected_owner:
        raise PolicyError(PolicyDecision("deny", "owner_mismatch"))
    if not binding.service.enabled:
        # An active publication still has its own version/access check.
        await require_policy(PolicyRequest("tool.invoke", tool_name=current_tool()))
        return
    actor = await binding.resolve()
    if deployment is not None:
        matches = [
            (entry, option) for entry in binding.service.catalog.models
            for option in entry.options if option.deploymentName == deployment
        ]
        if len(matches) != 1:
            raise PolicyError(PolicyDecision("deny", "model_unavailable"))
        entry, option = matches[0]
        await require_policy(PolicyRequest("model.invoke", model_id=entry.id, deployment=option))
        return
    if surface in {"document", "compute"}:
        await require_policy(PolicyRequest(
            "document.process" if surface == "document" else "document.compute",
        ))
        if "zones" in actor.domains or (surface == "document" and "models" in actor.domains):
            raise PolicyError(PolicyDecision("unavailable", "policy_surface_unsupported"))
        return
    if surface in {"mcp", "external_tool", "web_search"}:
        name = current_tool()
        if name is None:
            if "tools" in actor.domains:
                raise PolicyError(PolicyDecision("unavailable", "policy_surface_unsupported"))
            name = "unscoped_service"
        await require_policy(PolicyRequest("tool.invoke", tool_name=name))
        return
    # Some Speech Voice Live targets are service agents, not catalog deployments.
    if surface == "realtime" and "models" not in actor.domains and "zones" not in actor.domains:
        await require_policy(PolicyRequest("tool.invoke", tool_name="unscoped_service"))
        return
    raise PolicyError(PolicyDecision("unavailable", "policy_surface_unsupported"))
