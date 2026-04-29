"""Selenium-based login flow for Mój E.ON.

Spawns chromium via chromedriver on-demand, performs login (handles reCAPTCHA
v3 naturally because it's a real browser fingerprint), then closes chromium.

We use Selenium instead of Playwright because Playwright has no musllinux
(Alpine) wheels — Selenium is pure Python and chromedriver is in Alpine apk.

RAM profile: ~30 MB idle (chromium not running), ~500 MB peak for ~30 s during
login. The browser is killed immediately after the cookie is captured.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .const import COOKIE_NAME, ENDPOINT_LOGIN, PAGE_DASHBOARD

_LOGGER = logging.getLogger(__name__)


class LoginError(Exception):
    """Login attempt failed."""


def _build_driver() -> webdriver.Chrome:
    chromium_path = os.environ.get(
        "CHROMIUM_BIN", "/usr/bin/chromium-browser"
    )
    chromedriver_path = os.environ.get(
        "CHROMEDRIVER_BIN", "/usr/bin/chromedriver"
    )

    opts = Options()
    opts.binary_location = chromium_path
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--lang=pl-PL")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    service = Service(executable_path=chromedriver_path)
    return webdriver.Chrome(service=service, options=opts)


def _login_sync(email: str, password: str, timeout_s: int) -> str:
    """Blocking login flow. Caller wraps in asyncio.to_thread()."""
    if not email or not password:
        raise LoginError("Empty email or password — set them in addon options")

    try:
        driver = _build_driver()
    except WebDriverException as exc:
        raise LoginError(f"Failed to launch chromium: {exc}") from exc

    try:
        _LOGGER.info("Selenium: navigating to %s", ENDPOINT_LOGIN)
        driver.set_page_load_timeout(timeout_s)
        driver.get(ENDPOINT_LOGIN)

        wait = WebDriverWait(driver, timeout_s)

        # Email + password fields. Selectors derived from inspecting eon.pl;
        # if E.ON changes the form, this is the place to update.
        email_el = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR,
            'input[name="Email"], input[type="email"], input[id*="mail" i]',
        )))
        email_el.clear()
        email_el.send_keys(email)

        pw_el = driver.find_element(
            By.CSS_SELECTOR,
            'input[name="Password"], input[type="password"]',
        )
        pw_el.clear()
        pw_el.send_keys(password)

        submit_el = driver.find_element(
            By.CSS_SELECTOR,
            'button[type="submit"], input[type="submit"]',
        )
        submit_el.click()

        # Wait for redirect to dashboard (out of /Logowanie)
        try:
            wait.until(lambda d: PAGE_DASHBOARD in d.current_url and
                       "Logowanie" not in d.current_url)
        except TimeoutException as exc:
            snippet = driver.page_source[:500]
            raise LoginError(
                f"Login did not redirect to dashboard within {timeout_s}s. "
                f"Page: {snippet}"
            ) from exc

        for c in driver.get_cookies():
            if c.get("name") == COOKIE_NAME and "eon.pl" in c.get("domain", ""):
                value = c.get("value", "")
                if value:
                    _LOGGER.info("Selenium login OK, captured %s", COOKIE_NAME)
                    return value
        raise LoginError(f"{COOKIE_NAME} not found in cookies after login")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


async def selenium_login(email: str, password: str, *, timeout_s: int = 60) -> str:
    """Async wrapper. Runs blocking Selenium calls in a thread."""
    return await asyncio.to_thread(_login_sync, email, password, timeout_s)


async def login_with_retry(
    email: str, password: str, *, attempts: int = 2
) -> str:
    """Run login() with simple retry on transient failures."""
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await selenium_login(email, password)
        except LoginError as exc:
            last_exc = exc
            _LOGGER.warning("Login attempt %d/%d failed: %s", i, attempts, exc)
            if i < attempts:
                await asyncio.sleep(5)
    assert last_exc is not None
    raise last_exc
