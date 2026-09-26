"""Catalog scope is intent, never a credential or deployment readback."""
from __future__ import annotations

from typing import Any, Literal

ModelTarget = Literal["source", "external-claude"]


def model_target(model: dict[str, Any]) -> ModelTarget:
    value = model.get("deploymentTarget", "source")
    if value not in ("source", "external-claude"):
        raise ValueError("unsupported model deploymentTarget")
    if model.get("format") == "Anthropic" and value != "external-claude":
        raise ValueError("Anthropic catalog rows require the explicit external-claude target")
    if value == "external-claude":
        if model.get("format") != "Anthropic" or model.get("api") != "anthropic":
            raise ValueError("external-claude requires the Anthropic Messages adapter")
        thinking = model.get("anthropicThinking")
        if (
            thinking not in ("disabled", "adaptive")
            or model.get("samplingSupported") is not False
            or not model.get("reasoningEffort")
            or set(model["reasoningEffort"]) - {"low", "medium", "high"}
            # Adaptive thinking blocks are never replayed, so that profile is text-only.
            or (thinking == "adaptive" and (
                model.get("toolCalling") is not False or model.get("inputModalities") != ["text"]
            ))
        ):
            raise ValueError(
                "external-claude requires the explicit thinking-disabled or adaptive text-only effort profile"
            )
        for deployment in model.get("deployments", []):
            if (
                deployment.get("region") != "eastus2"
                or deployment.get("sku") not in ("GlobalStandard", "DataZoneStandard")
                or any(key in deployment for key in ("maxCapacity", "maxCapacityPool", "production"))
            ):
                raise ValueError("external-claude requires explicit eastus2 standard capacity")
        return "external-claude"
    return "source"


def runtime_enabled(model: dict[str, Any]) -> bool:
    """Read the strict optional ``runtimeEnabled`` Boolean (default true).

    False keeps the row's deployments in desired infrastructure, quota and
    retirement inventory; it only withdraws runtime availability and generated
    HTTP/realtime routes. It is never physical deletion or free quota.
    """
    value = model.get("runtimeEnabled", True)
    if type(value) is not bool:
        raise ValueError("runtimeEnabled must be a Boolean")
    return value


def source_catalog(models: dict[str, Any]) -> dict[str, Any]:
    return {
        **models,
        "catalog": [model for model in models["catalog"] if model_target(model) == "source"],
    }
