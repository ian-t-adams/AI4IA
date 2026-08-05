#!/usr/bin/env python3
"""Generate APIM model and realtime routing from the authoritative catalog."""

from __future__ import annotations

import argparse
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
# Number of APIM policy fragments the backend catalog is sharded across.
#
# The catalog cannot live in one fragment: APIM rejects a single decoded policy
# expression at 32 KiB (APIM_EXPRESSION_MAX_CHARS below), so render_catalog_fragments
# packs models into CATALOG_CHUNK_TARGET_CHARS-sized chunks and emits one fragment
# per chunk. Unused shards are padded with an empty JObject and merged harmlessly,
# so this is a ceiling, not a fixed cost — over-provisioning it is cheap.
#
# THIS VALUE IS DUPLICATED IN TWO NON-PYTHON FILES that cannot import it:
#   * infra/modules/gateway.bicep          (loadTextContent needs literal paths)
#   * scripts/test-apim-policy-compiler.ps1
# Bicep only deploys the fragments it explicitly lists, so raising this constant
# WITHOUT updating gateway.bicep would silently drop every model in the extra
# shards from gateway routing — a data-loss bug with no error anywhere. The
# three files are pinned together by
# test_gateway_policy.test_catalog_fragment_count_matches_bicep_and_compiler_script.
CATALOG_FRAGMENT_COUNT = 12
CATALOG_OUTPUT_PATHS = tuple(
    ROOT / "infra" / "policies" / f"simplel7proxy-endpoints-catalog-{index}.xml"
    for index in range(CATALOG_FRAGMENT_COUNT)
)
CATALOG_FRAGMENT_IDS = tuple(
    f"endpoint_selection_catalog_{index}_32"
    for index in range(CATALOG_FRAGMENT_COUNT)
)
SETUP_FRAGMENT_ID = "endpoint_selection_setup_32"
PRIORITY_POLICY_PATH = (
    ROOT / "infra" / "policies" / "simplel7proxy-priority-retry.xml"
)
PRIORITY_OUTPUT_PATH = (
    ROOT / "infra" / "policies" / "simplel7proxy-priority-policy.xml"
)
PRIORITY_FRAGMENT_IDS = (
    "simplel7proxy_inbound_pre_32",
    "simplel7proxy_inbound_post_32",
    "simplel7proxy_backend_32",
    "simplel7proxy_outbound_32",
    "simplel7proxy_on_error_32",
)
PRIORITY_OUTPUT_PATHS = tuple(
    ROOT / "infra" / "policies" / f"{fragment_id}.xml"
    for fragment_id in PRIORITY_FRAGMENT_IDS
)
REALTIME_OUTPUT_PATH = ROOT / "infra" / "policies" / "realtime-routing.xml"
# Generated from infra/voice-providers.json by gen-voice-provider-catalog.py,
# then independently validated here with the other gateway policies.
SPEECH_VOICE_LIVE_POLICY_PATH = ROOT / "infra" / "policies" / "speech-voice-live.xml"
CATALOG_MARKER = "__AI4IA_BACKEND_CATALOG_MERGE__"
ATTEMPTS_MARKER = "__AI4IA_MAX_IMMEDIATE_ATTEMPTS__"
# APIM rejects a single decoded policy expression at the 32 KiB boundary.
APIM_EXPRESSION_MAX_CHARS = 32_768
# The documented resource limit is 512 KiB, but a 74,784-byte fragment fails
# APIM's policy compiler. Keep generated fragments well below that observed path.
APIM_FRAGMENT_COMPILER_SAFE_BYTES = 48 * 1024
APIM_API_POLICY_MAX_BYTES = 16 * 1024
CATALOG_CHUNK_TARGET_CHARS = 24_000
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


