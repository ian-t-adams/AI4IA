"""Bounded MCP 2026-07-28 request metadata and schema-derived HTTP headers.

Contracts: specification/2026-07-28/basic/transports/streamable-http and
schema/2026-07-28/schema.ts in modelcontextprotocol/specification. These helpers
do not discover destinations, grant capabilities, or fetch schema references.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .mcp_servers import (
    MAX_RESOURCE_URI_LEN,
    McpConnectionError,
    McpProtocolVersion,
    UserMcpServer,
    health_config_revision,
    is_valid_remote_tool_name,
)
from .tools import is_fully_masked, redact, redact_obj

PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
CLIENT_INFO = {"name": "ai4ia", "version": "1.0"}

MAX_ROUTING_HEADERS = 16
MAX_HEADER_NAME = 64
MAX_HEADER_VALUE = 4096
MAX_ROUTING_HEADER_BYTES = 16_384
MAX_SCHEMA_NODES = 4096
MAX_SCHEMA_DEPTH = 32
MAX_SAFE_INTEGER = 2**53 - 1
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


@dataclass(frozen=True)
class McpRequestContext:
    """Server-owned protocol/cache identity; no credentials or approval grants."""

    protocol_version: McpProtocolVersion = McpProtocolVersion.legacy
    owner_id: str = ""
    server_id: str = ""
    configuration_revision: str = ""

    @classmethod
    def for_server(cls, server: UserMcpServer) -> McpRequestContext:
        identity = {
            "revision": health_config_revision(server),
            "endpoint": server.endpoint,
            "host": server.host,
            "transport": server.transport.value,
            "protocol": server.protocolVersion.value,
            "authMode": server.authMode.value,
            "secretRef": server.secretRef,
            "resourcesEnabled": server.resourcesEnabled,
        }
        return cls(
            protocol_version=server.protocolVersion,
            owner_id=server.userId,
            server_id=server.name,
            configuration_revision=hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        )


@dataclass(frozen=True)
class HeaderParameter:
    path: tuple[str, ...]
    name: str
    value_type: str


def _sensitive_label(label: str) -> bool:
    # Reuse the receipt redactor, including its signed-URL/gateway vocabulary.
    probe = f"{label}=value"
    return (
        is_fully_masked(redact_obj({label: True})[label])
        or redact(probe) != probe
        or label.casefold() in {"bearer", "cookie", "set-cookie"}
    )


def header_parameters(schema: dict[str, Any]) -> list[HeaderParameter]:
    """Validate every annotation, accepting only static ``properties`` paths.

    Sensitive annotations are deliberately rejected rather than silently omitted:
    omitting a required mirror would violate the upstream header/body contract.
    """
    found: list[HeaderParameter] = []
    names: set[str] = set()
    visited = 0

    def walk(node: Any, path: tuple[str, ...] | None, depth: int) -> None:
        nonlocal visited
        visited += 1
        if visited > MAX_SCHEMA_NODES or depth > MAX_SCHEMA_DEPTH:
            raise McpConnectionError("MCP parameter-header schema exceeds local bounds.")
        if isinstance(node, list):
            for child in node:
                walk(child, None, depth + 1)
            return
        if not isinstance(node, dict):
            return
        if "x-mcp-header" in node:
            name = node["x-mcp-header"]
            value_type = node.get("type")
            if (
                not path
                or not isinstance(name, str)
                or len(name) > MAX_HEADER_NAME
                or not _HEADER_TOKEN.fullmatch(name)
                or name.casefold() in names
                or value_type not in ("string", "integer", "boolean")
                or len(found) >= MAX_ROUTING_HEADERS
            ):
                raise McpConnectionError("MCP parameter-header annotation is invalid.")
            if (
                any(_sensitive_label(part) for part in (*path, name))
                or node.get("writeOnly") is True
                or node.get("format") == "password"
            ):
                raise McpConnectionError("MCP sensitive parameters cannot become routing headers.")
            names.add(name.casefold())
            found.append(HeaderParameter(path, name, value_type))
        for key, child in node.items():
            if key == "properties" and isinstance(child, dict):
                for prop, prop_schema in child.items():
                    walk(
                        prop_schema,
                        (*path, prop) if path is not None else None,
                        depth + 1,
                    )
            elif key in ("$defs", "definitions", "patternProperties", "dependentSchemas"):
                if isinstance(child, dict):
                    for sub_schema in child.values():
                        walk(sub_schema, None, depth + 1)
            elif key not in ("const", "enum", "default", "examples"):
                # Annotations behind composition, arrays, or other schema
                # keywords are not statically reachable, even if they look safe.
                if isinstance(child, (dict, list)):
                    walk(child, None, depth + 1)

    walk(schema, (), 0)
    return found


def encode_header_value(value: str) -> str:
    """The spec's UTF-8 Base64 sentinel, including literal-sentinel escaping."""
    if len(value) > MAX_HEADER_VALUE:
        raise McpConnectionError("MCP routing header exceeds local bounds.")
    if (
        value != value.strip(" \t")
        or any(not (0x20 <= ord(char) <= 0x7E or char == "\t") for char in value)
        or (value.startswith("=?base64?") and value.endswith("?="))
    ):
        try:
            encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        except UnicodeEncodeError as exc:
            raise McpConnectionError("MCP routing header has invalid Unicode.") from exc
        value = f"=?base64?{encoded}?="
    if len(value) > MAX_HEADER_VALUE:
        raise McpConnectionError("MCP routing header exceeds local bounds.")
    return value


