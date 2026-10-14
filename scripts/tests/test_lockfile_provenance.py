"""Pins the dependency lockfiles to public package provenance.

The npm lock also requires public registry artifacts and SHA-512 integrity for
every package record. Mirror URLs can survive a package-manager refresh and
leave Dependabot unable to update the otherwise public Markdown dependency tree.

`app/api/uv.lock` records, for every resolved package, the registry it came from
and the exact URL of each artifact. Those URLs are the repo's only machine-
readable statement of where its Python dependencies actually come from.

**This guard exists because the obvious gate does not cover it, and that was
demonstrated rather than assumed.** `uv lock --check` (run by `app-ci`) only
asserts that the lockfile agrees with `pyproject.toml`; it says nothing about
which registry the artifacts resolve from. When this guard was added, neither
Docker nor CI installed from the lock. The API image now consumes it directly,
making public artifact provenance an active build prerequisite rather than only
a statement for auditors.

That is not hypothetical. On 2026-08-06, commit `aad6889` ("retire PostgreSQL")
committed a `uv.lock` in which **1,659 lines** pointed at an internal Microsoft
package-feed proxy (`packagefeedproxy.microsoft.io` /
`ms-feed-*.pkgs.visualstudio.com`) instead of PyPI, because the machine that ran
`uv lock` resolves through a corporate mirror. `app-ci` passed green, including
its `uv lock --check` step. The file was repaired two PRs later purely by
coincidence, when a Dependabot PR regenerated it from PyPI.

Why it matters:

* It makes the lockfile **lie about provenance**. The lock is what a human or an
  agent reads to answer "where does this dependency come from"; an internal
  mirror URL answers that question wrongly, and the hashes alongside it lend the
  wrong answer false authority.
* The internal feed is **credentialed and unreachable** from anywhere else. A
  consumer -- the Dockerfile's frozen `uv sync`, a vendoring step, an SBOM
  generator -- then breaks for contributors and CI runners,
  with an error that points at a host most readers will not recognise.
* It leaks internal infrastructure identifiers (feed GUIDs, org names) into a
  public repository.

If you regenerate the lockfile behind a corporate mirror, re-run `uv lock` with
`UV_DEFAULT_INDEX=https://pypi.org/simple` and
`uv lock --default-index https://pypi.org/simple` (or on an unproxied network) before
committing.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
LOCKFILE = REPO / "app" / "api" / "uv.lock"
NPM_LOCKFILE = REPO / "app" / "web" / "package-lock.json"

# The only hosts a public Python lockfile should name: PyPI's index and the
# canonical artifact CDN it redirects to.
ALLOWED_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})

_URL_HOST = re.compile(r'https?://([^/"\s]+)')
_REGISTRY = re.compile(r'registry = "([^"]+)"')


def _hosts() -> Counter[str]:
    return Counter(_URL_HOST.findall(LOCKFILE.read_text(encoding="utf-8")))


def _npm_artifact_errors(packages: dict) -> list[str]:
    errors = []
    for location, entry in packages.items():
        if location == "":
            continue
        if not isinstance(entry, dict):
            errors.append(f"{location}: invalid package record")
            continue
        resolved = entry.get("resolved")
        try:
            url = urlsplit(resolved) if isinstance(resolved, str) else None
        except ValueError:
            url = None
        if (
            url is None or url.scheme != "https" or url.netloc != "registry.npmjs.org"
            or url.query or url.fragment or not url.path.endswith(".tgz")
        ):
            errors.append(f"{location}: nonpublic or missing npm artifact")
        integrity = entry.get("integrity")
        try:
            digest = (
                base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
                if isinstance(integrity, str) and integrity.startswith("sha512-")
                else b""
            )
        except (ValueError, binascii.Error):
            digest = b""
        if len(digest) != 64:
            errors.append(f"{location}: missing or malformed SHA-512 integrity")
    return errors


class LockfileResolvesFromPublicPyPI(unittest.TestCase):
    def test_every_url_points_at_pypi_or_its_cdn(self) -> None:
        foreign = {h: n for h, n in _hosts().items() if h not in ALLOWED_HOSTS}
        self.assertEqual(
            foreign,
            {},
            "app/api/uv.lock references hosts that are not public PyPI: "
            f"{sorted(foreign)}. This usually means `uv lock` was run behind a "
            "corporate package mirror, which rewrites every artifact URL. "
            "Regenerate with UV_DEFAULT_INDEX=https://pypi.org/simple and "
            "uv lock --default-index https://pypi.org/simple (or off the "
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


class LockfileResolvesFromPublicNpm(unittest.TestCase):
    def test_every_package_has_public_artifact_and_strong_integrity(self) -> None:
        document = json.loads(NPM_LOCKFILE.read_text(encoding="utf-8"))
        self.assertEqual(document["lockfileVersion"], 3)
        packages = document["packages"]
        self.assertGreater(
            len(packages), 400,
            "Too few npm package records: a truncated lock must not pass provenance.",
        )
        self.assertIn("node_modules/next", packages)
        self.assertIn("node_modules/remark-gfm", packages)
        errors = _npm_artifact_errors(packages)
        self.assertEqual(
            errors, [],
            "Repair npm metadata using the public registry and verified artifact "
            f"hashes, without changing locked versions. First errors: {errors[:8]}",
        )

    def test_guard_rejects_foreign_urls_missing_artifacts_and_weak_integrity(self) -> None:
        location = "node_modules/example"
        public = {
            "version": "1.0.0",
            "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
            "integrity": "sha512-" + base64.b64encode(bytes(range(64))).decode("ascii"),
        }
        self.assertEqual(_npm_artifact_errors({location: public}), [])
        for change in (
            {"resolved": "https://mirror.invalid/example.tgz"},
            {"resolved": "http://registry.npmjs.org/example.tgz"},
            {"resolved": "https://registry.npmjs.org:443/example.tgz"},
            {"resolved": "https://user@registry.npmjs.org/example.tgz"},
            {"resolved": "https://registry.npmjs.org/example.tgz?token=fixture"},
            {"resolved": None},
            {"resolved": "https://[broken"},
            {"integrity": "sha1-" + base64.b64encode(bytes(range(20))).decode("ascii")},
            {"integrity": "sha512-invalid"},
            {"integrity": None},
        ):
            with self.subTest(change=change):
                self.assertTrue(_npm_artifact_errors({location: {**public, **change}}))
                self.assertEqual(_npm_artifact_errors({location: public}), [])
        self.assertTrue(_npm_artifact_errors({location: None}))


if __name__ == "__main__":
    unittest.main()
