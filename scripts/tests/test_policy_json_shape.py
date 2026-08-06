"""No APIM policy may construct a JObject with a duplicate property name.

This is the class of bug that took the whole model plane down on 2026-08-05.
`render_catalog` labelled each backend with its region, DataZoneStandard
deployments gave 28 model/region pairs two deployments in the same region, and
the generated catalog emitted `EASTUS2` twice into one `JObject`. Newtonsoft
throws

    Can not add property EASTUS2 to Newtonsoft.Json.Linq.JObject.
    Property with the same name already exists on object.

...at **request** time, so APIM answered 500 (`ExpressionValueEvaluationFailure`)
for every chat and embedding call until the policy was fixed and redeployed.

Nothing caught it, and the reasons generalise:

* `gen-gateway-policy.py --check` only proves the generated file matches its
  source. Both were equally wrong.
* The policy is **syntactically valid C#**, so compiling it -- which is all the
  APIM policy compiler harness does -- succeeds. The duplicate is a *runtime*
  exception from the JSON library, not a compile error.

So the guard has to be structural, over the committed policy text. `#285` added a
narrow version (backend labels, inside the generator's own in-memory output).
This is the general one: every `JObject` in every committed policy file,
generated or hand-written, at every nesting depth.

Two construction forms are in use and **both** must be parsed:

1. ``new JObject(new JProperty("a", ...), new JProperty("b", ...))``
2. ``new JObject { { "a", ... }, { "b", ... } }``  -- collection initialiser

Form 2 appears only in hand-written fragments (`simplel7proxy_backend_32`,
`_on_error_32`, `_outbound_32`, `simplel7proxy-priority-retry`), which run on
*every* request. A parser that understood only form 1 would report a confident
zero while ignoring 40 constructions on the hottest path in the system, so
`test_every_construction_is_parsed` fails if any occurrence goes unclaimed.
"""

from __future__ import annotations

import html
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = ROOT / "infra" / "policies"

_NEW_JOBJECT = re.compile(r"new\s+JObject")
_OPENS = re.compile(r"new\s+JObject\s*([({])")
_JPROPERTY = re.compile(r'new\s+JProperty\s*\(\s*"([^"]*)"')
_INITIALIZER_KEY = re.compile(r'\{\s*"([^"]*)"')

_CLOSE_FOR = {"(": ")", "{": "}"}


def _policy_files() -> list[Path]:
    return sorted(POLICY_DIR.glob("*.xml"))


def _decoded(path: Path) -> str:
    """Policy XML holds C# as escaped text; decode before parsing it as code."""
    return html.unescape(path.read_text(encoding="utf-8"))


def _span_end(text: str, start: int, opener: str) -> int | None:
    """Index of the bracket closing the one at ``start``, or None if unbalanced.

    String-aware: a bracket inside a C# string literal (URLs, JSON snippets,
    interpolated messages) must not move the depth.
    """
    closer = _CLOSE_FOR[opener]
    depth = 0
    in_string = False
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _direct_keys(body: str, opener: str) -> list[str]:
    """Property names that are *direct* children of one JObject.

    Nested objects are excluded: a name repeated at a different depth is a
    different object and is legal.
    """
    keys: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    index = 0
    while index < len(body):
        char = body[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if depth == 0:
            if opener == "(":
                match = _JPROPERTY.match(body, index)
                if match:
                    keys.append(match.group(1))
            else:
                match = _INITIALIZER_KEY.match(body, index)
                if match:
                    keys.append(match.group(1))
        if char == '"':
            in_string = True
        elif char in "({":
            depth += 1
        elif char in ")}":
            depth -= 1
        index += 1
    return keys


def _constructions(text: str) -> list[tuple[int, str, str]]:
    """(start, opener, body) for every JObject construction in ``text``."""
    found: list[tuple[int, str, str]] = []
    for match in _OPENS.finditer(text):
        opener = match.group(1)
        open_at = match.end() - 1
        end = _span_end(text, open_at, opener)
        if end is None:
            continue
        found.append((match.start(), opener, text[open_at + 1 : end]))
    return found


class PolicyJsonShapeTests(unittest.TestCase):
    def test_there_are_policies_to_check(self) -> None:
        """Non-vacuity: a glob that stops matching must not read as success."""
        self.assertGreater(len(_policy_files()), 10)

    def test_every_construction_is_parsed(self) -> None:
        """Every `new JObject` must be claimed by the parser.

        Without this, a construction form the parser does not understand -- or a
        span it fails to bracket-match -- is silently skipped, and the suite
        reports a confident zero over code it never looked at. That is precisely
        how the first draft of this file missed 40 collection-initialiser
        objects in the hand-written fragments.
        """
        for path in _policy_files():
            text = _decoded(path)
            occurrences = len(_NEW_JOBJECT.findall(text))
            parsed = len(_constructions(text))
            self.assertEqual(
                parsed,
                occurrences,
                f"{path.name}: parsed {parsed} of {occurrences} JObject "
                "constructions; the unparsed ones are unchecked",
            )

    def test_no_jobject_has_duplicate_property_names(self) -> None:
        offenders: list[str] = []
        checked = 0
        for path in _policy_files():
            text = _decoded(path)
            for start, opener, body in _constructions(text):
                checked += 1
                keys = _direct_keys(body, opener)
                duplicates = sorted({k for k in keys if keys.count(k) > 1})
                if duplicates:
                    line = text.count("\n", 0, start) + 1
                    offenders.append(f"{path.name}:{line}: duplicate {duplicates}")
        self.assertGreater(checked, 200, "parser found far fewer objects than expected")
        self.assertEqual(
            offenders,
            [],
            "A duplicate property name makes Newtonsoft throw when the policy "
            "expression runs, so APIM returns 500 for every request through the "
            "API. This is a runtime failure that compiles cleanly:\n"
            + "\n".join(offenders),
        )

    def test_parser_detects_both_construction_forms(self) -> None:
        """Guards the guard: prove each form is understood, not assumed."""
        paren = 'new JObject(new JProperty("a", 1), new JProperty("a", 2))'
        brace = 'new JObject { { "a", 1 }, { "a", 2 } }'
        for source in (paren, brace):
            constructions = _constructions(source)
            self.assertEqual(len(constructions), 1, source)
            _, opener, body = constructions[0]
            self.assertEqual(_direct_keys(body, opener), ["a", "a"], source)

    def test_parser_ignores_nested_and_quoted_occurrences(self) -> None:
        """A repeated name at a *different* depth is legal and must not flag."""
        nested = (
            'new JObject(new JProperty("outer", new JObject('
            'new JProperty("dup", 1))), new JProperty("other", new JObject('
            'new JProperty("dup", 2))))'
        )
        _, opener, body = _constructions(nested)[0]
        self.assertEqual(_direct_keys(body, opener), ["outer", "other"])

        # A bracket inside a string literal must not shift the depth.
        quoted = 'new JObject(new JProperty("url", "https://x/y(z)"), new JProperty("b", 1))'
        _, opener, body = _constructions(quoted)[0]
        self.assertEqual(_direct_keys(body, opener), ["url", "b"])


if __name__ == "__main__":
    unittest.main()
