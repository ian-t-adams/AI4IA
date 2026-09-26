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
    HTTP routes. It is never physical deletion or free quota.
    """
    value = model.get("runtimeEnabled", True)
    if type(value) is not bool:
        raise ValueError("runtimeEnabled must be a Boolean")
    return value


def image_editing(model: dict[str, Any]) -> bool:
    """Read the strict optional ``imageEditing`` Boolean (default false).

    Only an Azure OpenAI image row (category ``image``, api ``chat``, format
    ``OpenAI``) may declare it: the edit contract is the deployment-scoped
    ``images/edits`` multipart operation, which MAI and BFL do not serve. The
    gateway generator opens ``images/edits`` only for rows that declare it.
    """
    value = model.get("imageEditing", False)
    if type(value) is not bool:
        raise ValueError("imageEditing must be a Boolean")
    if value and (
        model.get("category") != "image"
        or model.get("api", "chat") != "chat"
        or model.get("format") != "OpenAI"
    ):
        raise ValueError("imageEditing requires an Azure OpenAI image row")
    return value


def image_editing_default(model: dict[str, Any]) -> bool:
    """Read the strict optional ``imageEditingDefault`` Boolean (default false)."""
    value = model.get("imageEditingDefault", False)
    if type(value) is not bool:
        raise ValueError("imageEditingDefault must be a Boolean")
    if value and not image_editing(model):
        raise ValueError("imageEditingDefault requires imageEditing")
    return value


def source_catalog(models: dict[str, Any]) -> dict[str, Any]:
    return {
        **models,
        "catalog": [model for model in models["catalog"] if model_target(model) == "source"],
    }
