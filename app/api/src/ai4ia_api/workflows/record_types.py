"""Shared owner-partition namespace for definitions and control records."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

RECORD_KIND_FIELD = "recordKind"
CONTROL_RECORD_PREFIX = ":ai4ia:"
AUTOMATION_ID_PREFIX = ":ai4ia:automation:v1:"
AUTOMATION_OWNER_ID = AUTOMATION_ID_PREFIX + "owner"
AUTOMATION_OWNER_KIND = "ai4ia.automation.owner.v1"
AUTOMATION_SCHEDULE_KIND = "ai4ia.automation.schedule.v1"
AGENT_DEFINITION_KIND = "ai4ia.agent.definition.v1"
WORKFLOW_DEFINITION_KIND = "ai4ia.workflow.definition.v1"
PUBLICATION_HEAD_KIND = "ai4ia.publication.head.v1"
PUBLICATION_VERSION_KIND = "ai4ia.publication.version.v1"
PUBLICATION_REVIEW_KIND = "ai4ia.publication.review.v1"
PUBLICATION_BUDGET_KIND = "ai4ia.publication.budget.v1"
_NAME = re.compile(r"^[a-z](?:[a-z0-9_.-]{0,30}[a-z0-9_])?$")


def is_definition(
    document: Mapping[str, Any], *, user_id: str, kind: str, name: str | None = None,
) -> bool:
    identifier = document.get("id")
    return (
        isinstance(identifier, str)
        and not identifier.startswith(CONTROL_RECORD_PREFIX)
        and _NAME.fullmatch(identifier) is not None
        and document.get("name") == identifier
        and (name is None or identifier == name)
        and document.get("userId") == user_id
        and (RECORD_KIND_FIELD not in document or document[RECORD_KIND_FIELD] == kind)
    )


DEFINITION_QUERY = (
    "SELECT * FROM c WHERE c.userId = @uid "
    "AND NOT STARTSWITH(c.id, @controlPrefix) "
    "AND (NOT IS_DEFINED(c.recordKind) OR c.recordKind = @definitionKind)"
)
