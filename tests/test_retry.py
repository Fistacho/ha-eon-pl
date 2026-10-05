from __future__ import annotations

import logging

import httpx
import pytest

from src.api import EonTransientError
from src.retry import RetryAborted, retry_transient


async def test_returns_immediately_without_sleep(fake_sleep):
    async def op():
        return 42

    assert await retry_transient(
        op, what="x", sleep=fake_sleep, should_stop=lambda: False
    ) == 42
    assert fake_sleep.delays == []


async def test_backoff_grows_and_caps(fake_sleep):
    n = {"i": 0}

    async def op():
        n["i"] += 1
        if n["i"] <= 9:
            raise httpx.ConnectError("[Errno -3] Try again")
        return "ok"

    assert await retry_transient(
        op, what="x", sleep=fake_sleep, should_stop=lambda: False
    ) == "ok"
    assert fake_sleep.delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("dns"),
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        EonTransientError("HTTP 503"),
    ],
)
async def test_transient_exceptions_are_retried(fake_sleep, exc):
    calls = {"i": 0}

    async def op():
        calls["i"] += 1
        if calls["i"] == 1:
            raise exc
        return True

    assert await retry_transient(
        op, what="x", sleep=fake_sleep, should_stop=lambda: False
    )
    assert len(fake_sleep.delays) == 1


async def test_non_transient_exception_propagates(fake_sleep):
    async def op():
        raise ValueError("bug")

    with pytest.raises(ValueError):
        await retry_transient(
            op, what="x", sleep=fake_sleep, should_stop=lambda: False
        )
    assert fake_sleep.delays == []


async def test_stop_aborts_retry():
    stop = {"v": False}

    async def op():
        raise httpx.ConnectError("down")

    async def sleep(delay):
        stop["v"] = True  # SIGTERM arrives while waiting

    with pytest.raises(RetryAborted):
        await retry_transient(
            op, what="x", sleep=sleep, should_stop=lambda: stop["v"]
        )


async def test_logging_policy(fake_sleep, caplog):
    caplog.set_level(logging.DEBUG, logger="src.retry")
    n = {"i": 0}

    async def op():
        n["i"] += 1
        if n["i"] <= 12:
            raise httpx.ConnectError("[Errno -3] Try again")
        return 1

    await retry_transient(
        op, what="E.ON", sleep=fake_sleep, should_stop=lambda: False
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 1
    assert "ConnectError" in warnings[0].getMessage()
    assert "Try again" in warnings[0].getMessage()
    # attempt 10 (still failing) + final "restored"
    assert len(infos) == 2
    assert "restored" in infos[-1].getMessage()
