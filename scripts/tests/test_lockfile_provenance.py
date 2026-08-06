"""Pins the dependency lockfile to public package provenance.

`app/api/uv.lock` records, for every resolved package, the registry it came from
and the exact URL of each artifact. Those URLs are the repo's only machine-
readable statement of where its Python dependencies actually come from.

**This guard exists because the obvious gate does not cover it, and that was
demonstrated rather than assumed.** `uv lock --check` (run by `app-ci`) only
asserts that the lockfile agrees with `pyproject.toml`; it says nothing about
which registry the artifacts resolve from. And nothing on the install path reads
the lock at all -- `app/api/Dockerfile` runs `pip install .` and CI runs
`pip install -e ".[dev,foundry]"`, both straight from `pyproject.toml`. So a
lockfile can be rewritten to point at a completely different registry and every
existing check still reports success.

That is not hypothetical. On 2026-08-06, commit `aad6889` ("retire PostgreSQL")
committed a `uv.lock` in which **1,659 lines** pointed at an internal Microsoft
package-feed proxy (`packagefeedproxy.microsoft.io` /
`ms-feed-*.pkgs.visualstudio.com`) instead of PyPI, because the machine that ran
`uv lock` resolves through a corporate mirror. `app-ci` passed green, including
its `uv lock --check` step. The file was repaired two PRs later purely by
coincidence, when a Dependabot PR regenerated it from PyPI.

Why it matters even though nothing installs from the lock today:

* It makes the lockfile **lie about provenance**. The lock is what a human or an
  agent reads to answer "where does this dependency come from"; an internal
  mirror URL answers that question wrongly, and the hashes alongside it lend the
  wrong answer false authority.
* The internal feed is **credentialed and unreachable** from anywhere else. The
  moment anything does read the lock -- `uv sync` in a Dockerfile, a vendoring
  step, an SBOM generator -- it breaks for every contributor and every CI runner,
  with an error that points at a host most readers will not recognise.
* It leaks internal infrastructure identifiers (feed GUIDs, org names) into a
  public repository.

If you regenerate the lockfile behind a corporate mirror, re-run `uv lock` with
`UV_INDEX_URL=https://pypi.org/simple` (or on an unproxied network) before
committing.
"""
from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCKFILE = REPO / "app" / "api" / "uv.lock"

# The only hosts a public Python lockfile should name: PyPI's index and the
# canonical artifact CDN it redirects to.
ALLOWED_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})

_URL_HOST = re.compile(r'https?://([^/"\s]+)')
_REGISTRY = re.compile(r'registry = "([^"]+)"')


def _hosts() -> Counter[str]:
    return Counter(_URL_HOST.findall(LOCKFILE.read_text(encoding="utf-8")))


class LockfileResolvesFromPublicPyPI(unittest.TestCase):
    def test_every_url_points_at_pypi_or_its_cdn(self) -> None:
        foreign = {h: n for h, n in _hosts().items() if h not in ALLOWED_HOSTS}
        self.assertEqual(
            foreign,
            {},
            "app/api/uv.lock references hosts that are not public PyPI: "
            f"{sorted(foreign)}. This usually means `uv lock` was run behind a "
            "corporate package mirror, which rewrites every artifact URL. "
            "Regenerate with UV_INDEX_URL=https://pypi.org/simple (or off the "
            "proxied network). Note that `uv lock --check` does NOT catch this: "
            "it only compares the lock against pyproject.toml.",
        )

    def test_the_declared_registry_is_public_pypi(self) -> None:
        registries = sorted(set(_REGISTRY.findall(LOCKFILE.read_text(encoding="utf-8"))))
        self.assertEqual(
            registries,
            ["https://pypi.org/simple"],
            "app/api/uv.lock declares a non-PyPI registry. Every dependency's "
            "stated provenance must be a public index a contributor can reach.",
        )

    def test_the_guard_is_not_vacuous(self) -> None:
        """A lockfile with no URLs would pass both checks above trivially.

        This asserts the file actually contains the artifact references the other
        two tests are meant to police, so a truncated or restructured lock fails
        loudly instead of silently disabling this module.
        """
        hosts = _hosts()
        self.assertGreater(
            sum(hosts.values()),
            500,
            "app/api/uv.lock contains far fewer URLs than expected, so the "
            "provenance checks above are no longer exercising anything real.",
        )
        for required in ("pypi.org", "files.pythonhosted.org"):
            self.assertIn(required, hosts, f"no {required} URLs found in uv.lock")


if __name__ == "__main__":
    unittest.main()
