"""Lifespan shutdown resilience: one resource's close()/aclose() failing must
never skip the rest, and a resource that was never assigned must not raise
AttributeError during cleanup.

Regression coverage for two related fixes to ``create_app``'s shutdown block:

1. ``http.aclose()`` (the shared httpx client used by the gateway) used to be
   the one unguarded call among ~20 cleanup steps; a raise there aborted every
   subsequent close (memory, usage, resource metrics, entitlements, ...).
2. ``app.state.session_repo`` was accessed directly instead of via
   ``getattr(app.state, "session_repo", None)`` like every sibling service in
   the same block, so a partially-initialized app.state would raise
   AttributeError instead of degrading gracefully.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from ai4ia_api.main import create_app
from tests.conftest import make_settings


def test_shutdown_survives_http_aclose_failure():
    closed = {"memory": False}

    class _FakeMemory:
        async def close(self) -> None:
            closed["memory"] = True

    app = create_app(make_settings())
    with patch.object(httpx.AsyncClient, "aclose", side_effect=RuntimeError("boom")):
        with TestClient(app) as client:
            # memory.close() runs after http.aclose() in the shutdown block; if
            # the http failure weren't independently guarded, this would never
            # be reached.
            client.app.state.memory = _FakeMemory()
    assert closed["memory"] is True


def test_shutdown_survives_missing_session_repo():
    app = create_app(make_settings())
    with TestClient(app) as client:
        del client.app.state.session_repo
    # Exiting the TestClient context runs the shutdown block; it must not
    # raise AttributeError even though session_repo is now unset.
