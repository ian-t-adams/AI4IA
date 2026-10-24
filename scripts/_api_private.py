"""Finite API-private recovery phases; no deployment, grants or network IO."""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping

PHASES = ("legacy", "candidate", "activate", "private")


def phase(value: str) -> str:
    if value not in PHASES:
        raise ValueError("API network phase must be legacy, candidate, activate or private.")
    return value


def api_name(environment: str, selected: str) -> str:
    phase(selected)
    if re.fullmatch(r"[a-z][a-z0-9-]{1,19}", environment) is None:
        raise ValueError("Invalid API environment token.")
    return f"ca-api-{'private-' if selected != 'legacy' else ''}{environment}"


def validate_prefixes(selected: str, vnet: str, api: str, endpoint: str) -> None:
    phase(selected)
    if selected == "legacy":
        if any((vnet, api, endpoint)):
            raise ValueError("Private prefixes require an explicit recovery phase.")
        return
    networks = [ipaddress.IPv4Network(value, strict=True) for value in (vnet, api, endpoint)]
    space, application, private_endpoint = networks
    private = [ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    reserved = [ipaddress.IPv4Network(value) for value in ("172.30.0.0/16", "172.31.0.0/16")]
    if (
        not any(space.subnet_of(n) for n in private)
        or any(space.overlaps(n) for n in reserved)
        or space.prefixlen not in (24, 25)
        or application.prefixlen != 26 or private_endpoint.prefixlen != 27
        or not application.subnet_of(space) or not private_endpoint.subnet_of(space)
        or application.overlaps(private_endpoint)
    ):
        raise ValueError("Private API needs a standalone RFC1918 /24 or /25, disjoint /26 API and /27 endpoint subnets.")


def from_environment(env: Mapping[str, str]) -> str:
    selected = phase(env.get("AI4IA_API_NETWORK_PHASE") or "legacy")
    validate_prefixes(
        selected, env.get("AI4IA_API_PRIVATE_VNET_PREFIX", ""),
        env.get("AI4IA_API_PRIVATE_SUBNET_PREFIX", ""),
        env.get("AI4IA_COSMOS_PRIVATE_ENDPOINT_SUBNET_PREFIX", ""),
    )
    return selected
