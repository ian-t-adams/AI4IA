from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
generator = load_script("voice_migration_docs", ROOT / "scripts/gen-voice-migration-docs.py")


class VoiceMigrationDocsTests(unittest.TestCase):
    def test_public_dates_and_document_share_the_exact_parser_evidence(self):
        document = generator.TARGET.read_text(encoding="utf-8")
        self.assertEqual(generator.render_document(document), document)
        references = generator.render_references()
        data = json.loads(generator.SOURCE.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 4)
        for record in data:
            self.assertIn(record["name"], references)
            self.assertIn(record["version"], references)
            self.assertIn(record["date"] + "T00:00:00Z", references)
            self.assertIn(record["observed_at"], references)
        self.assertIn("not live subscription or inference evidence", references)
        # Never overwrite the independent full-inventory report section.
        marker = generator.retirement.DOC_START
        self.assertEqual(
            generator.render_document(document).split(marker)[1], document.split(marker)[1],
        )

    def test_changing_a_public_date_changes_only_its_generated_reference(self):
        data = json.loads(generator.SOURCE.read_text(encoding="utf-8"))
        before = generator.render_references()
        data[0]["date"] = "2026-09-01"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "public.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            changed = generator.render_references(path)
            self.assertEqual(changed, before.replace("2026-08-31T00:00:00Z", "2026-09-01T00:00:00Z"))
            data[0]["date"] = "2026-09-01T00:00:00"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                generator.render_references(path)

    def test_reference_scope_covers_the_desired_ga_versions(self):
        source = json.loads((ROOT / "infra/models.json").read_text(encoding="utf-8"))
        references = json.loads(generator.SOURCE.read_text(encoding="utf-8"))
        recorded = {(r["name"], r["version"], r["region"], r["sku"]) for r in references}
        for model in source["catalog"]:
            if model["name"] not in {"gpt-realtime-1.5", "gpt-4o-mini-tts"}:
                continue
            for deployment in model["deployments"]:
                self.assertIn((
                    model["name"], deployment["version"], deployment["region"], deployment["sku"],
                ), recorded)


if __name__ == "__main__":
    unittest.main()
