"""Shared fixtures: httpx mocked via MockTransport, sleep never really waits."""
from __future__ import annotations

import sys
import types

import httpx
import pytest

# The browser stack is never exercised by these tests; stubbing it keeps them
# independent of Chromium and of nodriver packaging quirks.
sys.modules.setdefault("nodriver", types.ModuleType("nodriver"))

from src import api
from src.config import AddonOptions, Runtime


@pytest.fixture
def make_runtime(tmp_path):
    def _make(**overrides) -> Runtime:
        opts = AddonOptions(
            email="user@example.com",
            password="pw",
            mqtt_discovery=False,
            **overrides,
        )
        return Runtime(
            options=opts, data_dir=str(tmp_path), ha_url="", ha_token=""
        )

    return _make


@pytest.fixture
def mock_httpx(monkeypatch):
    """Route every EonPolskaClient request through state["handler"]."""
    state = {"handler": None, "calls": []}
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append(str(request.url))
        return state["handler"](request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(api.httpx, "AsyncClient", factory)
    return state


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()
