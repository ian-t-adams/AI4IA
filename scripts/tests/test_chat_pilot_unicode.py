"""Offline Unicode-data and native-parser controls; no frontend execution."""
from __future__ import annotations

import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.tests._loader import load_script

ROOT = Path(__file__).resolve().parents[2]
generator = load_script("chat_pilot_unicode_generator", ROOT / "scripts" / "gen-chat-pilot-unicode.py")
commands = load_script(
    "chat_pilot_unicode_commands",
    ROOT / "app" / "api" / "src" / "ai4ia_api" / "agents" / "commands.py",
    register=True,
)


class ChatPilotUnicodeTests(unittest.TestCase):
    def test_committed_matcher_tracks_api_python_and_unicode(self) -> None:
        dockerfile = (ROOT / "app" / "api" / "Dockerfile").read_text(encoding="utf-8")
        versions = re.findall(r"^FROM python:(\d+\.\d+)-", dockerfile, re.M)
        self.assertTrue(versions)
        self.assertEqual(set(versions), {".".join(map(str, generator.PYTHON_VERSION))})
        self.assertEqual(generator.main(["--check"]), 0)

    def test_literal_ranges_match_native_word_characters_exhaustively(self) -> None:
        source = generator.TARGET.read_text(encoding="utf-8")
        represented = bytearray(0x110000)
        ranges = re.findall(r"\\u\{([0-9a-f]+)\}(?:-\\u\{([0-9a-f]+)\})?", source)
        for first, last in ranges:
            start, end = int(first, 16), int(last or first, 16)
            represented[start:end + 1] = b"\1" * (end - start + 1)
        native_word = re.compile(r"\w")
        mismatch = next(
            (
                codepoint
                for codepoint in range(0x110000)
                if represented[codepoint] != bool(native_word.fullmatch(chr(codepoint)))
            ),
            None,
        )
        self.assertIsNone(mismatch, "Generated literal differs from Python re \\w.")

    def test_refuses_a_different_unicode_database(self) -> None:
        with patch.object(generator.unicodedata, "unidata_version", "15.1.0"):
            with self.assertRaisesRegex(ValueError, "Requires the API's Python 3.12"):
                generator.render()

    def test_check_refuses_changed_ranges_without_writing(self) -> None:
        original = generator.TARGET.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "word.ts"
            target.write_bytes(original)
            with patch.object(generator, "TARGET", target):
                self.assertEqual(generator.main(["--check"]), 0)
                mutated = original.replace(b"\\u{30}-\\u{39}", b"\\u{30}-\\u{38}", 1)
                self.assertNotEqual(mutated, original)
                target.write_bytes(mutated)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(generator.main(["--check"]), 1)
                self.assertEqual(target.read_bytes(), mutated)

    def test_native_parser_version_boundary_and_ordinary_unicode(self) -> None:
        cases = [
            ("@agent\U0002ebf0 hello", "agent"),
            ("@agent\u1c89 hello", "agent"),
            ("@agent\U00031350 hello", None),
            ("@agent\U00020000 hello", None),
            ("@agent\u0661 hello", None),
            ("@agent\u0301 hello", "agent"),
            ("@café", None),
            ("@agenté hello", None),
        ]
        for content, agent in cases:
            with self.subTest(content=content):
                parsed = commands.parse_input(content)
                self.assertEqual(parsed.agent, agent)
                self.assertIsNone(parsed.command)


if __name__ == "__main__":
    unittest.main()
