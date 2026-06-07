"""Parsing of ``@agent`` mentions and ``/command`` directives in chat input.

These are product-surface affordances the chat layer understands:

- ``@name`` at the very start of a turn routes that turn to a named agent.
- ``/name [args]`` invokes an action command (e.g. ``/system You are ...``).

Parsing here is pure and side-effect free. Resolving an agent name to a real
agent, or executing a command, is the caller's responsibility — this module
only tells the caller *what* was requested.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class CommandKind(str, Enum):
    """Built-in action commands the chat layer recognizes."""

    help = "help"
    clear = "clear"
    system = "system"
    model = "model"
    agents = "agents"
    summarize = "summarize"
    forget = "forget"
    # Anything that looks like a command but isn't known.
    unknown = "unknown"


# A mention/command name looks like an identifier.
_NAME = r"[A-Za-z][A-Za-z0-9_.-]*"
_MENTION_RE = re.compile(rf"^@({_NAME})(?:\b\s*)")
_COMMAND_RE = re.compile(rf"^/({_NAME})(?:[ \t]+(.*))?$", re.DOTALL)

_KNOWN_COMMANDS = {k.value for k in CommandKind if k is not CommandKind.unknown}


@dataclass(frozen=True)
class SlashCommand:
    name: str
    kind: CommandKind
    args: str


@dataclass(frozen=True)
class ParsedInput:
    raw: str
    agent: str | None
    command: SlashCommand | None
    text: str

    @property
    def is_command(self) -> bool:
        return self.command is not None


def parse_input(content: str) -> ParsedInput:
    """Split a raw chat turn into an optional leading agent mention, an optional
    leading slash command, and the remaining text.

    A slash command is only recognized as the first token of the turn (after an
    optional ``@mention``); a slash anywhere else (e.g. ``"1/2"``) is left in the
    text untouched. For commands, ``text`` is the command's argument string.
    """
    rest = content.lstrip()
    agent: str | None = None

    mention = _MENTION_RE.match(rest)
    if mention:
        agent = mention.group(1).lower()
        rest = rest[mention.end():]

    command: SlashCommand | None = None
    cmd_match = _COMMAND_RE.match(rest)
    if cmd_match:
        name = cmd_match.group(1).lower()
        args = (cmd_match.group(2) or "").strip()
        kind = CommandKind(name) if name in _KNOWN_COMMANDS else CommandKind.unknown
        command = SlashCommand(name=name, kind=kind, args=args)
        rest = args

    return ParsedInput(raw=content, agent=agent, command=command, text=rest.strip())
