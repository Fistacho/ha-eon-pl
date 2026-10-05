"""Retry policy for transient network failures.

A network outage (DNS not ready after a host reboot, Wi-Fi flap, portal 5xx)
is not a dead session: it must never crash the addon and never trigger the
expensive browser login. Callers wrap a single I/O operation in
``retry_transient`` which waits with exponential backoff until it succeeds
or a stop is requested.

Why unbounded: giving up means either crashing the container (the incident
this module fixes: ``issue_addon_boot_fail``, the addon stayed down for
hours) or running degraded forever. One cheap request per ``max_delay`` is
harmless, and the wait is interruptible, so there is no reason to cap it.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, TypeVar

import httpx

from .api import EonTransientError

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# httpx.TransportError covers ConnectError (incl. DNS EAI_AGAIN), all
# timeouts, ReadError, WriteError, ProxyError, ... EonTransientError is a
# 5xx/429 answer from the portal.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    EonTransientError,
)

INITIAL_DELAY = 5.0
MAX_DELAY = 300.0
BACKOFF_FACTOR = 2.0
LOG_EVERY = 10  # INFO "still failing" every N-th failed attempt


class RetryAborted(Exception):
    """Stop was requested (SIGTERM) while waiting for the network."""


async def retry_transient(
    op: Callable[[], Awaitable[T]],
    *,
    what: str,
    sleep: Callable[[float], Awaitable[None]],
    should_stop: Callable[[], bool],
    initial_delay: float = INITIAL_DELAY,
    max_delay: float = MAX_DELAY,
    factor: float = BACKOFF_FACTOR,
) -> T:
    """Run ``op`` until it stops raising transient network errors.

    Any other exception propagates immediately. ``sleep`` must return early
    when a stop is requested; ``should_stop`` is then checked and
    ``RetryAborted`` raised. Task cancellation (CancelledError) is never
    swallowed.
    """
    attempt = 0
    delay = initial_delay
    while True:
        try:
            result = await op()
        except TRANSIENT_ERRORS as exc:
            attempt += 1
            reason = f"{type(exc).__name__}: {exc}"
            if attempt == 1:
                _LOGGER.warning(
                    "%s: network/portal unavailable (%s) — retrying with "
                    "backoff %.0f s .. %.0f s",
                    what, reason, initial_delay, max_delay,
                )
            elif attempt % LOG_EVERY == 0:
                _LOGGER.info(
                    "%s: still unavailable after %d attempts (%s)",
                    what, attempt, reason,
                )
            else:
                _LOGGER.debug(
                    "%s: attempt %d failed (%s), next in %.0f s",
                    what, attempt, reason, delay,
                )
            if should_stop():
                raise RetryAborted(what) from exc
            await sleep(delay)
            if should_stop():
                raise RetryAborted(what) from exc
            delay = min(delay * factor, max_delay)
            continue
        if attempt:
            _LOGGER.info("%s: connection restored after %d failed attempt(s)",
                         what, attempt)
        return result
