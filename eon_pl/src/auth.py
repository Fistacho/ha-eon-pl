"""Playwright-based login flow for Mój E.ON.

Spawns chromium on-demand, performs an interactive login (handles reCAPTCHA v3
naturally because it's a real browser fingerprint), then closes chromium.

RAM profile: ~30 MB idle (chromium not running), ~500 MB peak for ~30 s during
login. The browser is killed immediately after the cookie is captured.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from playwright.async_api import async_playwright

from .const import COOKIE_NAME, ENDPOINT_LOGIN, PAGE_DASHBOARD

_LOGGER = logging.getLogger(__name__)


class LoginError(Exception):
    """Login attempt failed."""


async def playwright_login(email: str, password: str, *, timeout_s: int = 60) -> str:
    """Log into eon.pl, return the .AspNet.Cookies value.

    Raises LoginError on bad credentials, captcha block, or timeout.
    """
    if not email or not password:
        raise LoginError("Empty email or password — set them in addon options")

    chromium_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    launch_kwargs: dict[str, Any] = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="pl-PL",
            )
            page = await context.new_page()

            _LOGGER.info("Playwright: navigating to %s", ENDPOINT_LOGIN)
            await page.goto(ENDPOINT_LOGIN, wait_until="domcontentloaded", timeout=timeout_s * 1000)

            # The login form fields — selectors derived from inspecting eon.pl.
            # If E.ON changes the form structure, this is the place to adapt.
            await page.fill('input[name="Email"], input[type="email"]', email)
            await page.fill('input[name="Password"], input[type="password"]', password)
            await page.click('button[type="submit"], input[type="submit"]')

            # Wait for either successful redirect to /mojeon or an error message.
            try:
                await page.wait_for_url(
                    lambda url: PAGE_DASHBOARD in url and "Logowanie" not in url,
                    timeout=timeout_s * 1000,
                )
            except Exception as exc:
                # Capture the page state for diagnostics
                content_snippet = (await page.content())[:500]
                raise LoginError(
                    f"Login did not redirect to dashboard — {exc}. Page: {content_snippet}"
                ) from exc

            cookies = await context.cookies()
            for c in cookies:
                if c.get("name") == COOKIE_NAME and "eon.pl" in c.get("domain", ""):
                    value = c.get("value", "")
                    if value:
                        _LOGGER.info("Playwright login OK, captured %s", COOKIE_NAME)
                        return value
            raise LoginError(f"{COOKIE_NAME} not found in cookies after login")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def login_with_retry(
    email: str, password: str, *, attempts: int = 2
) -> str:
    """Run login() with simple retry on transient failures."""
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await playwright_login(email, password)
        except LoginError as exc:
            last_exc = exc
            _LOGGER.warning("Login attempt %d/%d failed: %s", i, attempts, exc)
            if i < attempts:
                await asyncio.sleep(5)
    assert last_exc is not None
    raise last_exc
