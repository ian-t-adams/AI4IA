"""Shared fixtures for image-edit tests: catalogs, sources and a recording gateway.

Every edit test drives the real app, the real availability predicate and the real
source resolver. Only the provider is replaced, and the replacement records the
exact bytes it was handed so tests can prove what would have left the process.
"""
from __future__ import annotations

import base64
import json
import struct
import zlib
from collections.abc import Iterable
from pathlib import Path

import ai4ia_api
from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.sessions.models import Message, MessageAttachment, MessageRole

PACKAGED_CATALOG = Path(ai4ia_api.__file__).resolve().parent / "data" / "model_catalog.json"
EDIT_TOOL = "edit_image"
SUNBURST = "gpt-image-2.5-sunburst"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(
        ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
    )


def png_bytes(width: int = 40, height: int = 30, *, seed: int = 0x7F) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([seed]) * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")
    )


SOURCE_PNG = png_bytes()
EDITED_PNG = png_bytes(8, 8, seed=0x11)
EDITED_B64 = base64.b64encode(EDITED_PNG).decode()


def image_edit_catalog_path(
    directory: Path, *, disabled: Iterable[str] = (), name: str = "catalog",
) -> str:
    """A copy of the packaged catalog with ``disabled`` rows runtime-disabled."""
    raw = json.loads(PACKAGED_CATALOG.read_text(encoding="utf-8"))
    targets = set(disabled)
    for model in raw["models"]:
        if model["id"] in targets:
            model["runtimeEnabled"] = False
    path = directory / f"{name}-{'-'.join(sorted(targets)) or 'shipped'}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return str(path)


def editing_model_ids() -> list[str]:
    raw = json.loads(PACKAGED_CATALOG.read_text(encoding="utf-8"))
    return [m["id"] for m in raw["models"] if m.get("imageEditing") is True]


class FakeEditGateway:
    """Records edit calls; optionally drives an ``edit_image`` tool call in chat."""

    def __init__(self, *, tool_arguments: dict | None = None) -> None:
        self.edit_calls: list[dict] = []
        self.generate_calls: list[dict] = []
        self.offered: list[str] = []
        self.model_calls = 0
        self.error: ModelGatewayError | None = None
        self.reply: dict | None = None
        self.tool_arguments = tool_arguments or {"prompt": "make the sky purple"}

    async def edit_image(self, **kwargs):
        self.edit_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.reply is not None:
            return self.reply
        return {
            "data": [{"b64_json": EDITED_B64}],
            "usage": {"input_tokens": 20, "output_tokens": 200, "total_tokens": 220},
        }

    async def generate_image(self, **kwargs):
        self.generate_calls.append(kwargs)
        return {"data": [{"b64_json": base64.b64encode(SOURCE_PNG).decode()}]}

    async def complete(self, *, messages, params=None, **kwargs):
        self.model_calls += 1
        offered = [tool["function"]["name"] for tool in (params or {}).get("tools", [])]
        self.offered.extend(offered)
        if EDIT_TOOL in offered and not any(message["role"] == "tool" for message in messages):
            message = {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "edit-call", "type": "function",
                    "function": {"name": EDIT_TOOL, "arguments": json.dumps(self.tool_arguments)},
                }],
            }
        else:
            message = {"role": "assistant", "content": "Done."}
        return {"choices": [{"message": message}]}


async def seed_generated_image(
    app, user_id: str, session_id: str, artifact_id: str, data: bytes = SOURCE_PNG,
    *, kind: str = "image",
) -> None:
    """Store an owned artifact and reference it from an assistant message."""
    await app.state.image_artifacts.put(user_id, artifact_id, data)
    await app.state.session_repo.add_message(user_id, Message(
        sessionId=session_id, userId=user_id, role=MessageRole.assistant,
        content="Here is your image.",
        attachments=[MessageAttachment(
            id=artifact_id, kind=kind, prompt="a lighthouse", model="gpt-image-2",
            status="complete" if kind == "image" else "error",
        )],
    ))
