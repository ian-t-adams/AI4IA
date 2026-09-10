"""Exact predecessor discovery, not 'download the latest successful artifact'."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from .contracts import CanaryError, Run, WORKFLOW, integer, obj, timestamp
from .state import State, validate_predecessor
from .transport import Transport


def artifact_name(run: Run) -> str:
    return f"application-canary-state-{run.run_id}-{run.attempt}"


@dataclass(frozen=True)
class Predecessor:
    run: Run
    artifact_id: int
    created_at: str
    updated_at: str

    def document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, value: Any) -> Predecessor:
        data = obj(value, set(cls.__dataclass_fields__)).copy()
        data["run"] = Run.parse(data["run"])
        integer(data["artifact_id"], 1, 2**63 - 1)
        timestamp(data["created_at"])
        timestamp(data["updated_at"])
        return cls(**data)

    def validate_state(self, state: State, current: Run, now: datetime) -> None:
        validate_predecessor(state, current, now)
        observed = timestamp(state.report.observed_at)
        if (
            state.report.run != self.run
            or not timestamp(self.created_at) <= observed <= timestamp(self.updated_at) + timedelta(seconds=60)
        ):
            raise CanaryError("state_invalid")


def _run_metadata(data: Any, expected: Run, workflow_id: int, *, completed: bool) -> dict[str, Any]:
    raw = obj(data)
    for key in ("id", "run_number", "run_attempt", "workflow_id"):
        integer(raw.get(key), 1, 2**63 - 1)
    if timestamp(raw.get("created_at")) > timestamp(raw.get("updated_at")):
        raise CanaryError("state_invalid")
    if (
        raw.get("id") != expected.run_id or raw.get("run_number") != expected.number
        or raw.get("run_attempt") != expected.attempt or raw.get("head_sha") != expected.sha
        or raw.get("workflow_id") != workflow_id or raw.get("head_branch") != "main"
        or obj(raw.get("repository")).get("id") != expected.repository_id
        or obj(raw.get("head_repository")).get("id") != expected.repository_id
        or obj(raw.get("repository")).get("full_name") != expected.repository
        or raw.get("event") not in ("schedule", "workflow_dispatch")
        or (completed and raw.get("status") != "completed")
    ):
        raise CanaryError("state_invalid")
    return raw


async def locate(
    run: Run, token: str, *, transport: Transport,
) -> Predecessor | None:
    if not token:
        raise CanaryError("state_missing")
    root = f"https://api.github.com/repos/{run.repository}"

    async def read(path: str) -> dict[str, Any]:
        response = await transport.request(
            "GET", root + path, token=token,
            headers={"X-GitHub-Api-Version": "2022-11-28"},
        )
        if response.status != 200:
            raise CanaryError("state_missing")
        return response.object()

    workflow = await read(f"/actions/workflows/{WORKFLOW.rsplit('/', 1)[-1]}")
    if workflow.get("path") != WORKFLOW:
        raise CanaryError("state_invalid")
    workflow_id = integer(workflow.get("id"), 1, 2**63 - 1)
    _run_metadata(
        await read(f"/actions/runs/{run.run_id}"), run, workflow_id, completed=False,
    )
    if run.number == 1:
        return None
    inventory = await read(
        f"/actions/workflows/{workflow_id}/runs?per_page=20&branch=main"
    )
    rows = inventory.get("workflow_runs")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise CanaryError("state_missing")
    ids = [integer(obj(row).get("id"), 1, 2**63 - 1) for row in rows]
    if len(ids) != len(set(ids)):
        raise CanaryError("state_invalid")
    matches = [row for row in rows if row.get("run_number") == run.number - 1]
    if len(matches) != 1:
        raise CanaryError("state_gap")
    before = matches[0]
    previous_run = Run(
        run.repository, run.repository_id,
        integer(before.get("id"), 1, run.run_id - 1),
        run.number - 1, integer(before.get("run_attempt"), 1, 1),
        before.get("head_sha"),
    )
    previous_run.validate()
    _run_metadata(before, previous_run, workflow_id, completed=True)
    inventory = await read(f"/actions/runs/{previous_run.run_id}/artifacts?per_page=20")
    artifacts = inventory.get("artifacts")
    integer(inventory.get("total_count"), 0, 20)
    if (
        not isinstance(artifacts, list) or len(artifacts) > 20
        or inventory.get("total_count") != len(artifacts)
    ):
        raise CanaryError("state_missing")
    candidates = [obj(row) for row in artifacts if obj(row).get("name") == artifact_name(previous_run)]
    if len(candidates) != 1:
        raise CanaryError("state_missing")
    artifact = candidates[0]
    source = obj(artifact.get("workflow_run"))
    if (
        artifact.get("expired") is not False
        or source.get("id") != previous_run.run_id
        or source.get("repository_id") != run.repository_id
        or source.get("head_repository_id") != run.repository_id
        or source.get("head_branch") != "main" or source.get("head_sha") != previous_run.sha
    ):
        raise CanaryError("state_invalid")
    integer(artifact.get("size_in_bytes"), 1, 64 * 1024)
    return Predecessor(
        previous_run, integer(artifact.get("id"), 1, 2**63 - 1),
        before["created_at"], before["updated_at"],
    )
