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
import random
import re
import subprocess
import time
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium_stealth import stealth

from .const import COOKIE_NAME, ENDPOINT_LOGIN, PAGE_DASHBOARD

_LOGGER = logging.getLogger(__name__)


class LoginError(Exception):
    """Login attempt failed."""


def _detect_chromium_version(chromium_path: str) -> str:
    """Return installed Chromium version (e.g. '131.0.6778.139')."""
    try:
        r = subprocess.run(
            [chromium_path, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else "131.0.0.0"
    except Exception:
        return "131.0.0.0"


def _kill_stale_chromium() -> None:
    """Kill orphaned chromedriver/chromium processes from previous failed attempts.

    Each failed webdriver.Chrome() init leaves a zombie chromedriver (and the
    Chromium it spawned) because service.stop() is never called on exception.
    After a few rounds these exhaust memory and cause every new attempt to hang.
    """
    for pattern in ("chromedriver", "chromium-browser", "chromium"):
        try:
            subprocess.run(
                ["pkill", "-9", "-f", pattern],
                capture_output=True, timeout=5, check=False,
            )
        except Exception:
            pass
    time.sleep(0.8)


def _build_driver() -> webdriver.Chrome:
    chromium_path = os.environ.get(
        "CHROMIUM_BIN", "/usr/bin/chromium-browser"
    )
    chromedriver_path = os.environ.get(
        "CHROMEDRIVER_BIN", "/usr/bin/chromedriver"
    )

    version = _detect_chromium_version(chromium_path)
    _LOGGER.debug("Chromium version: %s", version)

    opts = Options()
    opts.binary_location = chromium_path
    # No --headless: we run on Xvfb (DISPLAY=:99 set by xvfb-run wrapper).
    # reCAPTCHA v3 scores --headless=new fingerprints very low; non-headless
    # on a virtual X server scores roughly the same as a real desktop browser.
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--lang=pl-PL")
    # Chromium 120+ on containers without D-Bus hangs waiting for the system
    # keyring; --password-store=basic bypasses the keyring entirely.
    opts.add_argument("--password-store=basic")
    # Force X11 backend regardless of XDG_SESSION_TYPE inherited from host.
    opts.add_argument("--ozone-platform=x11")
    # Use ANGLE with SwiftShader backend — this is how Chrome normally handles
    # WebGL (even on real hardware Chrome routes WebGL through ANGLE). SwiftShader
    # is bundled inside Chromium so no system GL library is needed. The GPU
    # process starts cleanly without /dev/dri, WebGL works, and canvas/WebGL
    # fingerprints follow the normal Chrome GPU code path that reCAPTCHA expects.
    # --disable-gpu is intentionally NOT set: it kills the GPU process entirely,
    # creating a unique "no GPU" browser fingerprint that reCAPTCHA scores as bot.
    opts.add_argument("--use-gl=angle")
    opts.add_argument("--use-angle=swiftshader")
    opts.add_argument(
        f"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    debug_dir = os.environ.get("EON_DATA_DIR", "/data")
    log_path = os.path.join(debug_dir, "chromedriver.log")
    service = Service(executable_path=chromedriver_path, log_output=log_path)
    driver = webdriver.Chrome(service=service, options=opts)

    # Apply selenium-stealth fingerprint masking. This patches:
    # navigator.webdriver, navigator.languages, navigator.plugins,
    # WebGL renderer/vendor, screen size etc — enough to bring reCAPTCHA v3
    # score back into "human" range. Without this eon.pl rejects the login
    # with "Błąd działania reCaptcha".
    stealth(
        driver,
        languages=["pl-PL", "pl"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )

    # Belt-and-braces — explicitly remove navigator.webdriver via CDP. Some
    # versions of selenium-stealth don't cover this on the very first
    # document load.
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            },
        )
    except Exception:
        pass

    return driver


def _login_sync(email: str, password: str, timeout_s: int) -> str:
    """Blocking login flow. Caller wraps in asyncio.to_thread()."""
    if not email or not password:
        raise LoginError("Empty email or password — set them in addon options")

    display = os.environ.get("DISPLAY", "")
    if display:
        socket_path = f"/tmp/.X11-unix/X{display.lstrip(':')}"
        if not os.path.exists(socket_path):
            _LOGGER.warning("X socket %s missing — Xvfb not running on %s", socket_path, display)

    _LOGGER.debug("Killing any stale chromium/chromedriver processes before launch")
    _kill_stale_chromium()

    try:
        driver = _build_driver()
    except Exception as exc:
        raise LoginError(f"Failed to launch chromium: {exc}") from exc

    debug_dir = os.environ.get("EON_DATA_DIR", "/data")

    try:
        _LOGGER.info("Selenium: navigating to %s", ENDPOINT_LOGIN)
        driver.set_page_load_timeout(timeout_s)
        driver.get(ENDPOINT_LOGIN)

        # Brief pause — let reCAPTCHA v3 observe page load before we interact.
        time.sleep(random.uniform(2.5, 4.5))

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

        # Move cursor to field before clicking — looks more human.
        ActionChains(driver).move_to_element(email_el).pause(
            random.uniform(0.3, 0.7)
        ).click().perform()
        email_el.clear()
        for ch in email:
            email_el.send_keys(ch)
            time.sleep(random.uniform(0.05, 0.13))

        time.sleep(random.uniform(0.3, 0.7))

        pw_el = driver.find_element(
            By.CSS_SELECTOR,
            'input#Password, input[name="Password"]',
        )
        ActionChains(driver).move_to_element(pw_el).pause(
            random.uniform(0.2, 0.5)
        ).click().perform()
        pw_el.clear()
        for ch in password:
            pw_el.send_keys(ch)
            time.sleep(random.uniform(0.05, 0.13))

        time.sleep(random.uniform(0.8, 1.8))

        # Cookie consent banner (#clb) overlays the submit button. Try to
        # accept it first; if no button is found, hide the overlay so the
        # submit click goes through. This is purely a UX overlay — eon.pl
        # already issued the cookies needed for the form to work.
        _dismiss_cookie_banner(driver)

        # Submit button — type="button", click triggers JS submitForm() which
        # runs reCAPTCHA verification and POSTs to /mojeon/Logowanie.
        submit_el = driver.find_element(
            By.CSS_SELECTOR,
            'button[data-test-id="login-button"]',
        )
        ActionChains(driver).move_to_element(submit_el).pause(
            random.uniform(0.3, 0.6)
        ).perform()
        try:
            submit_el.click()
        except Exception:
            driver.execute_script("arguments[0].click();", submit_el)

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


def _dismiss_cookie_banner(driver: Any) -> None:
    """Best-effort: click an Accept button in the GDPR/cookie banner; if no
    button is found, hide the overlay container outright."""
    accept_selectors = (
        '#clb button[id*="accept" i]',
        '#clb button[class*="accept" i]',
        '#clb [data-test-id*="accept" i]',
        'button#cookie-accept',
        'button[aria-label*="zgadzam" i]',
        'button[aria-label*="accept" i]',
    )
    for sel in accept_selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed():
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    _LOGGER.debug("Cookie banner dismissed via %s", sel)
                    return
        except Exception:
            continue
    # Fallback — just hide the overlay so it can't intercept clicks.
    try:
        driver.execute_script(
            "var b = document.getElementById('clb');"
            "if (b) { b.style.display = 'none'; b.remove(); }"
        )
        _LOGGER.debug("Cookie banner #clb hidden via JS fallback")
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
