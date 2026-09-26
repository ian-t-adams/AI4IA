"""Owner-attested resolution of a blocked chain: exact, single-use and digest-approved.

A blocked chain has lost evidence that could hide an application write, so no
dispatch, configuration change or elapsed time clears it. The owner investigates,
records the finding in the workflow-only ``AI4IA_CANARY_RESOLUTION`` variable, and
approves that exact record by dispatching ``resolve`` with the SHA-256 this module
prints. The record names the blocked predecessor it resolves, the lost runs it
investigated, and the new lease it admits. A later block has a different
predecessor, so a record can resolve only one blocked state.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .configuration import guid
from .contracts import SHA256, CanaryError, Run, digest, integer, obj, strict_json, timestamp

VERSION = 1
# The only finding that authorizes a new chain. Anything else, such as a write
# that still needs cleanup, must be resolved before a record can be approved.
EVIDENCE = ("no_application_write",)
MAX_LOST_RUNS = 4
MAX_SUPERSEDED = 8
MAX_RECORD_BYTES = 4096


@dataclass(frozen=True)
class Attested:
    """GitHub's own record of a run the owner investigated; never its artifacts or logs."""

    run: Run
    updated_at: str

    def document(self) -> dict[str, Any]:
        return {"run": asdict(self.run), "updated_at": self.updated_at}

    @classmethod
    def parse(cls, value: Any) -> Attested:
        data = obj(value, {"run", "updated_at"})
        timestamp(data["updated_at"])
        return cls(Run.parse(data["run"]), data["updated_at"])


@dataclass(frozen=True)
class Resolution:
    version: int
    blocked_run_id: int
    lost_run_ids: tuple[int, ...]
    evidence: str
    approval_id: str
    superseded_approval_digests: tuple[str, ...]

    @classmethod
    def load(cls, raw: str) -> Resolution:
        """Parse one record; every refusal is ``resolution_invalid`` and never poisons a chain."""
        try:
            data = obj(strict_json(raw.encode("utf-8"), limit=MAX_RECORD_BYTES), set(cls.__dataclass_fields__))
            integer(data["version"], VERSION, VERSION)
            blocked = integer(data["blocked_run_id"], 1, 2**63 - 1)
            lost, superseded = data["lost_run_ids"], data["superseded_approval_digests"]
            if (
                not isinstance(lost, list) or not 1 <= len(lost) <= MAX_LOST_RUNS
                or any(type(value) is not int or not 1 <= value < blocked for value in lost)
                or lost != sorted(set(lost))
                or not isinstance(superseded, list) or len(superseded) > MAX_SUPERSEDED
                or any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in superseded)
                or superseded != sorted(set(superseded))
                or data["evidence"] not in EVIDENCE
            ):
                raise CanaryError("resolution_invalid")
            result = cls(
                data["version"], blocked, tuple(lost), data["evidence"], guid(data["approval_id"]),
                tuple(superseded),
            )
        except (CanaryError, AttributeError, TypeError) as exc:
            raise CanaryError("resolution_invalid") from exc
        if result.approval_digest in result.superseded_approval_digests:
            raise CanaryError("resolution_invalid")
        return result

    def document(self) -> dict[str, Any]:
        return {
            "version": self.version, "blocked_run_id": self.blocked_run_id,
            "lost_run_ids": list(self.lost_run_ids), "evidence": self.evidence,
            "approval_id": self.approval_id,
            "superseded_approval_digests": list(self.superseded_approval_digests),
        }

    @property
    def sha256(self) -> str:
        # Canonical: whitespace and key order in the variable do not change it.
        return digest(self.document())

    @property
    def approval_digest(self) -> str:
        return digest(self.approval_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a resolution record offline and print the SHA-256 that approves it.",
    )
    parser.add_argument("record", type=Path, help="JSON file holding the exact AI4IA_CANARY_RESOLUTION value.")
    args = parser.parse_args(argv)
    try:
        record = Resolution.load(args.record.read_text(encoding="utf-8"))
    except (CanaryError, OSError, UnicodeError):
        print("Resolution record is invalid; nothing was approved.", file=sys.stderr)
        return 2
    print(f"blocked run {record.blocked_run_id}; lost runs {', '.join(map(str, record.lost_run_ids))}; "
          f"evidence {record.evidence}; new lease digest {record.approval_digest}")
    print(record.sha256)
    return 0


if __name__ == "__main__":
    sys.exit(main())
