"""The operator-authored activation record for request-count hard admission.

The application only reads the one record selected by configuration. There is
no API, startup repair, Bicep data write or tool that authors it: an operator
creates it after the owner approves concrete cutover, bootstrap and recovery
evidence. Validation checks shape and ordering, not the truth of that evidence.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .models import CONTROL_PARTITION, ROLLOUT_KIND, Count

ROLLOUT_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"


def evidence_reference(value: str) -> str:
    """A credential-free https reference; never fetched and never an approval."""
    if not 1 <= len(value) <= 1000 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("invalid evidence reference")
    url = urlsplit(value)
    if (
        url.scheme != "https" or not url.hostname or url.username or url.password
        or url.query or url.fragment or url.port not in (None, 443)
    ):
        raise ValueError("invalid evidence reference")
    return value


EvidenceReference = Annotated[str, Field(strict=True), AfterValidator(evidence_reference)]


class HardQuotaRollout(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: Annotated[str, Field(pattern=f"^{ROLLOUT_ID_PATTERN}$")]
    userId: Literal["__ai4ia_hard_quota_control__"]
    kind: Literal["hard_quota_rollout_v1"]
    protocol: Literal[1]
    state: Literal["approved"]
    policyVersion: Literal["rolling-dispatch-v1"]
    scope: Literal["request_count_only"]
    singleWriteRegion: Literal[True]
    noCoordinationExpiry: Literal[True]
    # Store-clock seconds at or after the proven drain of every non-enforcing writer.
    coverageStart: Count
    writerCutoverEvidence: EvidenceReference
    bootstrapEvidence: EvidenceReference
    recoveryRetentionEvidence: EvidenceReference

    @model_validator(mode="before")
    @classmethod
    def exact_markers(cls, value: Any) -> Any:
        # Pydantic literals compare by equality, so True would pass as 1 and 1.0
        # or 1 as True. Approval markers must be exactly the JSON values.
        if isinstance(value, Mapping) and (
            type(value.get("protocol")) is not int
            or value.get("singleWriteRegion") is not True
            or value.get("noCoordinationExpiry") is not True
        ):
            raise ValueError("invalid rollout markers")
        return value


def parse_rollout(raw: Mapping[str, Any], *, rollout_id: str, observed_at: int) -> HardQuotaRollout:
    """Validate the exactly selected record against the store clock of its read."""
    if not re.fullmatch(ROLLOUT_ID_PATTERN, rollout_id):
        raise ValueError("invalid rollout id")
    body = {key: value for key, value in raw.items() if not key.startswith("_")}
    rollout = HardQuotaRollout.model_validate(body)
    if rollout.id != rollout_id or rollout.userId != CONTROL_PARTITION or rollout.kind != ROLLOUT_KIND:
        raise ValueError("rollout record is not the selected record")
    if rollout.coverageStart > observed_at:
        # Evidence must precede approval; a future cutover cannot have been observed.
        raise ValueError("rollout coverage is in the future")
    return rollout
