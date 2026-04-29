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

    debug_dir = os.environ.get("EON_DATA_DIR", "/data")

    try:
        _LOGGER.info("Selenium: navigating to %s", ENDPOINT_LOGIN)
        driver.set_page_load_timeout(timeout_s)
        driver.get(ENDPOINT_LOGIN)

        wait = WebDriverWait(driver, timeout_s)

        # Email field — eon.pl uses name="UserName" (not "Email").
        try:
            email_el = wait.until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                'input#UserName, input[name="UserName"]',
            )))
        except TimeoutException as exc:
            _dump_debug(driver, debug_dir, "email_field_missing")
            raise LoginError(
                f"Email field not found within {timeout_s}s — "
                f"login page may have changed (debug saved to {debug_dir})"
            ) from exc

        email_el.clear()
        email_el.send_keys(email)

        pw_el = driver.find_element(
            By.CSS_SELECTOR,
            'input#Password, input[name="Password"]',
        )
        pw_el.clear()
        pw_el.send_keys(password)

        # Submit button — type="button", click triggers JS submitForm() which
        # runs reCAPTCHA verification and POSTs to /mojeon/Logowanie.
        submit_el = driver.find_element(
            By.CSS_SELECTOR,
            'button[data-test-id="login-button"]',
        )
        submit_el.click()

        # Wait for redirect to /mojeon (out of /Logowanie). reCAPTCHA + POST
        # can take 5–15 s, so the timeout matters.
        try:
            wait.until(lambda d: PAGE_DASHBOARD in d.current_url and
                       "Logowanie" not in d.current_url)
        except TimeoutException as exc:
            _dump_debug(driver, debug_dir, "no_redirect_after_submit")
            err_text = _try_capture_error(driver)
            raise LoginError(
                f"Login did not redirect to dashboard within {timeout_s}s. "
                f"Page error: {err_text}"
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


def _dump_debug(driver: Any, data_dir: str, tag: str) -> None:
    """Save page HTML + screenshot to /data for post-mortem inspection."""
    try:
        html_path = os.path.join(data_dir, f"login_debug_{tag}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        png_path = os.path.join(data_dir, f"login_debug_{tag}.png")
        driver.save_screenshot(png_path)
        _LOGGER.warning("Login debug dumped to %s and %s", html_path, png_path)
    except Exception as exc:
        _LOGGER.debug("Debug dump failed: %s", exc)


def _try_capture_error(driver: Any) -> str:
    """Read any visible validation message from the form."""
    try:
        for sel in (
            ".validation-msg",
            ".validation-msg-recaptcha",
            "#recaptcha-error-banner",
            ".form-validation-msg",
        ):
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                txt = (el.text or "").strip()
                if txt:
                    return txt
    except Exception:
        pass
    return f"current URL: {driver.current_url}"


async def selenium_login(email: str, password: str, *, timeout_s: int = 90) -> str:
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
