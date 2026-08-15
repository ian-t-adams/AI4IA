"""Guards the PostgreSQL retirement of 2026-08-06.

The memory migration to Cosmos completed, the Flexible Server was decommissioned,
and every provisioning path, runtime setting and admin panel that depended on it
was removed. This test exists because the failure mode is *silent and expensive*:
a reintroduced `Microsoft.DBforPostgreSQL` resource in `infra/` costs money from
the next `azd provision` onward and nothing else in CI would notice, while a
reintroduced `AI4IA_POSTGRES_*` setting would read as a supported knob that no
deployment can populate — the "claimed but not present" defect the repository
audit was written to catch.

Deliberately NOT guarded:

* `scripts/migrate-memory-to-cosmos.py` keeps its `--postgres-*` flags. It is a
  standalone operator tool pointed at an explicit connection target, so it makes
  no claim that the platform provisions a server. It needs `pip install asyncpg`
  at run time; `asyncpg` was dropped from the API's runtime dependencies because
  nothing under `app/api/src` imports it any more.
* `infra/abbreviations.json` keeps `azurePostgreSqlFlexible`. That file is azd's
  standard naming reference for *all* Azure resource types, not an inventory of
  what this repo deploys.
* Prose that describes the retirement itself (this file, the runbook, the
  changelog, the audit) obviously has to name the thing being retired.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Any ARM resource under the Postgres provider. Substring-tolerant so a nested
# child type (`.../flexibleServers/databases`) is caught too.
_ARM_PROVIDER = re.compile(r"Microsoft\.DBforPostgreSQL", re.IGNORECASE)

# The runtime settings the retired server used to populate. Matched on the env
# name rather than the field so `.env.example`, Bicep and docs are all covered by
# one pattern.
_RUNTIME_ENV = re.compile(r"AI4IA_(?:POSTGRES_[A-Z_]+|METRICS_POSTGRES_RESOURCE_ID)")

# Files whose *subject* is the retirement. Prose here must be able to say
# "PostgreSQL" without tripping the guard.
_PROSE_EXEMPT = {
    Path("scripts/tests/test_postgres_retired.py"),
    Path("CHANGELOG.md"),
    # "Why Cosmos" is a comparison against the design that was replaced, and the
    # page now states the retirement outright. It has to be able to name the thing
    # it replaced.
    Path("docs/memory.md"),
    # Generated point-in-time snapshots of the *live* subscription, produced by
    # scripts/status-snapshot.ps1 from Azure Resource Graph. They report what is
    # actually deployed, so asserting on them here would mean asserting on
    # Azure's state, not the repo's claims.
    Path("site/data/inventory.js"),
    Path("site/data/status.js"),
}


def _tracked(*globs: str) -> list[Path]:
    out: list[Path] = []
    for pattern in globs:
        out.extend(
            p
            for p in REPO.glob(pattern)
            if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts
        )
    return sorted(out)


class PostgresProvisioningStaysRetired(unittest.TestCase):
    def test_no_bicep_declares_a_postgres_resource(self) -> None:
        offenders = [
            str(p.relative_to(REPO))
            for p in _tracked("infra/**/*.bicep")
            if _ARM_PROVIDER.search(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            offenders,
            [],
            "PostgreSQL was retired and the server deleted, but these "
            f"Bicep files declare a Microsoft.DBforPostgreSQL resource: {offenders}. "
            "A reintroduced server bills from the next provision and has no data to "
            "serve. Cosmos is the only memory backend and Azure AI Search the only "
            "document-chunk index.",
        )

    def test_compiled_arm_template_contains_no_postgres(self) -> None:
        """Catches an indirect reintroduction a source grep would miss.

        A module reference, a `loadJsonContent` catalog or a param default could
        put the provider back into the deployed template without the literal
        string appearing in `infra/**/*.bicep`. This asserts on the artifact ARM
        actually receives, when a prebuilt one is available.
        """
        compiled = REPO / "infra" / "main.json"
        if not compiled.exists():
            self.skipTest("no prebuilt infra/main.json; the Bicep source check covers this")
        self.assertNotRegex(compiled.read_text(encoding="utf-8"), _ARM_PROVIDER)

    def test_no_deployable_parameter_configures_postgres(self) -> None:
        params = REPO / "infra" / "main.parameters.json"
        text = params.read_text(encoding="utf-8")
        self.assertNotRegex(
            text,
            re.compile(r"postgres", re.IGNORECASE),
            "infra/main.parameters.json still binds a Postgres parameter; the "
            "server it targeted no longer exists.",
        )
        json.loads(text)  # the removal must leave valid JSON


class PostgresRuntimeSettingsStayRetired(unittest.TestCase):
    def test_no_api_source_reads_a_postgres_setting(self) -> None:
        offenders: list[str] = []
        for path in _tracked("app/api/src/**/*.py"):
            text = path.read_text(encoding="utf-8")
            if _RUNTIME_ENV.search(text) or "asyncpg" in text:
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(
            offenders,
            [],
            "These API sources reference a retired Postgres setting or the asyncpg "
            f"driver: {offenders}. asyncpg was removed from app/api/pyproject.toml, "
            "so importing it raises ImportError in the deployed container.",
        )

    def test_env_example_offers_no_postgres_knob(self) -> None:
        text = (REPO / "app" / "api" / ".env.example").read_text(encoding="utf-8")
        found = sorted(set(_RUNTIME_ENV.findall(text)))
        self.assertEqual(
            found,
            [],
            f".env.example still documents retired Postgres settings: {found}. "
            "Nothing reads them, so an operator setting one gets silence.",
        )

    def test_asyncpg_is_not_an_api_dependency(self) -> None:
        text = (REPO / "app" / "api" / "pyproject.toml").read_text(encoding="utf-8")
        # Only flag a real requirement line, not the comment explaining the removal.
        requirement = re.compile(r'^\s*"asyncpg[^"]*"', re.MULTILINE)
        self.assertIsNone(
            requirement.search(text),
            "asyncpg is back in the API's dependencies but no API source imports "
            "it. scripts/migrate-memory-to-cosmos.py installs it on demand.",
        )


class PostgresIsNotClaimedAsDeployedArchitecture(unittest.TestCase):
    """The portal and docs must not advertise a component that is gone.

    This is the half of the retirement that a build gate cannot catch: Bicep
    stops provisioning the server, but a service card on the portal keeps telling
    a reader it exists.
    """

    def test_no_doc_or_portal_page_lists_postgres_as_a_component(self) -> None:
        # A line may name PostgreSQL if it says, on that same line, that it is
        # retired. That is deliberately narrower than exempting a whole file:
        # deployment.md needs one sentence explaining why §7.1 and §7.7 are
        # vacant, and the price of that sentence is that it must state the fact
        # rather than describe a live resource.
        allowed = re.compile(r"retire", re.IGNORECASE)
        offenders: list[str] = []
        for path in _tracked("docs/**/*.md", "site/**/*.html", "site/data/*.js", "*.md"):
            rel = path.relative_to(REPO)
            if rel in _PROSE_EXEMPT:
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if re.search(r"postgres|pgvector|psql", line, re.IGNORECASE) and not allowed.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()[:90]}")
        self.assertEqual(
            offenders,
            [],
            "These pages still present PostgreSQL as part of the running system, "
            "which is the exact 'claimed but not present' gap the audit flagged:\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
