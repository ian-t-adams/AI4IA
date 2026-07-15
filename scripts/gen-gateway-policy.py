#!/usr/bin/env python3
"""Generate APIM model and realtime routing from the authoritative catalog."""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = ROOT / "infra" / "models.json"
TEMPLATE_PATH = ROOT / "infra" / "policies" / "simplel7proxy-endpoints.template.xml"
OUTPUT_PATH = ROOT / "infra" / "policies" / "simplel7proxy-endpoints.xml"
POLICY_FRAGMENT_GENERATION = 33
POLICY_GENERATION_MANIFEST_PATH = (
    ROOT / "infra" / "policies" / "policy-fragment-generation.json"
)
CATALOG_FRAGMENT_COUNT = 10
CATALOG_OUTPUT_PATHS = tuple(
    ROOT / "infra" / "policies" / f"simplel7proxy-endpoints-catalog-{index}.xml"
    for index in range(CATALOG_FRAGMENT_COUNT)
)
CATALOG_FRAGMENT_IDS = tuple(
    f"endpoint_selection_catalog_{index}_{POLICY_FRAGMENT_GENERATION}"
    for index in range(CATALOG_FRAGMENT_COUNT)
)
SETUP_FRAGMENT_ID = f"endpoint_selection_setup_{POLICY_FRAGMENT_GENERATION}"
PRIORITY_POLICY_PATH = (
    ROOT / "infra" / "policies" / "simplel7proxy-priority-retry.xml"
)
PRIORITY_TEMPLATE_PATH = (
    ROOT / "infra" / "policies" / "simplel7proxy-priority-retry.template.xml"
)
PRIORITY_FRAGMENT_COUNT = 10
PRIORITY_OUTPUT_PATHS = tuple(
    ROOT / "infra" / "policies" / f"simplel7proxy-priority-fragment-{index}.xml"
    for index in range(PRIORITY_FRAGMENT_COUNT)
)
PRIORITY_FRAGMENT_IDS = tuple(
    f"priority_policy_{index}_{POLICY_FRAGMENT_GENERATION}"
    for index in range(PRIORITY_FRAGMENT_COUNT)
)
REALTIME_OUTPUT_PATH = ROOT / "infra" / "policies" / "realtime-routing.xml"
CATALOG_MARKER = "__AI4IA_BACKEND_CATALOG_MERGE__"
ATTEMPTS_MARKER = "__AI4IA_MAX_IMMEDIATE_ATTEMPTS__"
# APIM rejects a single decoded policy expression at the 32 KiB boundary.
APIM_EXPRESSION_MAX_CHARS = 32_768
# Production ARM validation rejects policy fragment payloads above 16 KiB even
# though the live fragment compiler accepts larger files. Leave 2 KiB headroom.
APIM_POLICY_FRAGMENT_MAX_BYTES = 14 * 1024
APIM_POLICY_DOCUMENT_MAX_BYTES = 14 * 1024
ATTRIBUTE_PATTERN = re.compile(
    r"(?P<name>[\w:.-]+)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
XML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
JOBJECT_INDEX_INITIALIZER_PATTERN = re.compile(
    r"new\s+JObject\s*\{\s*\[",
    re.DOTALL,
)


def deployment_name(
    *,
    model: str,
    subscription_token: str,
    region: str,
    sku: str,
    sku_short: dict[str, str],
) -> str:
    return f"{model}-{subscription_token}-{region}-{sku_short[sku]}"


def timeout_seconds(category: str) -> int:
    if category in {"image", "video"}:
        return 240
    return 120


def backend_row(
    *,
    label: str,
    region: str,
    deployment: str,
    priority: int,
    timeout: int,
) -> str:
    named_value = f"{{{{foundry-{region}-endpoint}}}}"
    return (
        f'                new JProperty("{label}", new JObject(\n'
        f'                    new JProperty("url", "{named_value}"),\n'
        '                    new JProperty("path", "openai"),\n'
        f'                    new JProperty("deployment", "{deployment}"),\n'
        f'                    new JProperty("priority", {priority}),\n'
        '                    new JProperty("acceptablePriorities", "1, 2, 3"),\n'
        f'                    new JProperty("timeout", {timeout}),\n'
        '                    new JProperty("bufferResponse", false),\n'
        '                    new JProperty("auth", "MI")\n'
        "                ))"
    )


def render_catalog(models: dict[str, Any]) -> tuple[list[str], int]:
    naming = models["naming"]
    subscription_token = naming["subscriptionToken"]
    sku_short = naming["skuShort"]
    blocks: list[str] = []
    max_attempts = 1

    for model in models["catalog"]:
        deployments = model["deployments"]
        max_attempts = max(max_attempts, len(deployments))
        timeout = timeout_seconds(model["category"])
        resolved = [
            {
                "region": deployment["region"],
                "name": deployment_name(
                    model=model["name"],
                    subscription_token=subscription_token,
                    region=deployment["region"],
                    sku=deployment["sku"],
                    sku_short=sku_short,
                ),
            }
            for deployment in deployments
        ]

        for requested in resolved:
            ordered = sorted(
                resolved,
                key=lambda candidate: candidate["region"] != requested["region"],
            )
            rows = [
                backend_row(
                    label=candidate["region"].upper(),
                    region=candidate["region"],
                    deployment=candidate["name"],
                    priority=1 if candidate["region"] == requested["region"] else 2,
                    timeout=timeout,
                )
                for candidate in ordered
            ]
            block = (
                f'            new JProperty("{requested["name"]}", new JObject(\n'
                + ",\n".join(rows)
                + "\n            ))"
            )
            blocks.append(block)

    if not blocks:
        raise ValueError("infra/models.json contains no model deployments")
    return blocks, max_attempts


def catalog_expression(blocks: list[str]) -> str:
    if not blocks:
        return "@{\n        return new JObject();\n    }"
    return (
        "@{\n"
        "        return new JObject(\n"
        + ",\n\n".join(blocks)
        + "\n"
        "        );\n"
        "    }"
    )


def render_catalog_fragment(index: int, blocks: list[str]) -> str:
    name = f"backendCatalogChunk{index}"
    expression = catalog_expression(blocks)
    return (
        "<fragment>\n"
        f'    <set-variable name="{name}" '
        f'value="{html.escape(expression, quote=True)}" />\n'
        "</fragment>\n"
    )


def policy_payload_bytes(xml_text: str) -> int:
    return len(xml_text.encode("utf-8"))


def policy_payload_bytes_with_crlf(xml_text: str) -> int:
    normalized = xml_text.replace("\r\n", "\n").replace("\n", "\r\n")
    return policy_payload_bytes(normalized)


def policy_payload_split_bytes(xml_text: str) -> int:
    return max(
        policy_payload_bytes(xml_text),
        policy_payload_bytes_with_crlf(xml_text),
    )


def policy_generation_digest(
    outputs: tuple[tuple[Path, str], ...],
) -> str:
    digest = hashlib.sha256()
    for path, content in outputs:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def validate_generation_manifest(
    generated_digest: str,
    *,
    check: bool,
) -> bool:
    if not POLICY_GENERATION_MANIFEST_PATH.exists():
        if check:
            raise ValueError("policy fragment generation manifest is missing")
        return True

    manifest = json.loads(
        POLICY_GENERATION_MANIFEST_PATH.read_text(encoding="utf-8")
    )
    manifest_generation = manifest.get("generation")
    manifest_digest = manifest.get("sha256")
    if not isinstance(manifest_generation, int) or not isinstance(
        manifest_digest, str
    ):
        raise ValueError("policy fragment generation manifest is invalid")
    if manifest_generation > POLICY_FRAGMENT_GENERATION:
        raise ValueError(
            "policy fragment generation cannot move backwards"
        )
    if manifest_generation == POLICY_FRAGMENT_GENERATION:
        if manifest_digest != generated_digest:
            raise ValueError(
                f"immutable policy fragment generation "
                f"{POLICY_FRAGMENT_GENERATION} changed; increment "
                "POLICY_FRAGMENT_GENERATION and update Bicep/harness IDs"
            )
        return False
    if check:
        raise ValueError(
            "policy fragment generation manifest is stale; regenerate policies"
        )
    return True


def chunk_catalog(blocks: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []

    for block in blocks:
        candidate = current + [block]
        candidate_fragment = render_catalog_fragment(len(chunks), candidate)
        if (
            current
            and policy_payload_split_bytes(candidate_fragment)
            > APIM_POLICY_FRAGMENT_MAX_BYTES
        ):
            chunks.append(current)
            current = [block]
        else:
            current = candidate

        single_expression = catalog_expression(current)
        if len(single_expression) >= APIM_EXPRESSION_MAX_CHARS:
            raise ValueError("a generated backend catalog entry exceeds APIM limits")
        current_fragment = render_catalog_fragment(len(chunks), current)
        if (
            policy_payload_split_bytes(current_fragment)
            > APIM_POLICY_FRAGMENT_MAX_BYTES
        ):
            raise ValueError(
                "a generated backend catalog entry exceeds the APIM policy "
                "fragment deployment limit"
            )

    if current:
        chunks.append(current)
    return chunks


def render_catalog_fragments(blocks: list[str]) -> tuple[tuple[str, ...], str]:
    chunks = chunk_catalog(blocks)
    if len(chunks) > CATALOG_FRAGMENT_COUNT:
        raise ValueError(
            f"backend catalog requires {len(chunks)} fragments; "
            f"only {CATALOG_FRAGMENT_COUNT} are configured"
        )
    chunks.extend([[] for _ in range(CATALOG_FRAGMENT_COUNT - len(chunks))])

    fragments: list[str] = []
    chunk_names: list[str] = []
    for index, chunk in enumerate(chunks):
        name = f"backendCatalogChunk{index}"
        chunk_names.append(name)
        fragments.append(render_catalog_fragment(index, chunk))

    merge_lines = [
        "        JObject catalog = new JObject();",
        *[
            "        catalog.Merge((JObject)context.Variables"
            f'["{name}"]);'
            for name in chunk_names
        ],
        "        return catalog;",
    ]
    merge_expression = "@{\n" + "\n".join(merge_lines) + "\n    }"
    merge_variable = (
        '    <set-variable name="backendCatalog" '
        f'value="{html.escape(merge_expression, quote=True)}" />'
    )
    return tuple(fragments), merge_variable


def _xml_parser() -> ElementTree.XMLParser:
    return ElementTree.XMLParser(
        target=ElementTree.TreeBuilder(insert_comments=True)
    )


def _serialize_xml(element: ElementTree.Element) -> str:
    ElementTree.indent(element, space="  ")
    return ElementTree.tostring(
        element,
        encoding="unicode",
        short_empty_elements=True,
    ) + "\n"


def _render_priority_fragment(
    children: list[ElementTree.Element],
) -> str:
    fragment = ElementTree.Element("fragment")
    for child in children:
        cloned = copy.deepcopy(child)
        cloned.tail = None
        fragment.append(cloned)
    return _serialize_xml(fragment)


def _append_priority_chunks(
    children: list[ElementTree.Element],
    shell_parent: ElementTree.Element,
    fragments: list[str],
) -> None:
    current: list[ElementTree.Element] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        if len(fragments) >= PRIORITY_FRAGMENT_COUNT:
            raise ValueError(
                "priority policy requires more configured fragment slots"
            )
        fragment = _render_priority_fragment(current)
        if policy_payload_split_bytes(fragment) > APIM_POLICY_FRAGMENT_MAX_BYTES:
            raise ValueError(
                "a priority policy statement exceeds the APIM policy fragment "
                "deployment limit"
            )
        fragment_id = PRIORITY_FRAGMENT_IDS[len(fragments)]
        fragments.append(fragment)
        ElementTree.SubElement(
            shell_parent,
            "include-fragment",
            {"fragment-id": fragment_id},
        )
        current = []

    for child in children:
        candidate = [*current, child]
        if (
            current
            and policy_payload_split_bytes(_render_priority_fragment(candidate))
            > APIM_POLICY_FRAGMENT_MAX_BYTES
        ):
            flush()
            candidate = [child]
        current = candidate
        if (
            policy_payload_split_bytes(_render_priority_fragment(current))
            > APIM_POLICY_FRAGMENT_MAX_BYTES
        ):
            raise ValueError(
                "a priority policy statement exceeds the APIM policy fragment "
                "deployment limit"
            )
    flush()


def _append_priority_sequence(
    children: list[ElementTree.Element],
    shell_parent: ElementTree.Element,
    fragments: list[str],
) -> None:
    pending: list[ElementTree.Element] = []
    for child in children:
        shell_only = (
            isinstance(child.tag, str)
            and child.tag in {"base", "include-fragment"}
        )
        if not shell_only:
            pending.append(child)
            continue
        _append_priority_chunks(pending, shell_parent, fragments)
        pending = []
        cloned = copy.deepcopy(child)
        cloned.tail = None
        shell_parent.append(cloned)
    _append_priority_chunks(pending, shell_parent, fragments)


def rewrite_csharp_line_comments(expression: str) -> str:
    result: list[str] = []
    state = "code"
    index = 0

    while index < len(expression):
        char = expression[index]
        next_char = expression[index + 1] if index + 1 < len(expression) else ""
        next_two = expression[index + 1 : index + 3]

        if state == "line_comment":
            if char in "\r\n":
                result.append(" */")
                state = "code"
            result.append(char)
            index += 1
            continue
        if state == "block_comment":
            result.append(char)
            if char == "*" and next_char == "/":
                result.append(next_char)
                state = "code"
                index += 2
            else:
                index += 1
            continue
        if state == "string":
            result.append(char)
            if char == "\\" and next_char:
                result.append(next_char)
                index += 2
            elif char == '"':
                state = "code"
                index += 1
            else:
                index += 1
            continue
        if state in {"verbatim_string", "interpolated_verbatim_string"}:
            result.append(char)
            if char == '"' and next_char == '"':
                result.append(next_char)
                index += 2
            elif char == '"':
                state = "code"
                index += 1
            else:
                index += 1
            continue
        if state == "char":
            result.append(char)
            if char == "\\" and next_char:
                result.append(next_char)
                index += 2
            elif char == "'":
                state = "code"
                index += 1
            else:
                index += 1
            continue

        if char == "/" and next_char == "/":
            result.append("/*")
            state = "line_comment"
            index += 2
        elif char == "/" and next_char == "*":
            result.extend((char, next_char))
            state = "block_comment"
            index += 2
        elif char == "$" and next_two == '@"':
            result.extend((char, next_two))
            state = "interpolated_verbatim_string"
            index += 3
        elif char == "@" and next_two == '$"':
            result.extend((char, next_two))
            state = "interpolated_verbatim_string"
            index += 3
        elif char == "@" and next_char == '"':
            result.extend((char, next_char))
            state = "verbatim_string"
            index += 2
        elif char == "$" and next_char == '"':
            result.extend((char, next_char))
            state = "string"
            index += 2
        elif char == '"':
            result.append(char)
            state = "string"
            index += 1
        elif char == "'":
            result.append(char)
            state = "char"
            index += 1
        else:
            result.append(char)
            index += 1

    if state == "line_comment":
        result.append(" */")
    return "".join(result)


def normalize_policy_expression_comments(xml_text: str) -> str:
    def replace_attribute(match: re.Match[str]) -> str:
        value = html.unescape(match.group("value"))
        if not value.lstrip().startswith("@"):
            return match.group(0)
        rewritten = rewrite_csharp_line_comments(value)
        return (
            f"{match.group('name')}={match.group('quote')}"
            f"{html.escape(rewritten, quote=True)}{match.group('quote')}"
        )

    return ATTRIBUTE_PATTERN.sub(replace_attribute, xml_text)


def generate_priority_policy() -> tuple[str, tuple[str, ...], int]:
    template = normalize_policy_expression_comments(
        PRIORITY_TEMPLATE_PATH.read_text(encoding="utf-8")
    )
    source_root = ElementTree.fromstring(template, parser=_xml_parser())
    if source_root.tag != "policies":
        raise ValueError("priority policy template root must be <policies>")

    shell_root = ElementTree.Element("policies")
    fragments: list[str] = []
    expected_sections = {"inbound", "backend", "outbound", "on-error"}
    found_sections: set[str] = set()

    for source_section in source_root:
        if not isinstance(source_section.tag, str):
            cloned = copy.deepcopy(source_section)
            cloned.tail = None
            shell_root.append(cloned)
            continue
        if source_section.tag not in expected_sections:
            raise ValueError(
                f"unexpected priority policy section <{source_section.tag}>"
            )
        found_sections.add(source_section.tag)
        shell_section = ElementTree.SubElement(
            shell_root,
            source_section.tag,
            dict(source_section.attrib),
        )

        if source_section.tag == "inbound":
            _append_priority_sequence(
                list(source_section),
                shell_section,
                fragments,
            )
            continue

        if source_section.tag == "backend":
            retry_children = [
                child
                for child in source_section
                if isinstance(child.tag, str) and child.tag == "retry"
            ]
            if len(retry_children) != 1 or len(source_section) != 1:
                raise ValueError(
                    "priority backend template must contain exactly one retry"
                )
            retry = retry_children[0]
            retry_shell = ElementTree.SubElement(
                shell_section,
                "retry",
                dict(retry.attrib),
            )
            _append_priority_sequence(list(retry), retry_shell, fragments)
            continue

        _append_priority_sequence(
            list(source_section),
            shell_section,
            fragments,
        )

    if found_sections != expected_sections:
        raise ValueError(
            "priority policy template sections are missing or duplicated"
        )

    populated_count = len(fragments)
    for index in range(populated_count, PRIORITY_FRAGMENT_COUNT):
        fragments.append(
            "<fragment>\n"
            f'  <set-variable name="priorityPolicyPlaceholder{index}" '
            'value="@((bool)true)" />\n'
            "</fragment>\n"
        )
    shell = _serialize_xml(shell_root)
    return shell, tuple(fragments), populated_count


def _position(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    return line, index - last_newline


def validate_csharp_expression(expression: str, source: str) -> None:
    if JOBJECT_INDEX_INITIALIZER_PATTERN.search(expression):
        raise ValueError(
            f"{source}: JObject index initializers are forbidden; "
            "use JProperty constructors or assignment statements"
        )
    if len(expression) >= APIM_EXPRESSION_MAX_CHARS:
        raise ValueError(
            f"{source}: policy expression is {len(expression)} characters; "
            f"APIM requires fewer than {APIM_EXPRESSION_MAX_CHARS}"
        )

    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int, str | None]] = []
    state = "code"
    literal_start = 0
    index = 0

    while index < len(expression):
        char = expression[index]
        next_char = expression[index + 1] if index + 1 < len(expression) else ""
        next_two = expression[index + 1 : index + 3]

        if state == "line_comment":
            if char == "\n":
                state = "code"
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "code"
                index += 2
            else:
                index += 1
            continue
        if state == "string":
            if char == "\\":
                index += 2
            elif char == '"':
                state = "code"
                index += 1
            elif char in "\r\n":
                line, column = _position(expression, literal_start)
                raise ValueError(
                    f"{source}:{line}:{column}: unterminated string literal"
                )
            else:
                index += 1
            continue
        if state == "interpolated_string":
            if char == "\\":
                index += 2
            elif char == "{" and next_char == "{":
                index += 2
            elif char == "{":
                stack.append(("{", index, state))
                state = "code"
                index += 1
            elif char == "}" and next_char == "}":
                index += 2
            elif char == "}":
                line, column = _position(expression, index)
                raise ValueError(
                    f"{source}:{line}:{column}: unmatched '}}'"
                )
            elif char == '"':
                state = "code"
                index += 1
            elif char in "\r\n":
                line, column = _position(expression, literal_start)
                raise ValueError(
                    f"{source}:{line}:{column}: unterminated string literal"
                )
            else:
                index += 1
            continue
        if state == "verbatim_string":
            if char == '"' and next_char == '"':
                index += 2
            elif char == '"':
                state = "code"
                index += 1
            else:
                index += 1
            continue
        if state == "interpolated_verbatim_string":
            if char == '"' and next_char == '"':
                index += 2
            elif char == "{" and next_char == "{":
                index += 2
            elif char == "{":
                stack.append(("{", index, state))
                state = "code"
                index += 1
            elif char == "}" and next_char == "}":
                index += 2
            elif char == "}":
                line, column = _position(expression, index)
                raise ValueError(
                    f"{source}:{line}:{column}: unmatched '}}'"
                )
            elif char == '"':
                state = "code"
                index += 1
            else:
                index += 1
            continue
        if state == "char":
            if char == "\\":
                index += 2
            elif char == "'":
                state = "code"
                index += 1
            elif char in "\r\n":
                line, column = _position(expression, literal_start)
                raise ValueError(
                    f"{source}:{line}:{column}: unterminated character literal"
                )
            else:
                index += 1
            continue

        if char == "/" and next_char == "/":
            state = "line_comment"
            index += 2
        elif char == "/" and next_char == "*":
            state = "block_comment"
            literal_start = index
            index += 2
        elif char == "$" and next_two == '@"':
            state = "interpolated_verbatim_string"
            literal_start = index
            index += 3
        elif char == "@" and next_two == '$"':
            state = "interpolated_verbatim_string"
            literal_start = index
            index += 3
        elif char == "@" and next_char == '"':
            state = "verbatim_string"
            literal_start = index
            index += 2
        elif char == "$" and next_char == '"':
            state = "interpolated_string"
            literal_start = index
            index += 2
        elif char == '"':
            state = "string"
            literal_start = index
            index += 1
        elif char == "'":
            state = "char"
            literal_start = index
            index += 1
        elif char in "([{":
            stack.append((char, index, None))
            index += 1
        elif char in ")]}":
            if not stack or stack[-1][0] != pairs[char]:
                line, column = _position(expression, index)
                raise ValueError(
                    f"{source}:{line}:{column}: unmatched {char!r}"
                )
            _, _, return_state = stack.pop()
            if return_state is not None:
                state = return_state
            index += 1
        else:
            index += 1

    if state != "code" and state != "line_comment":
        line, column = _position(expression, literal_start)
        description = {
            "block_comment": "unterminated block comment",
            "char": "unterminated character literal",
        }.get(state, "unterminated string literal")
        raise ValueError(f"{source}:{line}:{column}: {description}")
    if stack:
        delimiter, delimiter_index, _ = stack[-1]
        line, column = _position(expression, delimiter_index)
        raise ValueError(
            f"{source}:{line}:{column}: unmatched {delimiter!r}"
        )


def validate_policy_expressions(xml_text: str, source: str) -> None:
    root = ElementTree.fromstring(xml_text)
    searchable_xml = XML_COMMENT_PATTERN.sub(
        lambda match: re.sub(r"[^\n]", " ", match.group(0)),
        xml_text,
    )

    for match in ATTRIBUTE_PATTERN.finditer(searchable_xml):
        expression = html.unescape(match.group("value"))
        if not expression.lstrip().startswith("@"):
            continue
        line = searchable_xml.count("\n", 0, match.start("value")) + 1
        validate_csharp_expression(
            expression,
            f"{source}:{line} attribute {match.group('name')}",
        )

    for element in root.iter():
        text = element.text or ""
        expression = text.strip()
        if expression.startswith("@"):
            validate_csharp_expression(
                expression,
                f"{source} <{element.tag}> text",
            )


def validate_policy_fragment(xml_text: str, source: str) -> None:
    validate_policy_expressions(xml_text, source)
    root = ElementTree.fromstring(xml_text)
    forbidden_elements = [
        element.tag
        for element in root.iter()
        if isinstance(element.tag, str)
        and element.tag in {"base", "include-fragment"}
    ]
    if forbidden_elements:
        raise ValueError(
            f"{source}: policy fragments cannot contain "
            f"<{forbidden_elements[0]} />"
        )
    searchable_xml = XML_COMMENT_PATTERN.sub(
        lambda match: re.sub(r"[^\n]", " ", match.group(0)),
        xml_text,
    )
    for match in ATTRIBUTE_PATTERN.finditer(searchable_xml):
        expression = html.unescape(match.group("value"))
        if expression.lstrip().startswith("@") and "://" in expression:
            raise ValueError(
                f"{source}: APIM policy fragments reject literal '://' tokens "
                "inside expression attributes; construct the scheme separator"
            )
    validate_policy_payload(
        xml_text,
        source,
        max_bytes=APIM_POLICY_FRAGMENT_MAX_BYTES,
        payload_kind="policy fragment",
    )


def validate_policy_payload(
    xml_text: str,
    source: str,
    *,
    max_bytes: int,
    payload_kind: str,
) -> None:
    raw_bytes = policy_payload_bytes(xml_text)
    if raw_bytes > max_bytes:
        raise ValueError(
            f"{source}: {payload_kind} payload is {raw_bytes} raw UTF-8 bytes; "
            f"safe ceiling is {max_bytes}"
        )


def validate_fragment_include_chain(xml_text: str, source: str) -> None:
    root = ElementTree.fromstring(xml_text)
    include_ids = [
        element.attrib["fragment-id"]
        for element in root.iter("include-fragment")
        if element.attrib.get("fragment-id", "").startswith("endpoint_selection_")
    ]
    expected_ids = [*CATALOG_FRAGMENT_IDS, SETUP_FRAGMENT_ID]
    if include_ids != expected_ids:
        raise ValueError(
            f"{source}: endpoint fragment include chain must be "
            f"{expected_ids}, found {include_ids}"
        )


def validate_priority_include_chain(
    xml_text: str,
    source: str,
    populated_count: int,
) -> None:
    root = ElementTree.fromstring(xml_text)
    include_ids = [
        element.attrib["fragment-id"]
        for element in root.iter("include-fragment")
        if element.attrib.get("fragment-id", "").startswith("priority_policy_")
    ]
    expected_ids = list(PRIORITY_FRAGMENT_IDS[:populated_count])
    if include_ids != expected_ids:
        raise ValueError(
            f"{source}: priority fragment include chain must be "
            f"{expected_ids}, found {include_ids}"
        )


def generate_endpoint_policies() -> tuple[str, tuple[str, ...]]:
    models = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(CATALOG_MARKER) != 1 or template.count(ATTEMPTS_MARKER) != 3:
        raise ValueError("gateway policy template markers are missing or duplicated")
    catalog_blocks, max_attempts = render_catalog(models)
    catalog_fragments, merge_variable = render_catalog_fragments(catalog_blocks)
    setup_fragment = template.replace(CATALOG_MARKER, merge_variable).replace(
        ATTEMPTS_MARKER, str(max_attempts)
    )
    return setup_fragment, catalog_fragments


def generate() -> str:
    setup_fragment, _ = generate_endpoint_policies()
    return setup_fragment


def generate_realtime_policy(models: dict[str, Any]) -> str:
    naming = models["naming"]
    routes: list[str] = []
    for model in models["catalog"]:
        if model["category"] != "realtime":
            continue
        for deployment in model["deployments"]:
            name = deployment_name(
                model=model["name"],
                subscription_token=naming["subscriptionToken"],
                region=deployment["region"],
                sku=deployment["sku"],
                sku_short=naming["skuShort"],
            )
            routes.append(
                "      <when condition=\"@(&quot;"
                + html.escape(name, quote=True)
                + "&quot;.Equals(context.Request.Url.Query.GetValueOrDefault"
                "(&quot;deployment&quot;, &quot;&quot;), "
                "StringComparison.OrdinalIgnoreCase))\">\n"
                "        <set-backend-service base-url=\"{{foundry-"
                + deployment["region"]
                + "-endpoint}}/openai/realtime\" />\n"
                "      </when>"
            )

    if not routes:
        raise ValueError("infra/models.json contains no realtime deployments")

    return (
        "<policies>\n"
        "  <inbound>\n"
        "    <base />\n"
        "    <choose>\n"
        + "\n".join(routes)
        + "\n"
        "      <otherwise>\n"
        "        <return-response>\n"
        "          <set-status code=\"404\" reason=\"Realtime deployment is not in the AI4IA catalog\" />\n"
        "          <set-body>{\"error\":{\"code\":\"model_not_allowed\",\"message\":\"The requested realtime deployment is not allowed by the gateway catalog.\"}}</set-body>\n"
        "        </return-response>\n"
        "      </otherwise>\n"
        "    </choose>\n"
        "    <set-header name=\"x-correlation-id\" exists-action=\"override\">\n"
        "      <value>@(context.RequestId.ToString())</value>\n"
        "    </set-header>\n"
        "    <set-header name=\"Ocp-Apim-Subscription-Key\" exists-action=\"delete\" />\n"
        "    <set-header name=\"Authorization\" exists-action=\"delete\" />\n"
        "    <authentication-managed-identity resource=\"https://cognitiveservices.azure.com\" />\n"
        "  </inbound>\n"
        "  <backend><base /></backend>\n"
        "  <outbound><base /></outbound>\n"
        "  <on-error><base /></on-error>\n"
        "</policies>\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in fragment differs from generated output",
    )
    args = parser.parse_args()
    models = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    generated, catalog_fragments = generate_endpoint_policies()
    priority_generated, priority_fragments, priority_populated_count = (
        generate_priority_policy()
    )
    realtime_generated = generate_realtime_policy(models)
    fragments = (
        (OUTPUT_PATH, generated),
        *zip(CATALOG_OUTPUT_PATHS, catalog_fragments, strict=True),
        *zip(PRIORITY_OUTPUT_PATHS, priority_fragments, strict=True),
    )
    policies = (
        *fragments,
        (PRIORITY_POLICY_PATH, priority_generated),
        (REALTIME_OUTPUT_PATH, realtime_generated),
    )
    immutable_generation_outputs = (
        *fragments,
        (PRIORITY_POLICY_PATH, priority_generated),
    )
    try:
        for path, policy in fragments:
            validate_policy_fragment(policy, str(path.relative_to(ROOT)))
        for path, policy in policies[len(fragments) :]:
            source = str(path.relative_to(ROOT))
            validate_policy_expressions(policy, source)
            validate_policy_payload(
                policy,
                source,
                max_bytes=APIM_POLICY_DOCUMENT_MAX_BYTES,
                payload_kind="policy document",
            )
        validate_fragment_include_chain(
            policies[len(fragments)][1],
            str(PRIORITY_POLICY_PATH.relative_to(ROOT)),
        )
        validate_priority_include_chain(
            policies[len(fragments)][1],
            str(PRIORITY_POLICY_PATH.relative_to(ROOT)),
            priority_populated_count,
        )
        generation_digest = policy_generation_digest(
            immutable_generation_outputs
        )
        write_generation_manifest = validate_generation_manifest(
            generation_digest,
            check=args.check,
        )
    except (ElementTree.ParseError, ValueError) as error:
        print(f"Gateway policy validation failed: {error}", file=sys.stderr)
        return 1

    if args.check:
        generated_outputs = (
            *fragments,
            (PRIORITY_POLICY_PATH, priority_generated),
            (REALTIME_OUTPUT_PATH, realtime_generated),
        )
        stale = any(
            not path.exists() or path.read_text(encoding="utf-8") != content
            for path, content in generated_outputs
        )
        if stale:
            print(
                "Gateway policy catalog is stale. Run scripts/gen-gateway-policy.py.",
                file=sys.stderr,
            )
            return 1
        print("Gateway model and realtime policy catalogs are current.")
        return 0

    for path, content in (
        *fragments,
        (PRIORITY_POLICY_PATH, priority_generated),
        (REALTIME_OUTPUT_PATH, realtime_generated),
    ):
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"Wrote {path.relative_to(ROOT)}")
    if write_generation_manifest:
        manifest = {
            "generation": POLICY_FRAGMENT_GENERATION,
            "sha256": generation_digest,
        }
        POLICY_GENERATION_MANIFEST_PATH.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"Wrote {POLICY_GENERATION_MANIFEST_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
