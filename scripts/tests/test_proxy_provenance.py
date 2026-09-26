"""Machine-verifiable provenance for the vendored SimpleL7Proxy source."""

from __future__ import annotations

import json
import tempfile
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
                "ai4ia-added": 7,
                "ai4ia-excluded": 95,
                "ai4ia-patched": 22,
                "upstream-equivalent": 292,
            },
        )
        self.assertEqual(len(document["files"]), 416)
        self.assertEqual(
            {patch["path"] for patch in document["patches"]},
            set(proxy_provenance.AI4IA_PATCH_REASONS),
        )
        self.assertEqual(
            {entry["rule"] for entry in document["exclusions"]},
            set(proxy_provenance.AI4IA_EXCLUSION_REASONS),
        )

    def test_excluded_upstream_file_present_locally_is_rejected(self) -> None:
        local = proxy_provenance._local_files()
        excluded = "CompanionApp/Components/Pages/UrlTesterPage.razor"
        self.assertNotIn(excluded, local)
        with mock.patch.object(
            proxy_provenance, "_local_files", return_value={**local, excluded: b"@page \"/url-tester\"\n"},
        ):
            errors = proxy_provenance.check()
        self.assertIn(f"{excluded}: excluded upstream file is present locally", errors)
        # Control: the unmodified tree has no such error.
        self.assertEqual(proxy_provenance.check(), [])

    def test_exclusion_without_a_reviewed_rule_is_rejected(self) -> None:
        rules = dict(proxy_provenance.AI4IA_EXCLUSION_REASONS)
        removed = rules.pop("CompanionApp/Components/Pages/UrlTesterPage.razor")
        self.assertTrue(removed)
        with mock.patch.object(proxy_provenance, "AI4IA_EXCLUSION_REASONS", rules):
            errors = proxy_provenance.check()
        self.assertIn(
            "CompanionApp/Components/Pages/UrlTesterPage.razor: "
            "exclusion is not declared by the reviewed AI4IA rules",
            errors,
        )
        self.assertIn(
            "explicit exclusion list does not match the reviewed AI4IA exclusions", errors,
        )

    def test_unused_exclusion_rule_is_rejected(self) -> None:
        rules = {**proxy_provenance.AI4IA_EXCLUSION_REASONS, "CompanionApp/no-such-file": "stale"}
        with mock.patch.object(proxy_provenance, "AI4IA_EXCLUSION_REASONS", rules):
            errors = proxy_provenance.check()
        self.assertIn("exclusion rules match no recorded file: ['CompanionApp/no-such-file']", errors)

    def _check_with_vendored_entry(self, path: str, data: bytes) -> list[str]:
        """check() after vendoring ``data`` at ``path`` and recording it as upstream-equivalent.

        Every other invariant is kept consistent (hashes, counts), so the only thing
        that can fail is whether a reviewed exclusion rule matches ``path``.
        """
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
        previous = document["files"].get(path, {}).get("disposition")
        digest = proxy_provenance._canonical_sha256(data)
        document["files"][path] = {
            "localCanonicalSha256": digest,
            "upstreamRawSha256": proxy_provenance._sha256(data),
            "upstreamCanonicalSha256": digest,
            "disposition": "upstream-equivalent",
        }
        counts = document["counts"]
        if previous is not None:
            counts[previous] -= 1
        counts["upstream-equivalent"] += 1
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "upstream-provenance.json"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            local = {**proxy_provenance._local_files(), path: data}
            with (
                mock.patch.object(proxy_provenance, "MANIFEST", manifest),
                mock.patch.object(proxy_provenance, "_local_files", return_value=local),
            ):
                return proxy_provenance.check()

    def test_readded_file_under_a_multi_file_exclusion_rule_is_rejected(self) -> None:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for path, rule in (
            ("CompanionApp/chat-models.json", "CompanionApp/chat-models*"),
            ("CompanionApp/Components/Pages/DeploymentSetupPage.razor",
             "CompanionApp/Components/Pages/Deployment*"),
        ):
            with self.subTest(path=path):
                # The rule still matches another recorded file, so "unused rule" cannot fire.
                siblings = [
                    other for other, entry in document["files"].items()
                    if other != path and entry.get("rule") == rule
                ]
                self.assertTrue(siblings, f"{rule} must cover more than one file")
                errors = self._check_with_vendored_entry(path, b'{"fixture": true}\n')
                self.assertEqual(errors, [f"{path}: vendored file matches exclusion rule {rule!r}"])

    def test_the_same_flip_on_an_unexcluded_file_passes(self) -> None:
        # Control: the identical manifest rewrite of a path no rule matches is accepted.
        path = "CompanionApp/wwwroot/app.css"
        self.assertIsNone(proxy_provenance._exclusion_rule(path))
        data = proxy_provenance._local_files()[path]
        self.assertEqual(self._check_with_vendored_entry(path, data), [])

    def test_generation_rejects_undeclared_missing_and_present_excluded_files(self) -> None:
        local = proxy_provenance._local_files()
        upstream = {path: data for path, data in local.items() if path.startswith("Shared/")}
        with (
            mock.patch.object(proxy_provenance, "_validated_upstream_files",
                              return_value={**upstream, "CompanionApp/new-upstream.cs": b"x"}),
            mock.patch.object(proxy_provenance, "_local_files", return_value=local),
        ):
            with self.assertRaisesRegex(ValueError, "upstream files are missing locally"):
                proxy_provenance.generate("FETCH_HEAD")
        present = "CompanionApp/event.json"
        with (
            mock.patch.object(proxy_provenance, "_validated_upstream_files",
                              return_value={**upstream, present: b"{}"}),
            mock.patch.object(proxy_provenance, "_local_files", return_value={**local, present: b"{}"}),
        ):
            with self.assertRaisesRegex(ValueError, "excluded upstream files are present locally"):
                proxy_provenance.generate("FETCH_HEAD")

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
