"""Gateway-backed embedder: embeds through the same model gateway as chat.

Keeping embeddings on the governed gateway (rather than a direct Foundry/OpenAI
client) means memory inherits the same auth, routing, and (later) cost telemetry
as every other model call.
"""
from __future__ import annotations

from collections.abc import Sequence

from ..gateway.client import ModelGatewayClient


class GatewayEmbedder:
    """Embeds text via :meth:`ModelGatewayClient.embed` for a fixed deployment."""

    def __init__(self, gateway: ModelGatewayClient, deployment: str) -> None:
        self._gateway = gateway
        self._deployment = deployment

    async def embed(self, inputs: Sequence[str]) -> list[list[float]]:
        return await self._gateway.embed(deployment=self._deployment, inputs=list(inputs))

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0] if vectors else []
