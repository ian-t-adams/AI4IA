"""Explicit offline-only browser fixture; never imported by the shipping app."""
from __future__ import annotations

import argparse
import json
import socket
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import patch

import httpx
import uvicorn
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from ai4ia_api.config import Settings
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app


class OfflineSettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls, settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource, env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource, file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # A workstation's credentials, cloud endpoints and feature flags are not
        # inputs to this fixture, including through the ambient environment.
        return (init_settings,)


def build_preview():
    settings = OfflineSettings(
        env="local", auth_provider="dev", allow_dev_auth=True,
        session_store="memory", model_gateway_url="http://gateway.test",
    )
    app = create_app(settings)
    original_lifespan = app.router.lifespan_context
    calls: list[dict] = []

    def native(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "gateway.test"
        assert request.url.path.endswith("/systemone") and not request.url.query
        body = json.loads(request.content)
        calls.append(body)
        if body["state"] == "fixture:upstream-error":
            return httpx.Response(503, stream=httpx.ByteStream(b'{"detail":"Synthetic provider failure"}'))
        answers = {}
        for name, question in body["questions"].items():
            kind = question["type"]
            if kind == "noul":
                answers[name] = {"type": kind, "noul": .75}
            else:
                criteria = question["criteria"]
                keys = list(criteria) if kind == "choice" else [str(i) for i in range(len(criteria))]
                answers[name] = {
                    "type": kind, "confidence": .5,
                    "probabilities": {key: (1.0 if i == 0 else 0.0) for i, key in enumerate(keys)},
                    **({"choice": keys[0]} if kind == "choice" else {"score": 0.0}),
                }
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=httpx.ByteStream(json.dumps({
            "model": "microsoft-decision-1", "answers": answers,
            "usage": {"input_tokens": 123, "output_tokens": 7},
        }).encode()))

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
                app.state.gateway = ModelGatewayClient(settings, client)
                app.state.preview_native_calls = calls
                yield

    app.router.lifespan_context = lifespan
    return app


@contextmanager
def offline_network():
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def loopback_only(method):
        def guarded(sock, address):
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
                raise OSError("Offline Decision fixture refuses non-loopback egress")
            return method(sock, address)
        return guarded

    with patch.object(socket.socket, "connect", loopback_only(connect)), patch.object(
        socket.socket, "connect_ex", loopback_only(connect_ex),
    ):
        yield


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3220)
    args = parser.parse_args()
    with offline_network():
        uvicorn.run(build_preview(), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
