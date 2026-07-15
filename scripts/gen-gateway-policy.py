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
PRIORITY_POLICY_PATH = (
    ROOT / "infra" / "policies" / "simplel7proxy-priority-retry.xml"
)
REALTIME_OUTPUT_PATH = ROOT / "infra" / "policies" / "realtime-routing.xml"
CATALOG_MARKER = "__AI4IA_BACKEND_CATALOG_VARIABLES__"
ATTEMPTS_MARKER = "__AI4IA_MAX_IMMEDIATE_ATTEMPTS__"
# APIM rejects a single decoded policy expression at the 32 KiB boundary.
APIM_EXPRESSION_MAX_CHARS = 32_768
CATALOG_CHUNK_TARGET_CHARS = 24_000
ATTRIBUTE_PATTERN = re.compile(
    r"(?P<name>[\w:.-]+)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
XML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)


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
        f'                ["{label}"] = new JObject {{ '
        f'["url"] = "{named_value}", ["path"] = "openai", '
        f'["deployment"] = "{deployment}", ["priority"] = {priority}, '
        '["acceptablePriorities"] = "1, 2, 3", '
        f'["timeout"] = {timeout}, ["bufferResponse"] = false, ["auth"] = "MI" }}'
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
                f'            ["{requested["name"]}"] = new JObject {{\n'
                + ",\n".join(rows)
                + "\n            }"
            )
            blocks.append(block)

    if not blocks:
        raise ValueError("infra/models.json contains no model deployments")
    return blocks, max_attempts


def chunk_catalog(blocks: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []

    for block in blocks:
        candidate = current + [block]
        expression = (
            "@{\n"
            "        return new JObject {\n"
            + ",\n\n".join(candidate)
            + "\n"
            "        };\n"
            "    }"
        )
        if current and len(expression) > CATALOG_CHUNK_TARGET_CHARS:
            chunks.append(current)
            current = [block]
        else:
            current = candidate

        single_expression = (
            "@{\n"
            "        return new JObject {\n"
            + ",\n\n".join(current)
            + "\n"
            "        };\n"
            "    }"
        )
        if len(single_expression) >= APIM_EXPRESSION_MAX_CHARS:
            raise ValueError("a generated backend catalog entry exceeds APIM limits")

    if current:
        chunks.append(current)
    return chunks


def render_catalog_variables(blocks: list[str]) -> str:
    variables: list[str] = []
    chunk_names: list[str] = []

    for index, chunk in enumerate(chunk_catalog(blocks)):
        name = f"backendCatalogChunk{index}"
        chunk_names.append(name)
        expression = (
            "@{\n"
            "        return new JObject {\n"
            + ",\n\n".join(chunk)
            + "\n"
            "        };\n"
            "    }"
        )
        variables.append(
            f'    <set-variable name="{name}" '
            f'value="{html.escape(expression, quote=True)}" />'
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
    variables.append(
        '    <set-variable name="backendCatalog" '
        f'value="{html.escape(merge_expression, quote=True)}" />'
    )
    return "\n".join(variables)


def _position(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    return line, index - last_newline


def validate_csharp_expression(expression: str, source: str) -> None:
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


def generate() -> str:
    models = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(CATALOG_MARKER) != 1 or template.count(ATTEMPTS_MARKER) != 3:
        raise ValueError("gateway policy template markers are missing or duplicated")
    catalog_blocks, max_attempts = render_catalog(models)
    return template.replace(
        CATALOG_MARKER, render_catalog_variables(catalog_blocks)
    ).replace(ATTEMPTS_MARKER, str(max_attempts))


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
    generated = generate()
    realtime_generated = generate_realtime_policy(models)
    policies = (
        (OUTPUT_PATH, generated),
        (PRIORITY_POLICY_PATH, PRIORITY_POLICY_PATH.read_text(encoding="utf-8")),
        (REALTIME_OUTPUT_PATH, realtime_generated),
    )
    try:
        for path, policy in policies:
            validate_policy_expressions(policy, str(path.relative_to(ROOT)))
    except (ElementTree.ParseError, ValueError) as error:
        print(f"Gateway policy validation failed: {error}", file=sys.stderr)
        return 1

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        realtime_current = (
            REALTIME_OUTPUT_PATH.read_text(encoding="utf-8")
            if REALTIME_OUTPUT_PATH.exists()
            else ""
        )
        if current != generated or realtime_current != realtime_generated:
            print(
                "Gateway policy catalog is stale. Run scripts/gen-gateway-policy.py.",
                file=sys.stderr,
            )
            return 1
        print("Gateway model and realtime policy catalogs are current.")
        return 0

    OUTPUT_PATH.write_text(generated, encoding="utf-8", newline="\n")
    REALTIME_OUTPUT_PATH.write_text(
        realtime_generated, encoding="utf-8", newline="\n"
    )
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)}")
    print(f"Wrote {REALTIME_OUTPUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