def validate_resource_uri(uri: object) -> None:
    if (
        not isinstance(uri, str)
        or not uri
        or len(uri) > MAX_RESOURCE_URI_LEN
        or any(
            ord(char) < 0x20 or ord(char) == 0x7F or 0xD800 <= ord(char) <= 0xDFFF
            for char in uri
        )
    ):
        raise McpConnectionError("resources/read: invalid resource URI.")


def request_headers(
    body: dict[str, Any],
    *,
    protocol: McpProtocolVersion,
    input_schema: dict[str, Any] | None = None,
    secret: str | None = None,
) -> dict[str, str]:
    """Derive mirrors solely from the exact RPC body and consent-bound schema."""
    if not isinstance(protocol, McpProtocolVersion):
        raise McpConnectionError("Unsupported configured MCP protocol version.")
    method = body.get("method")
    params = body.get("params")
    if (
        body.get("jsonrpc") != "2.0"
        or type(body.get("id")) is not int
        or method not in (
            "initialize", "server/discover", "tools/list", "tools/call",
            "resources/list", "resources/read",
        )
        or not isinstance(params, dict)
    ):
        raise McpConnectionError("Malformed outbound MCP request.")
    if method == "tools/call":
        if not is_valid_remote_tool_name(params.get("name")) or not isinstance(
            params.get("arguments"), dict
        ):
            raise McpConnectionError("tools/call: invalid name or arguments.")
    if method == "resources/read":
        validate_resource_uri(params.get("uri"))
    if protocol is McpProtocolVersion.legacy:
        if method == "server/discover":
            raise McpConnectionError("server/discover requires MCP 2026-07-28.")
        return {} if method == "initialize" else {"MCP-Protocol-Version": protocol.value}
    meta = params.get("_meta")
    if (
        method == "initialize"
        or not isinstance(meta, dict)
        or meta.get(PROTOCOL_META) != protocol.value
        or meta.get(CAPABILITIES_META) != {}
    ):
        raise McpConnectionError("MCP request metadata contradicts configured protocol.")
    headers = {"MCP-Protocol-Version": meta[PROTOCOL_META], "Mcp-Method": method}
    if method in ("tools/call", "resources/read"):
        name = params["name"] if method == "tools/call" else params["uri"]
        headers["Mcp-Name"] = encode_header_value(name)
    if method == "tools/call":
        for parameter in header_parameters(input_schema or {}):
            value: Any = params["arguments"]
            for part in parameter.path:
                if not isinstance(value, dict):
                    raise McpConnectionError("MCP parameter-header argument path is invalid.")
                value = value.get(part)
                if value is None:
                    break
            if value is None:
                continue
            expected = {"string": str, "integer": int, "boolean": bool}[parameter.value_type]
            if type(value) is not expected or (
                parameter.value_type == "integer" and abs(value) > MAX_SAFE_INTEGER
            ):
                raise McpConnectionError("MCP parameter-header argument type or range is invalid.")
            text = str(value).lower() if isinstance(value, bool) else str(value)
            if redact(text) != text or (secret and secret in text):
                raise McpConnectionError("MCP sensitive values cannot become routing headers.")
            headers[f"Mcp-Param-{parameter.name}"] = encode_header_value(text)
    if sum(len(name) + len(value) for name, value in headers.items()) > MAX_ROUTING_HEADER_BYTES:
        raise McpConnectionError("MCP routing headers exceed local bounds.")
    return headers
