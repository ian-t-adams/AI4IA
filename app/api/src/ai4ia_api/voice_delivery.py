"""Server-owned delivery guidance for live spoken sessions.

Every word a live voice session produces is heard, not read: a native-audio
model speaks its own reply, and the Azure Speech chain's text-to-speech reads a
text model's reply verbatim. A model that believes it is in a text chat answers
with paragraphs, lists and markdown, which are then read aloud.

The relay therefore composes one short, versioned delivery instruction into
every live session's instructions. Persona or saved conversation instructions
stay first and byte-for-byte unchanged; the guidance follows them, or stands
alone when there are none. Only the relay composes it: the browser sends no
instructions, and any it sends are replaced. The guidance changes only the
spoken form of replies and says so, so it never replaces persona, tool, safety
or policy instructions.
"""
from __future__ import annotations

VOICE_DELIVERY_GUIDANCE_VERSION = "v1"
# Fixed receipt marker. It stays under 32 characters so the receipt redactor,
# which masks longer opaque tokens, keeps it readable.
VOICE_DELIVERY_RECEIPT_NOTE = f"voice_delivery_guidance_{VOICE_DELIVERY_GUIDANCE_VERSION}"

_RULES = (
    "Everything you write is spoken aloud, so talk the way people talk: usually one "
    "to three short sentences unless the user asks for more detail, and offer to go "
    "deeper instead of listing everything. Never use markdown, bullet or numbered "
    "lists, headings, tables, code blocks, URLs or emoji; say it in plain words "
    "instead. Ask at most one question at a time. If you didn't catch something, "
    "say so briefly and ask the user to repeat it."
)
_PRECEDENCE = (
    "These rules change only how your replies are spoken; follow the instructions "
    "above for everything else."
)


def voice_delivery_guidance(*, avatar: bool, after_instructions: bool) -> str:
    """The delivery instruction; ``avatar`` adds that the user sees one speaking."""
    setting = "This is a live spoken conversation" + (
        ", and the user sees an animated avatar speaking your replies" if avatar else ""
    )
    parts = ["Voice delivery:", f"{setting}.", _RULES]
    if after_instructions:
        parts.append(_PRECEDENCE)
    return " ".join(parts)


def compose_voice_instructions(base: str | None, *, avatar: bool) -> str:
    """``base`` first and unchanged, then the delivery guidance (alone without one)."""
    if base is None or not base.strip():
        return voice_delivery_guidance(avatar=avatar, after_instructions=False)
    return f"{base}\n\n{voice_delivery_guidance(avatar=avatar, after_instructions=True)}"
