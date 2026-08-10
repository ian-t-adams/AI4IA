"""Machine-verifiable provenance for the vendored SimpleL7Proxy source."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen-proxy-provenance.py"
MANIFEST = ROOT / "proxy" / "upstream-provenance.json"

spec = importlib.util.spec_from_file_location("proxy_provenance", GENERATOR)
assert spec and spec.loader
proxy_provenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy_provenance)


class ProxyProvenanceTests(unittest.TestCase):
    def test_manifest_matches_every_local_vendored_file(self) -> None:
        self.assertEqual(proxy_provenance.check(), [])

    def test_measured_dispositions_are_not_vacuous(self) -> None:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            document["counts"],
            {
                "ai4ia-added": 4,
                "ai4ia-patched": 7,
                "line-ending-only": 138,
                "upstream-identical": 29,
            },
        )
        self.assertEqual(len(document["files"]), 178)
        self.assertEqual(
            {patch["path"] for patch in document["patches"]},
            set(proxy_provenance.AI4IA_PATCH_REASONS),
        )

    def test_pin_is_shared_by_wrapper_and_documentation(self) -> None:
        pin = proxy_provenance.UPSTREAM_COMMIT
        readme = (ROOT / "proxy" / "README.md").read_text(encoding="utf-8")
        dockerfile = (ROOT / "proxy" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(pin, readme)
        self.assertIn("upstream-provenance.json", dockerfile)

    def test_generation_rejects_a_ref_that_is_not_the_pinned_commit(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected pinned upstream commit"):
            proxy_provenance._validated_upstream_files("HEAD")

    def test_generation_reads_files_only_after_exact_oid_validation(self) -> None:
        sentinel = {"Shared/example.cs": b"content"}
        with (
            mock.patch.object(
                proxy_provenance,
                "_resolve_commit",
                return_value=proxy_provenance.UPSTREAM_COMMIT,
            ) as resolve,
            mock.patch.object(
                proxy_provenance,
                "_upstream_files",
                return_value=sentinel,
            ) as read_files,
        ):
            self.assertIs(
                proxy_provenance._validated_upstream_files("FETCH_HEAD"),
                sentinel,
            )
        resolve.assert_called_once_with("FETCH_HEAD")
        read_files.assert_called_once_with("FETCH_HEAD")


if __name__ == "__main__":
    unittest.main()
