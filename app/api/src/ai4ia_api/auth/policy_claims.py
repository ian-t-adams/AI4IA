"""Bounded policy evidence retained only after the existing JWT verification."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

MAX_ROLES = 128
MAX_GROUPS = 200
MAX_ROLE_LENGTH = 256
GROUP_ID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


class ValidatedPolicyClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    roles_complete: bool = True
    groups_complete: bool = True
    expires_at: int | None = None


def _values(raw: Any, *, groups: bool) -> tuple[tuple[str, ...], bool]:
    maximum = MAX_GROUPS if groups else MAX_ROLES
    if not isinstance(raw, list) or len(raw) > maximum:
        return (), False
    values: list[str] = []
    for value in raw:
        if (
            not isinstance(value, str)
            or not value or len(value) > MAX_ROLE_LENGTH
            or value != value.strip() or not value.isprintable()
            or (groups and GROUP_ID.fullmatch(value) is None)
            or value in values
        ):
            return (), False
        values.append(value)
    return tuple(values), True


def verified_policy_claims(claims: Mapping[str, Any]) -> ValidatedPolicyClaims:
    roles, roles_complete = _values(claims["roles"], groups=False) if "roles" in claims else ((), True)
    groups, groups_complete = _values(claims["groups"], groups=True) if "groups" in claims else ((), True)
    names = claims.get("_claim_names")
    if (
        "hasgroups" in claims
        or "_claim_sources" in claims
        or ("_claim_names" in claims and (
            not isinstance(names, dict) or "groups" in names
        ))
    ):
        groups, groups_complete = (), False
    if "_claim_names" in claims and (not isinstance(names, dict) or "roles" in names):
        roles, roles_complete = (), False
    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, int) or not 0 < expiry < 253402300800:
        expiry = None
        roles_complete = groups_complete = False
    return ValidatedPolicyClaims(
        roles=roles, groups=groups, roles_complete=roles_complete,
        groups_complete=groups_complete, expires_at=expiry,
    )