def chunk_catalog(blocks: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []

    for block in blocks:
        candidate = current + [block]
        expression = catalog_expression(candidate)
        if current and len(expression) > CATALOG_CHUNK_TARGET_CHARS:
            chunks.append(current)
            current = [block]
        else:
            current = candidate

        single_expression = catalog_expression(current)
        if len(single_expression) >= APIM_EXPRESSION_MAX_CHARS:
            raise ValueError("a generated backend catalog entry exceeds APIM limits")

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
        expression = catalog_expression(chunk)
        fragments.append(
            "<fragment>\n"
            f'    <set-variable name="{name}" '
            f'value="{html.escape(expression, quote=True)}" />\n'
            "</fragment>\n"
        )

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
    raw_bytes = len(xml_text.encode("utf-8"))
    if raw_bytes > APIM_FRAGMENT_COMPILER_SAFE_BYTES:
        raise ValueError(
            f"{source}: policy fragment is {raw_bytes} bytes; "
            f"safe compiler ceiling is {APIM_FRAGMENT_COMPILER_SAFE_BYTES}"
        )


def _find_tag_end(xml_text: str, start: int) -> int:
    quote = ""
    for index in range(start + 1, len(xml_text)):
        char = xml_text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == ">":
            return index
    raise ValueError("unterminated XML tag")


def _top_level_xml_nodes(xml_text: str) -> list[str]:
    nodes: list[str] = []
    index = 0
    while index < len(xml_text):
        while index < len(xml_text) and xml_text[index].isspace():
            index += 1
        if index >= len(xml_text):
            break
        start = index
        depth = 0
        while index < len(xml_text):
            marker = xml_text.find("<", index)
            if marker < 0:
                raise ValueError("unterminated XML element")
            if xml_text.startswith("<!--", marker):
                comment_end = xml_text.find("-->", marker + 4)
                if comment_end < 0:
                    raise ValueError("unterminated XML comment")
                index = comment_end + 3
                if depth == 0:
                    nodes.append(xml_text[start:index])
                    break
                continue
            tag_end = _find_tag_end(xml_text, marker)
            token = xml_text[marker : tag_end + 1]
            if token.startswith("</"):
                depth -= 1
            elif not token.startswith(("<?", "<!")) and not token.rstrip().endswith(
                "/>"
            ):
                depth += 1
            index = tag_end + 1
            if depth == 0:
                nodes.append(xml_text[start:index])
                break
        else:
            raise ValueError("unterminated XML node")
    return nodes


def _section_nodes(policy: str, section_name: str) -> list[str]:
    match = re.search(
        rf"<{re.escape(section_name)}>(?P<body>.*?)</{re.escape(section_name)}>",
        policy,
        re.DOTALL,
    )
    if match is None:
        raise ValueError(f"priority policy section {section_name!r} is missing")
    return _top_level_xml_nodes(match.group("body"))


def _node_tag(node: str) -> str:
    match = re.match(r"<([A-Za-z][\w-]*)\b", node)
    return match.group(1) if match else ""


def _serialize_fragment(children: list[str]) -> str:
    body = "\n".join(child.rstrip() for child in children)
    body = "\n".join(line.rstrip() for line in body.splitlines())
    return f"<fragment>\n{body}\n</fragment>\n"


def generate_priority_policies() -> tuple[str, tuple[str, ...]]:
    source = PRIORITY_POLICY_PATH.read_text(encoding="utf-8")
    inbound_children = _section_nodes(source, "inbound")
    base_index = next(
        index
        for index, child in enumerate(inbound_children)
        if _node_tag(child) == "base"
    )
    if base_index != 0:
        raise ValueError("inbound base policy must be the first policy")
    endpoint_ids = (*CATALOG_FRAGMENT_IDS, SETUP_FRAGMENT_ID)
    endpoint_nodes = [
        (index, child)
        for index, child in enumerate(inbound_children)
        if _node_tag(child) == "include-fragment"
        and any(f'fragment-id="{fragment_id}"' in child for fragment_id in endpoint_ids)
    ]
    endpoint_indices = [index for index, _ in endpoint_nodes]
    endpoint_order = [
        next(
            fragment_id
            for fragment_id in endpoint_ids
            if f'fragment-id="{fragment_id}"' in child
        )
        for _, child in endpoint_nodes
    ]
    if tuple(endpoint_order) != endpoint_ids:
        raise ValueError("priority policy endpoint fragment chain is incomplete")
    expected_indices = list(
        range(endpoint_indices[0], endpoint_indices[0] + len(endpoint_ids))
    )
    if endpoint_indices != expected_indices:
        raise ValueError("priority policy endpoint fragment chain must be contiguous")
    inbound_pre = inbound_children[base_index + 1 : endpoint_indices[0]]
    inbound_post = inbound_children[endpoint_indices[-1] + 1 :]

    def without_base(section_name: str) -> tuple[str, list[str]]:
        children = _section_nodes(source, section_name)
        base_indices = [
            index for index, child in enumerate(children) if _node_tag(child) == "base"
        ]
        if not base_indices:
            return "none", children
        if len(base_indices) != 1:
            raise ValueError(f"{section_name} contains multiple base policies")
        base_index = base_indices[0]
        if base_index == 0:
            placement = "before"
        elif base_index == len(children) - 1:
            placement = "after"
        else:
            raise ValueError(
                f"{section_name} base policy must be first or last for splitting"
            )
        return placement, [
            child for index, child in enumerate(children) if index != base_index
        ]

    backend_base, backend_children = without_base("backend")
    outbound_base, outbound_children = without_base("outbound")
    on_error_base, on_error_children = without_base("on-error")
    fragments = (
        _serialize_fragment(inbound_pre),
        _serialize_fragment(inbound_post),
        _serialize_fragment(backend_children),
        _serialize_fragment(outbound_children),
        _serialize_fragment(on_error_children),
    )

    def section_fragment(fragment_id: str, base_placement: str) -> str:
        before = "    <base />\n" if base_placement == "before" else ""
        after = "    <base />\n" if base_placement == "after" else ""
        return (
            before
            + f'    <include-fragment fragment-id="{fragment_id}" />\n'
            + after
        )

    wrapper = (
        "<policies>\n"
        "  <inbound>\n"
        "    <base />\n"
        f'    <include-fragment fragment-id="{PRIORITY_FRAGMENT_IDS[0]}" />\n'
        + "".join(
            f'    <include-fragment fragment-id="{fragment_id}" />\n'
            for fragment_id in endpoint_ids
        )
        + f'    <include-fragment fragment-id="{PRIORITY_FRAGMENT_IDS[1]}" />\n'
        + "  </inbound>\n"
        + "  <backend>\n"
        + section_fragment(PRIORITY_FRAGMENT_IDS[2], backend_base)
        + "  </backend>\n"
        + "  <outbound>\n"
        + section_fragment(PRIORITY_FRAGMENT_IDS[3], outbound_base)
        + "  </outbound>\n"
        + "  <on-error>\n"
        + section_fragment(PRIORITY_FRAGMENT_IDS[4], on_error_base)
        + "  </on-error>\n"
        + "</policies>\n"
    )
    return wrapper, fragments


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
                + "-realtime-wss-endpoint}}/openai/realtime\" />\n"
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


def validate_realtime_policy(policy: str, source: str) -> None:
    """Reject policies unsupported during an APIM WebSocket onHandshake phase."""
    root = ElementTree.fromstring(policy)
    allowed = {
        "policies", "inbound", "backend", "outbound", "on-error", "base",
        "choose", "when", "otherwise", "set-backend-service", "return-response",
        "set-status", "set-header", "value", "authentication-managed-identity",
    }
    unsupported = {element.tag for element in root.iter() if element.tag not in allowed}
    if unsupported:
        raise ValueError(f"{source}: unsupported WebSocket handshake policy element(s): {sorted(unsupported)}")
    if root.findall(".//set-body"):
        raise ValueError(f"{source}: set-body is unsupported for WebSocket onHandshake")
    for backend in root.findall(".//set-backend-service"):
        url = backend.attrib.get("base-url", "")
        if "-realtime-wss-endpoint}}/openai/realtime" not in url:
            raise ValueError(f"{source}: realtime backend must use the WSS named value and exact /openai/realtime path")


def validate_speech_voice_live_policy(policy: str, source: str) -> None:
    """Statically pin the Speech Voice Live onHandshake policy to the approved,
    additive, isolated topology: only WebSocket-handshake-supported elements, a
    curated model allowlist, one fixed backend/API version, managed-identity
    backend auth via a named value, and no reference to another host or API."""
    root = ElementTree.fromstring(policy)
    allowed = {
        "policies", "inbound", "backend", "outbound", "on-error", "base",
        "set-backend-service", "set-query-parameter", "value", "set-header",
        "authentication-managed-identity", "return-response", "set-status",
        "choose", "when",
    }
    unsupported = {element.tag for element in root.iter() if element.tag not in allowed}
    if unsupported:
        raise ValueError(f"{source}: unsupported WebSocket handshake policy element(s): {sorted(unsupported)}")
    if root.findall(".//set-body"):
        raise ValueError(f"{source}: set-body is unsupported for WebSocket onHandshake")
    if root.findall(".//validate-parameters"):
        raise ValueError(f"{source}: validate-parameters is unsupported for WebSocket onHandshake")
    inbound = root.find("./inbound")
    if inbound is None:
        raise ValueError(f"{source}: expected an inbound policy section")
    inbound_children = list(inbound)

    backends = root.findall(".//set-backend-service")
    if len(backends) != 1:
        raise ValueError(f"{source}: expected exactly one set-backend-service")
    if backends[0] not in inbound_children:
        raise ValueError(f"{source}: set-backend-service must be a direct inbound policy")
    backend_url = backends[0].attrib.get("base-url", "")
    if backend_url != "{{speech-voice-live-wss-endpoint}}/voice-live/realtime":
        raise ValueError(
            f"{source}: backend must be the named-value WSS endpoint plus the exact "
            "/voice-live/realtime path, and nothing else"
        )

    query_params = root.findall(".//set-query-parameter")
    if any(query_param not in inbound_children for query_param in query_params):
        raise ValueError(f"{source}: set-query-parameter must be a direct inbound policy")
    fixed_params = {
        query_param.attrib.get("name"): (query_param.findtext("value") or "").strip()
        for query_param in query_params
        if query_param.attrib.get("exists-action") == "override"
    }
    model_ids = (
        "gpt-realtime",
        "gpt-realtime-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-5-mini",
        "gpt-5.1",
    )
    model_override = fixed_params.get("model", "")
    if (
        "String.IsNullOrWhiteSpace" not in model_override
        or '? "gpt-realtime" :' not in model_override
    ):
        raise ValueError(
            f"{source}: model query parameter must default blank values to gpt-realtime"
        )
    choices = inbound.findall("./choose")
    if len(choices) != 1:
        raise ValueError(f"{source}: expected exactly one inbound model allowlist")
    reject_branches = choices[0].findall("./when")
    if len(reject_branches) != 1:
        raise ValueError(f"{source}: expected exactly one model rejection branch")
    reject_condition = html.unescape(reject_branches[0].attrib.get("condition", ""))
    allowed_model_literals = tuple(
        re.findall(
            r'"([^"]+)"\.Equals\(model,\s*StringComparison\.Ordinal\)',
            reject_condition,
        )
    )
    if (
        "String.IsNullOrWhiteSpace" not in reject_condition
        or "StringComparison.Ordinal" not in reject_condition
        or allowed_model_literals != model_ids
    ):
        raise ValueError(
            f"{source}: model rejection branch must allow exactly the managed-model catalog"
        )
    rejection_statuses = reject_branches[0].findall("./return-response/set-status")
    if (
        len(rejection_statuses) != 1
        or rejection_statuses[0].attrib.get("code") != "400"
    ):
        raise ValueError(f"{source}: unsupported models must receive a bodyless 400 response")
    if fixed_params.get("api-version") != "2026-04-10":
        raise ValueError(f"{source}: api-version query parameter must be fixed to 2026-04-10")
    deleted_params = {
        query_param.attrib.get("name")
        for query_param in query_params
        if query_param.attrib.get("exists-action") == "delete"
    }
    required_deleted_params = {
        "deployment",
        "subscription-key",
        "api-key",
        "agent_id",
        "project_id",
    }
    missing_param_strips = required_deleted_params - deleted_params
    if missing_param_strips:
        raise ValueError(
            f"{source}: must strip caller selectors/credentials before the backend: "
            f"{sorted(missing_param_strips)}"
        )
    for query_param in query_params:
        if query_param.attrib.get("exists-action") not in {"override", "delete"}:
            raise ValueError(
                f"{source}: set-query-parameter must override (fix) or delete a value, "
                "never append/skip a caller-controlled one"
            )

    identities = root.findall(".//authentication-managed-identity")
    if len(identities) != 1:
        raise ValueError(f"{source}: expected exactly one authentication-managed-identity policy")
    if identities[0] not in inbound_children:
        raise ValueError(
            f"{source}: authentication-managed-identity must be a direct inbound policy"
        )
    if identities[0].attrib.get("resource") != "{{speech-voice-live-mi-audience}}":
        raise ValueError(
            f"{source}: managed-identity resource must be the named-value audience, "
            "never a literal or caller-influenced host"
        )

    headers = inbound.findall("./set-header")
    strip_headers = {
        header.attrib.get("name")
        for header in headers
        if header.attrib.get("exists-action") == "delete"
    }
    required_stripped = {
        "Ocp-Apim-Subscription-Key", "api-key", "Authorization",
        "X-AI4IA-App-Id", "X-AI4IA-User-Id", "X-UserProfile",
    }
    missing_strips = required_stripped - strip_headers
    if missing_strips:
        raise ValueError(f"{source}: must strip caller/internal headers before the backend: {sorted(missing_strips)}")
    identity_index = inbound_children.index(identities[0])
    required_strip_elements = [
        element
        for element in inbound_children
        if (
            element.tag == "set-query-parameter"
            and element.attrib.get("name") in required_deleted_params
            and element.attrib.get("exists-action") == "delete"
        )
        or (
            element.tag == "set-header"
            and element.attrib.get("name") in required_stripped
            and element.attrib.get("exists-action") == "delete"
        )
    ]
    if any(inbound_children.index(element) > identity_index for element in required_strip_elements):
        raise ValueError(
            f"{source}: must strip caller selectors/credentials before managed-identity authentication"
        )

    # No fallback to Azure OpenAI, another host, or the proxy/MCP APIs. This
    # deliberately scans the whole document (including comments) so an accidental
    # reference anywhere is caught. "consumption" is still rejected even though the
    # Consumption APIM has been deleted, so reintroducing a fallback to one fails here.
    lowered = policy.lower()
    forbidden_tokens = ("openai", "cognitiveservices.azure.com", "consumption", "/mcp", "proxy")
    hits = [token for token in forbidden_tokens if token in lowered]
    if hits:
        raise ValueError(f"{source}: must not reference {hits} (no fallback path is permitted)")


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
    priority_generated, priority_fragments = generate_priority_policies()
    realtime_generated = generate_realtime_policy(models)
    fragments = (
        (OUTPUT_PATH, generated),
        *zip(CATALOG_OUTPUT_PATHS, catalog_fragments, strict=True),
        *zip(PRIORITY_OUTPUT_PATHS, priority_fragments, strict=True),
    )
    policies = (
        *fragments,
        (
            PRIORITY_POLICY_PATH,
            PRIORITY_POLICY_PATH.read_text(encoding="utf-8"),
        ),
        (PRIORITY_OUTPUT_PATH, priority_generated),
        (REALTIME_OUTPUT_PATH, realtime_generated),
    )
    try:
        for path, policy in fragments:
            validate_policy_fragment(policy, str(path.relative_to(ROOT)))
        for path, policy in policies[len(fragments) :]:
            validate_policy_expressions(policy, str(path.relative_to(ROOT)))
        validate_realtime_policy(realtime_generated, str(REALTIME_OUTPUT_PATH.relative_to(ROOT)))
        speech_voice_live_policy = SPEECH_VOICE_LIVE_POLICY_PATH.read_text(encoding="utf-8")
        validate_policy_expressions(
            speech_voice_live_policy, str(SPEECH_VOICE_LIVE_POLICY_PATH.relative_to(ROOT))
        )
        validate_speech_voice_live_policy(
            speech_voice_live_policy, str(SPEECH_VOICE_LIVE_POLICY_PATH.relative_to(ROOT))
        )
        if len(priority_generated.encode("utf-8")) > APIM_API_POLICY_MAX_BYTES:
            raise ValueError(
                f"{PRIORITY_OUTPUT_PATH.relative_to(ROOT)} exceeds "
                f"{APIM_API_POLICY_MAX_BYTES} bytes"
            )
    except (ElementTree.ParseError, ValueError) as error:
        print(f"Gateway policy validation failed: {error}", file=sys.stderr)
        return 1

    if args.check:
        generated_outputs = (
            *fragments,
            (PRIORITY_OUTPUT_PATH, priority_generated),
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
        (PRIORITY_OUTPUT_PATH, priority_generated),
        (REALTIME_OUTPUT_PATH, realtime_generated),
    ):
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
