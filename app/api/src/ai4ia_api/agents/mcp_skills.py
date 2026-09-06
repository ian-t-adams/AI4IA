"""Progressive disclosure for repository-curated Foundry Toolbox skills.

Foundry exposes toolbox-attached skills as MCP resources. AI4IA advertises only
their bounded name/description metadata to the model, then offers one synthetic
``load_skill`` tool that reads the selected ``SKILL.md`` on demand. The full
instructions therefore consume context only when selected.

Resource discovery is restricted to official catalog entries explicitly marked
``resourcesEnabled``. The loader also re-checks that the exact URI was advertised
before every read. Once the tool returns, :func:`run_agent_turn` marks the turn as
tainted just as it does for every tool result, so later external calls retain the
normal exact-argument approval gate.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib.parse import parse_qs, urlparse

from .consent import contract_hash
from .mcp_client import McpResourceResult
from .mcp_health import is_quarantined
from .mcp_servers import UserMcpServer
from .tool_exec import ToolContext, ToolDefinition, ToolExecutionError
from .tools import ToolRisk, ToolSpec

LOAD_SKILL_NAME = "load_skill"
MAX_ADVERTISED_SKILLS = 32
MAX_LOAD_SKILL_DESCRIPTION = 2_000

_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_ALLOWED_MIME_TYPES = {None, "text/markdown", "text/plain"}


class OfficialResourceReader(Protocol):
    async def read_resource(
        self, server: UserMcpServer, uri: str
    ) -> McpResourceResult: ...


@dataclass(frozen=True)
class DiscoveredSkill:
    """One instruction-only skill advertised by a curated toolbox."""

    name: str
    description: str
    uri: str
    server: UserMcpServer
    version: str | None = None
    mime_type: str | None = None


def _skill_name_from_uri(uri: str) -> tuple[str | None, str | None]:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "skill":
        return None, None

    name = parsed.netloc or parsed.path.strip("/").split("/", 1)[0]
    path = parsed.path.strip("/")
    if parsed.netloc and path and path.casefold() != "skill.md":
        return None, None
    if not parsed.netloc and "/" in path:
        name, leaf = path.split("/", 1)
        if leaf.casefold() != "skill.md":
            return None, None
    if not _SKILL_NAME_RE.fullmatch(name) or "--" in name:
        return None, None

    versions = parse_qs(parsed.query).get("version", [])
    version = versions[0][:64] if versions and versions[0] else None
    return name, version


def discover_skills(
    servers: Sequence[UserMcpServer],
) -> list[DiscoveredSkill]:
    """Project skill resources from curated servers, first catalog entry wins."""
    skills: list[DiscoveredSkill] = []
    seen: set[str] = set()
    for server in servers:
        if not server.enabled or not server.resourcesEnabled or is_quarantined(server):
            continue
        for resource in server.discoveredResources:
            if resource.mimeType not in _ALLOWED_MIME_TYPES:
                continue
            name, version = _skill_name_from_uri(resource.uri)
            if name is None or name in seen:
                continue
            seen.add(name)
            skills.append(
                DiscoveredSkill(
                    name=name,
                    description=resource.description or resource.name or name,
                    uri=resource.uri,
                    server=server,
                    version=version,
                    mime_type=resource.mimeType,
                )
            )
            if len(skills) >= MAX_ADVERTISED_SKILLS:
                return skills
    return skills


def _description(skills: Sequence[DiscoveredSkill]) -> str:
    lines = [
        "Load one repository-curated Foundry skill when its instructions are "
        "relevant. Available skills:"
    ]
    for skill in skills:
        lines.append(f"- {skill.name}: {skill.description}")
    return "\n".join(lines)[:MAX_LOAD_SKILL_DESCRIPTION]


def build_load_skill_definition(
    *,
    servers: Sequence[UserMcpServer],
    reader: OfficialResourceReader,
) -> ToolDefinition | None:
    """Build the governed progressive-disclosure tool for discovered skills."""
    skills = discover_skills(servers)
    if not skills:
        return None
    by_name = {skill.name: skill for skill in skills}

    async def handler(args: dict, _ctx: ToolContext) -> dict:
        name = args.get("name")
        skill = by_name.get(name) if isinstance(name, str) else None
        if skill is None:
            raise ToolExecutionError("Requested skill is not available for this turn.")
        try:
            resource = await reader.read_resource(skill.server, skill.uri)
        except Exception as exc:
            raise ToolExecutionError("The selected skill could not be loaded.") from exc
        content_digest = hashlib.sha256(resource.text.encode("utf-8")).hexdigest()
        return {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version or "default",
            "source": {
                "server": skill.server.name,
                "uri": resource.uri,
                "mimeType": resource.mime_type,
            },
            "contentSha256": content_digest,
            "truncated": resource.truncated,
            "instructions": resource.text,
        }

    return ToolDefinition(
        spec=ToolSpec(
            name=LOAD_SKILL_NAME,
            description=_description(skills),
            risk=ToolRisk.safe,
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [skill.name for skill in skills],
                    "description": "Exact name of the skill to load.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=handler,
        consent_metadata={
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "uri": skill.uri,
                    "version": skill.version,
                    "mimeType": skill.mime_type,
                    "server": skill.server.model_dump(
                        mode="json",
                        include={
                            "name", "userId", "endpoint", "host", "transport", "authMode",
                            "configurationRevision", "trusted", "enabled", "resourcesEnabled",
                        },
                    ),
                    "credentialRefHash": contract_hash(skill.server.secretRef),
                }
                for skill in skills
            ],
        },
    )
