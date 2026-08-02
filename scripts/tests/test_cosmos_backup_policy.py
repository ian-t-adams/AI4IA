"""Pin the Cosmos backup posture, which is irreversible and silently costly to get wrong.

The Cosmos account holds the canonical store -- sessions, messages, usage,
memory, user agents, workflows, MCP server records, document manifests. Its
backup policy is therefore the difference between "restore yesterday" and
"that data is gone", and two properties of this particular knob make it worth a
test rather than a comment:

1. **Enabling continuous mode is one-way.** Azure offers no path back to
   periodic. So a change here is not a change you can review-and-revert; it is
   effectively permanent the moment it deploys.

2. **The tier boundary is a billing boundary, and it is invisible.**
   `Continuous7Days` is the only continuous tier with no backup-storage charge.
   `Continuous30Days` / `Continuous35Days` are valid ARM, deploy identically,
   look identical in the portal, and quietly start billing for backup storage.
   Nothing fails, so nothing catches it.

This account was migrated from `Periodic` (4-hourly snapshots, 8 hours retained)
to `Continuous7Days`, taking the recovery window from roughly eight hours --
recoverable only through a support ticket -- to any second in the last seven
days, self-service, at no recurring cost.

That migration is also the reason this file exists rather than a comment. The
repo previously asserted, in both `data.bicep` and the deployment runbook, that
continuous backup was "not usable on a serverless account" and that Azure would
"refuse to restore *into* a serverless account". Both claims were false, and
false in the expensive direction: they documented the better posture as
impossible, so nobody would try it. They were disproved empirically by standing
up a throwaway serverless account, enabling continuous backup, and restoring it
-- which produced a *serverless* account (`createMode=Restore`) with the
container and its partition key intact.

The genuine serverless restriction that makes that claim plausible belongs to a
different feature: Azure Backup **vaulted** backup (preview) cannot restore to a
serverless target. Conflating the two is the trap.

One more constraint worth knowing before editing the block this test guards:
Azure rejects a backup-*mode* change bundled with any other property change
("Cannot update continuous backup mode and other properties at the same time"),
so the mode cannot be moved by editing Bicep and redeploying. It has to be a
standalone `az cosmosdb update`, after which the Bicep restates the result to
keep redeploys a no-op.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_BICEP = ROOT / "infra" / "modules" / "data.bicep"

# The only continuous tier Azure does not charge backup storage for.
FREE_CONTINUOUS_TIER = "Continuous7Days"


def _strip_comments(text: str) -> str:
    """Drop `//` line comments so prose about `Periodic` cannot satisfy a match.

    The block under test is deliberately heavily commented, and those comments
    name both `Periodic` and the billed tiers while explaining why they are not
    used. Matching against raw source would let the documentation itself pass
    the test.
    """
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _extract_block(text: str, key: str) -> str | None:
    """Return the full body of `key: { ... }`, matching braces rather than regex.

    A non-greedy regex stops at the first `}`, which here is the one closing the
    nested `continuousModeProperties`. That would silently exclude anything
    declared after it -- including exactly the stale periodic properties one of
    these tests exists to catch. Counting braces keeps the whole block in scope.
    """
    start = re.search(rf"\b{re.escape(key)}:\s*\{{", text)
    if start is None:
        return None
    i = start.end()
    depth = 1
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    if depth:
        return None
    return text[start.end() : i - 1]


class CosmosBackupPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            DATA_BICEP.is_file(),
            f"expected the data module at {DATA_BICEP}; if it moved, update this test "
            "rather than deleting it -- the invariant it guards is irreversible",
        )
        self.source = _strip_comments(DATA_BICEP.read_text(encoding="utf-8"))

        body = _extract_block(self.source, "backupPolicy")
        self.assertIsNotNone(
            body,
            "no backupPolicy block found in data.bicep. Leaving it unset inherits an "
            "Azure default rather than declaring a recovery posture, which is exactly "
            "the ambiguity this account was pinned to remove.",
        )
        assert body is not None
        self.body = body

    def test_the_cosmos_account_declares_a_backup_policy_at_all(self) -> None:
        """Non-vacuity floor: the rest of the assertions are meaningless if setUp
        matched an empty or unrelated block."""
        self.assertIn(
            "Microsoft.DocumentDB/databaseAccounts",
            self.source,
            "this test only means something while data.bicep still declares the Cosmos "
            "account; it appears to have moved",
        )
        self.assertGreater(
            len(self.body.strip()),
            10,
            f"backupPolicy block looks empty: {self.body!r}",
        )

    def test_backup_mode_is_continuous(self) -> None:
        """A revert to Periodic is a durability downgrade, and unshippable besides.

        Azure cannot move a continuous account back to periodic, so this would not
        merely reduce the recovery window on paper -- it would fail the deploy
        against the live account while looking like a reasonable diff in review.
        """
        self.assertRegex(
            self.body,
            r"type:\s*'Continuous'",
            "Cosmos backup mode must stay 'Continuous'. Periodic drops self-service "
            "point-in-time restore, and Azure will not accept the transition on the "
            "live account because continuous mode is one-way.",
        )
        self.assertNotRegex(
            self.body,
            r"type:\s*'Periodic'",
            "found a Periodic backup policy; the live account is Continuous and cannot "
            "go back",
        )

    def test_continuous_tier_is_the_one_that_costs_nothing_to_store(self) -> None:
        """30/35-day tiers are valid, look identical, and silently bill for storage."""
        tier = re.search(r"tier:\s*'(?P<tier>[^']+)'", self.body)
        self.assertIsNotNone(
            tier,
            "continuous mode without an explicit tier: Azure defaults to 30-day "
            "retention, which is a billed tier. State the tier so the cost is a "
            "decision rather than an inherited default.",
        )
        assert tier is not None
        self.assertEqual(
            tier.group("tier"),
            FREE_CONTINUOUS_TIER,
            f"expected {FREE_CONTINUOUS_TIER}, the only continuous tier with no "
            "backup-storage charge. Moving to Continuous30Days/Continuous35Days is a "
            "legitimate choice, but it starts billing for backup storage with no "
            "deployment-time signal -- change this test in the same commit so the "
            "cost is acknowledged.",
        )

    def test_periodic_only_properties_are_not_left_behind(self) -> None:
        """Stale periodic properties alongside continuous mode are inert, not fatal.

        ARM ignores `periodicModeProperties` once the mode is continuous, so a
        half-finished edit leaves numbers in the template that describe a retention
        policy the account does not have -- the same "reads as configured, governs
        nothing" failure this repo keeps finding.
        """
        for leftover in ("periodicModeProperties", "backupIntervalInMinutes",
                         "backupRetentionIntervalInHours", "backupStorageRedundancy"):
            self.assertNotIn(
                leftover,
                self.body,
                f"'{leftover}' is a periodic-mode property and is ignored under "
                "continuous mode. Remove it rather than leaving a value that looks "
                "authoritative but governs nothing.",
            )


if __name__ == "__main__":
    unittest.main()
