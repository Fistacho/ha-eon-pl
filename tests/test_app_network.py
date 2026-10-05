"""Network outage at startup / in loops must not kill the process."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from src import __main__ as main_mod
from src.__main__ import App

GOOD = {"Partners": [{"ContractAccounts": []}], "HasOze": False}


def _seed_cookie(app: App, value: str = "abc") -> None:
    app.cookie_store.save(value)


class Flaky:
    """Fails with ConnectError n times, then answers with `then`."""

    def __init__(self, failures: int, then) -> None:
        self.failures = failures
        self.then = then
        self.seen = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen += 1
        if self.seen <= self.failures:
            raise httpx.ConnectError("[Errno -3] Try again", request=request)
        return self.then(request)


def _ok(request):
    return httpx.Response(200, json=GOOD)


def _forbid_login(app: App) -> None:
    async def no_login():
        raise AssertionError("browser login must not run here")

    app.relogin = no_login  # type: ignore[assignment]


async def test_ensure_cookie_waits_for_network_and_reuses_cookie(
    make_runtime, mock_httpx, fake_sleep
):
    app = App(make_runtime(), sleep=fake_sleep)
    _seed_cookie(app)
    mock_httpx["handler"] = Flaky(3, _ok)
    _forbid_login(app)
    cookie = await app.ensure_cookie()
    assert "abc" in cookie
    assert fake_sleep.delays == [5, 10, 20]


async def test_dead_session_still_goes_to_browser_login(
    make_runtime, mock_httpx, fake_sleep
):
    app = App(make_runtime(), sleep=fake_sleep)
    _seed_cookie(app)
    mock_httpx["handler"] = lambda r: httpx.Response(401)
    logins = []

    async def fake_login():
        logins.append(1)
        return "fresh"

    app.relogin = fake_login  # type: ignore[assignment]
    assert await app.ensure_cookie() == "fresh"
    assert logins == [1]
    assert fake_sleep.delays == []


async def test_empty_partners_is_dead_session_not_network(
    make_runtime, mock_httpx, fake_sleep
):
    app = App(make_runtime(), sleep=fake_sleep)
    _seed_cookie(app)
    mock_httpx["handler"] = lambda r: httpx.Response(200, json={"Partners": []})

    async def fake_login():
        return "fresh"

    app.relogin = fake_login  # type: ignore[assignment]
    assert await app.ensure_cookie() == "fresh"
    assert fake_sleep.delays == []


async def test_5xx_is_transient_not_dead_session(
    make_runtime, mock_httpx, fake_sleep
):
    app = App(make_runtime(), sleep=fake_sleep)
    _seed_cookie(app)
    seq = iter([503, 502, 200])

    def handler(request):
        code = next(seq)
        return httpx.Response(code, json=GOOD if code == 200 else {})

    mock_httpx["handler"] = handler
    _forbid_login(app)
    assert await app.ensure_cookie()
    assert fake_sleep.delays == [5, 10]


class _FakeRunner:
    def __init__(self, app):
        pass

    async def setup(self):
        pass

    async def cleanup(self):
        pass


def _patch_web(monkeypatch, started: asyncio.Event):
    class FakeSite:
        def __init__(self, runner, host, port):
            pass

        async def start(self):
            started.set()

    monkeypatch.setattr(main_mod.web, "AppRunner", _FakeRunner)
    monkeypatch.setattr(main_mod.web, "TCPSite", FakeSite)
    monkeypatch.setattr(main_mod, "build_app", lambda **kw: None)


async def test_run_survives_dns_failure_at_boot(
    make_runtime, mock_httpx, fake_sleep, monkeypatch
):
    started = asyncio.Event()
    _patch_web(monkeypatch, started)
    app = App(make_runtime(), sleep=fake_sleep)
    _seed_cookie(app)
    mock_httpx["handler"] = Flaky(2, _ok)

    task = asyncio.create_task(app.run())
    waiter = asyncio.create_task(started.wait())
    done, _ = await asyncio.wait(
        {task, waiter}, timeout=5, return_when=asyncio.FIRST_COMPLETED
    )
    assert done, "run() neither started the web UI nor finished"
    if task in done:
        task.result()  # re-raises the escaped exception (the incident)
        pytest.fail("run() returned before shutdown")
    assert fake_sleep.delays[:2] == [5, 10]
    app.request_stop()
    await asyncio.wait_for(task, 5)


async def test_stop_during_boot_retry_exits_cleanly(
    make_runtime, mock_httpx, monkeypatch
):
    started = asyncio.Event()
    _patch_web(monkeypatch, started)
    holder: dict[str, App] = {}

    async def sleep(delay):
        holder["app"].request_stop()  # SIGTERM while waiting for network

    app = App(make_runtime(), sleep=sleep)
    holder["app"] = app
    _seed_cookie(app)

    def always_down(request):
        raise httpx.ConnectError("down", request=request)

    mock_httpx["handler"] = always_down
    await asyncio.wait_for(app.run(), 5)
    assert not started.is_set()


async def test_loop_fetch_retries_on_network_error_instead_of_dying(
    make_runtime, fake_sleep
):
    app = App(make_runtime(), sleep=fake_sleep)
    calls = {"i": 0}

    async def no_wait(delay):  # scan-interval wait
        pass

    app._interruptible_sleep = no_wait  # type: ignore[assignment]

    async def fake_fetch_once():
        calls["i"] += 1
        if calls["i"] < 3:
            raise httpx.ConnectError("[Errno -3] Try again")
        app.request_stop()

    app.fetch_once = fake_fetch_once  # type: ignore[assignment]
    await asyncio.wait_for(app.loop_fetch(), 5)
    assert calls["i"] == 3
    assert fake_sleep.delays == [5, 10]


async def test_initial_fetch_network_error_is_deferred_to_background(
    make_runtime, mock_httpx, monkeypatch
):
    started = asyncio.Event()
    _patch_web(monkeypatch, started)
    delays: list[float] = []

    async def yielding_sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    app = App(make_runtime(), sleep=yielding_sleep)
    _seed_cookie(app)
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        if seen["n"] == 1:  # session check succeeds
            return httpx.Response(200, json=GOOD)
        raise httpx.ConnectError("[Errno -3] Try again", request=request)

    mock_httpx["handler"] = handler
    task = asyncio.create_task(app.run())
    await asyncio.wait_for(started.wait(), 5)
    for _ in range(50):  # let loop_fetch start retrying in the background
        if delays:
            break
        await asyncio.sleep(0.01)
    assert delays[:2] == [5, 10]
    app.request_stop()
    await asyncio.wait_for(task, 5)
