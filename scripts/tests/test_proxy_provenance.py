"""Machine-verifiable provenance for the vendored SimpleL7Proxy source."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen-proxy-provenance.py"
MANIFEST = ROOT / "proxy" / "upstream-provenance.json"

proxy_provenance = load_script("proxy_provenance", GENERATOR)


class ProxyProvenanceTests(unittest.TestCase):
    def test_manifest_matches_every_local_vendored_file(self) -> None:
        self.assertEqual(proxy_provenance.check(), [])

    def test_measured_dispositions_are_not_vacuous(self) -> None:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            document["counts"],
            {
                "ai4ia-added": 4,
                "ai4ia-patched": 14,
                "upstream-equivalent": 160,
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

    def test_local_lf_and_crlf_have_the_same_canonical_hash(self) -> None:
        lf = b"first line\nsecond line\n"
        crlf = b"first line\r\nsecond line\r\n"
        self.assertEqual(
            proxy_provenance._canonical_sha256(lf),
            proxy_provenance._canonical_sha256(crlf),
        )

    def test_check_accepts_the_same_tracked_text_with_other_line_endings(self) -> None:
        local = proxy_provenance._local_files()
        path = "SimpleL7Proxy/config.json"
        alternate = dict(local)
        canonical = proxy_provenance._canonicalize(local[path])
        alternate[path] = canonical.replace(b"\n", b"\r\n")
        with mock.patch.object(
            proxy_provenance,
            "_local_files",
            return_value=alternate,
        ):
            self.assertEqual(proxy_provenance.check(), [])

    def test_check_rejects_a_semantic_local_change(self) -> None:
        local = proxy_provenance._local_files()
        path = "SimpleL7Proxy/config.json"
        changed = dict(local)
        self.assertIn(b'"userId"', changed[path])
        changed[path] = changed[path].replace(b'"userId"', b'"userID"', 1)
        with mock.patch.object(
            proxy_provenance,
            "_local_files",
            return_value=changed,
        ):
            self.assertIn(
                f"{path}: local canonical SHA-256 drift",
                proxy_provenance.check(),
            )


if __name__ == "__main__":
    unittest.main()
