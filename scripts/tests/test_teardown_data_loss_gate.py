"""Pin the data-loss gate on the teardown path.

``teardown.ps1`` deletes a resource group and *purges* soft-deleted Key Vaults.
``-Force`` has always meant "yes, really delete the infrastructure", and
infrastructure is precisely the part this repo can rebuild -- ``azd provision``
plus ``infra/models.json`` reconstruct it. The data is the part that has no IaC
behind it:

* Blob (uploaded documents, generated images/videos) has **no restore path**.
* Key Vault (per-user BYO MCP credentials) is **purged**, not soft-deleted.
* Cosmos is restorable only if the restorable-account instance id, location and a
  timestamp were captured *before* deletion, because restore targets a new
  account and the deleted one can no longer be queried for them.

The audit logged this as P1-8. The first fix corrected the runbook's prose, which
told the operator to "export anything you need to keep" -- true, but with no tool
to do it and nothing stopping a delete that skipped the step. This adds
``capture-data-recovery-state.ps1`` and makes ``-AcknowledgeDataLoss`` mandatory
alongside ``-Force``, so the irreversible acknowledgement cannot ride along with
the routine one.

These are source-level assertions, matching the convention of the other operator
script tests in this directory (no ``pwsh`` dependency, so they run anywhere).
The runtime behaviour they describe was verified by hand against the real script:
``-Force`` alone exits 2 and reaches no ``az`` call, ``-Force
-AcknowledgeDataLoss`` proceeds, and a dry run is unaffected. What is checked
here are the structural properties that keep that true.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEARDOWN = ROOT / "scripts" / "teardown.ps1"
CAPTURE = ROOT / "scripts" / "capture-data-recovery-state.ps1"
RUNBOOK = ROOT / "docs" / "runbooks" / "teardown.md"


class TeardownDataLossGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.teardown = TEARDOWN.read_text(encoding="utf-8")
        self.capture = CAPTURE.read_text(encoding="utf-8")
        self.runbook = RUNBOOK.read_text(encoding="utf-8")

    def test_teardown_declares_the_acknowledgement_switch(self) -> None:
        self.assertRegex(
            self.teardown,
            r"\[switch\]\s*\$AcknowledgeDataLoss",
            "teardown.ps1 must declare -AcknowledgeDataLoss",
        )

    def test_force_without_acknowledgement_refuses(self) -> None:
        """-Force alone must stop, and stop with a non-zero exit.

        Exiting 0 would let a wrapper script treat the refusal as success and
        carry on, which is the failure mode this gate exists to prevent.
        """
        gate = re.search(
            r"if\s*\(\s*\$Force\s+-and\s+-not\s+\$AcknowledgeDataLoss\s*\)\s*\{(.+?)\n\}",
            self.teardown,
            re.DOTALL,
        )
        self.assertIsNotNone(gate, "teardown.ps1 must refuse -Force without -AcknowledgeDataLoss")
        assert gate is not None
        self.assertRegex(
            gate.group(1),
            r"\bexit\s+[1-9]",
            "the refusal must exit non-zero, not fall through",
        )

    def test_gate_precedes_every_az_invocation(self) -> None:
        """The refusal must be unreachable-late.

        A check placed after the delete loop, or even after ``az account set``,
        would already have done work against the target subscription. Position is
        the whole property here, so it is asserted rather than assumed.
        """
        gate_at = self.teardown.find("-not $AcknowledgeDataLoss")
        self.assertGreater(gate_at, 0, "gate not found")
        first_az = re.search(r"^\s*az\s", self.teardown, re.MULTILINE)
        self.assertIsNotNone(first_az, "expected teardown.ps1 to invoke az")
        assert first_az is not None
        self.assertLess(
            gate_at,
            first_az.start(),
            "the data-loss gate must run before any az call, not partway through a delete",
        )

    def test_teardown_points_at_the_capture_script(self) -> None:
        self.assertTrue(CAPTURE.exists(), "capture-data-recovery-state.ps1 must exist")
        self.assertIn(
            "capture-data-recovery-state.ps1",
            self.teardown,
            "the refusal must tell the operator how to capture state, not just say no",
        )

    def test_capture_never_reads_secret_values(self) -> None:
        """The manifest is meant to be copied out of the environment.

        Secret *names* tell you who to notify. Secret *values* in that file would
        turn a recovery aid into a credential export, so the value-reading call
        shapes are excluded outright.
        """
        forbidden = [
            r"az\s+keyvault\s+secret\s+show",
            r"secret\s+list[^\n]*--query[^\n]*value",
            r"secret\s+download",
        ]
        for pattern in forbidden:
            self.assertNotRegex(
                self.capture,
                pattern,
                f"capture script must not read secret values ({pattern})",
            )
        self.assertRegex(
            self.capture,
            r"secret\D+list[\s\S]{0,200}?\[\]\.name",
            "capture script should list secret names only",
        )

    def test_capture_distinguishes_unreadable_from_empty(self) -> None:
        """A failed probe and an empty container both leave a counter at zero.

        Reporting "0 blobs" for a container the caller had no data-plane rights to
        read tells an operator there is nothing to lose, immediately before an
        irreversible delete. Listing blobs needs Storage Blob Data Reader, which a
        subscription Owner does not automatically hold, so this is the normal
        case rather than an edge case.
        """
        self.assertIn(
            "$blobCountsIncomplete",
            self.capture,
            "capture script must track whether blob sizes were actually measured",
        )
        self.assertRegex(
            self.capture,
            r"SIZE UNKNOWN",
            "capture script must report unknown sizes as unknown, never as zero",
        )

    def test_capture_normalizes_restore_timestamps(self) -> None:
        """Restore arguments must not be rendered in the operator's locale.

        ConvertFrom-Json yields a [datetime]; printing it gives "8/2/2026", which
        is unreadable months later on another machine. These values are inputs to
        `az cosmosdb restore`.
        """
        self.assertRegex(
            self.capture,
            r"ToUniversalTime\(\)\.ToString\(\s*[\"']yyyy-MM-ddTHH:mm:ssZ",
            "restore timestamps must be normalized to ISO-8601 UTC",
        )

    def test_runbook_never_shows_force_without_acknowledgement(self) -> None:
        """Docs must not publish a command that the script now refuses.

        A copy-pasteable ``-Force`` line that exits 2 trains operators to add
        whatever flag makes the error go away, which defeats the gate.
        """
        offenders = [
            line.strip()
            for line in self.runbook.splitlines()
            if "teardown.ps1" in line
            and "-Force" in line
            and "-AcknowledgeDataLoss" not in line
        ]
        self.assertEqual(
            offenders,
            [],
            "teardown.md shows -Force without -AcknowledgeDataLoss:\n" + "\n".join(offenders),
        )

    def test_runbook_runs_capture_before_teardown(self) -> None:
        """Ordering in the doc, not just existence of the step."""
        capture_at = self.runbook.find("capture-data-recovery-state.ps1")
        self.assertGreater(capture_at, 0, "teardown.md must reference the capture script")
        force_at = self.runbook.find("-AcknowledgeDataLoss")
        self.assertGreater(force_at, 0, "teardown.md must show the acknowledged teardown command")
        self.assertLess(
            capture_at,
            force_at,
            "the capture step must appear before the destructive command",
        )


if __name__ == "__main__":
    unittest.main()
