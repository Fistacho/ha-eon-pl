"""nodriver-based login flow for Mój E.ON.

nodriver uses CDP websockets directly — Chrome never enters WebDriver automation
mode, so navigator.webdriver is never set and reCAPTCHA v3 cannot detect the
standard Selenium automation signal.

If EON_CAPSOLVER_API_KEY is set, reCAPTCHA is solved via capsolver.com API
and the token is injected before form submit (~$0.09/month at 2 logins/day).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import subprocess
import time
import urllib.request
from typing import Any

import nodriver

from .const import COOKIE_NAME, ENDPOINT_LOGIN, PAGE_DASHBOARD

_LOGGER = logging.getLogger(__name__)

_JS_EXTRACT_SITE_KEY = """
(function() {
    var scripts = document.querySelectorAll('script[src*="recaptcha"]');
    for (var s of scripts) {
        var m = s.src.match(/[?&]render=([^&\\s]+)/);
        if (m && m[1] !== 'explicit') return m[1];
    }
    var el = document.querySelector('[data-sitekey]');
    if (el) return el.getAttribute('data-sitekey');
    return null;
})()
"""


class LoginError(Exception):
    """Login attempt failed."""


def _kill_stale_chromium() -> None:
    """Kill orphaned chromedriver/chromium processes."""
    for pattern in ("chromedriver", "chromium-browser", "chromium"):
        try:
            subprocess.run(
                ["pkill", "-9", "-f", pattern],
                capture_output=True, timeout=5, check=False,
            )
        except Exception:
            pass
    time.sleep(0.8)


def _capsolver_get_token(api_key: str, page_url: str, site_key: str) -> str:
    """Call capsolver.com API — blocking, run via asyncio.to_thread."""
    def _post(url: str, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    _LOGGER.info("Capsolver: creating task for %s", page_url)
    result = _post("https://api.capsolver.com/createTask", {
        "clientKey": api_key,
        "task": {
            "type": "ReCaptchaV3TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": "login",
            "minScore": 0.7,
        },
    })
    if result.get("errorId", 0) != 0:
        raise LoginError(f"Capsolver createTask error: {result.get('errorDescription')}")

    task_id = result["taskId"]
    _LOGGER.debug("Capsolver task ID: %s", task_id)

    for _ in range(30):
        time.sleep(3)
        result = _post("https://api.capsolver.com/getTaskResult", {
            "clientKey": api_key,
            "taskId": task_id,
        })
        if result.get("errorId", 0) != 0:
            raise LoginError(f"Capsolver error: {result.get('errorDescription')}")
        if result.get("status") == "ready":
            token = result["solution"]["gRecaptchaResponse"]
            _LOGGER.info("Capsolver: token received (len=%d)", len(token))
            return token

    raise LoginError("Capsolver: timeout after 90s")


async def _login_async(email: str, password: str, timeout_s: int) -> str:
    """Native async login using nodriver (CDP-based, no WebDriver protocol)."""
    if not email or not password:
        raise LoginError("Empty email or password — set them in addon options")

    chromium_path = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium-browser")
    debug_dir = os.environ.get("EON_DATA_DIR", "/data")

    display = os.environ.get("DISPLAY", "")
    if display:
        socket_path = f"/tmp/.X11-unix/X{display.lstrip(':')}"
        if not os.path.exists(socket_path):
            _LOGGER.warning("X socket %s missing — Xvfb not running on %s", socket_path, display)

    _LOGGER.debug("Killing stale chromium processes")
    await asyncio.to_thread(_kill_stale_chromium)

    cfg = nodriver.Config()
    cfg.browser_executable_path = chromium_path
    cfg.headless = False
    # nodriver manages several flags internally and raises ValueError if we try
    # to add them via add_argument — silently skip those.
    for arg in [
        "--disable-dev-shm-usage",
        "--window-size=1366,768",
        "--lang=pl-PL",
        "--password-store=basic",
        "--ozone-platform=x11",
        "--use-gl=angle",
        "--use-angle=swiftshader",
    ]:
        try:
            cfg.add_argument(arg)
        except ValueError:
            pass

    try:
        browser = await nodriver.start(cfg)
    except Exception as exc:
        raise LoginError(f"Failed to launch chromium: {exc}") from exc

    try:
        _LOGGER.info("nodriver: navigating to %s", ENDPOINT_LOGIN)
        tab = await browser.get(ENDPOINT_LOGIN)

        await asyncio.sleep(random.uniform(2.5, 4.5))

        # Email field
        try:
            email_el = await tab.find('input#UserName', timeout=timeout_s)
        except Exception as exc:
            await _dump_debug(tab, debug_dir, "email_field_missing")
            raise LoginError(f"Email field not found within {timeout_s}s") from exc

        await email_el.click()
        await asyncio.sleep(random.uniform(0.3, 0.6))
        for ch in email:
            await email_el.send_keys(ch)
            await asyncio.sleep(random.uniform(0.05, 0.13))

        await asyncio.sleep(random.uniform(0.3, 0.7))

        # Password field
        pw_el = await tab.find('input[name="Password"]', timeout=10)
        await pw_el.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))
        for ch in password:
            await pw_el.send_keys(ch)
            await asyncio.sleep(random.uniform(0.05, 0.13))

        await asyncio.sleep(random.uniform(0.8, 1.8))

        # Cookie banner
        await _dismiss_cookie_banner(tab)

        # Submit button
        submit_el = await tab.find('button[data-test-id="login-button"]', timeout=10)

        # Capsolver — inject pre-solved token before clicking submit
        capsolver_key = os.environ.get("EON_CAPSOLVER_API_KEY", "")
        if capsolver_key:
            site_key = await tab.evaluate(_JS_EXTRACT_SITE_KEY)
            if site_key:
                _LOGGER.info("Capsolver: solving reCAPTCHA v3 (site_key=%s...)", site_key[:8])
                token = await asyncio.to_thread(
                    _capsolver_get_token, capsolver_key, ENDPOINT_LOGIN, site_key
                )
                await tab.evaluate(f"""
                    window.__eon_token = {json.dumps(token)};
                    (function() {{
                        var _o = window.grecaptcha || {{}};
                        window.grecaptcha = Object.assign({{}}, _o, {{
                            execute: function() {{
                                var t = window.__eon_token;
                                window.__eon_token = null;
                                if (t) return Promise.resolve(t);
                                return _o.execute ? _o.execute.apply(_o, arguments) : Promise.resolve('');
                            }},
                            ready: function(cb) {{ if (_o.ready) _o.ready(cb); else cb(); }}
                        }});
                    }})();
                """)
                _LOGGER.info("Capsolver: token injected")
            else:
                _LOGGER.warning("Capsolver: could not extract reCAPTCHA site key from page")

        await submit_el.click()

        # Wait for redirect away from login page
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            url = tab.url or ""
            if PAGE_DASHBOARD in url and "Logowanie" not in url:
                break
        else:
            await _dump_debug(tab, debug_dir, "no_redirect_after_submit")
            err_text = await _try_capture_error(tab)
            raise LoginError(
                f"Login did not redirect to dashboard within {timeout_s}s. "
                f"Page error: {err_text}"
            )

        # Extract cookie via CDP
        from nodriver.cdp import network as cdp_net  # noqa: PLC0415
        cookies = await tab.send(cdp_net.get_cookies())
        for c in cookies:
            if c.name == COOKIE_NAME and "eon.pl" in (c.domain or ""):
                if c.value:
                    _LOGGER.info("nodriver: login OK, captured %s", COOKIE_NAME)
                    return c.value

        raise LoginError(f"{COOKIE_NAME} not found in cookies after login")

    finally:
        try:
            browser.stop()
        except Exception:
            pass


async def _dismiss_cookie_banner(tab: Any) -> None:
    """Click accept button in GDPR banner, or hide it."""
    selectors = (
        '#clb button[id*="accept" i]',
        '#clb button[class*="accept" i]',
        'button#cookie-accept',
        'button[aria-label*="zgadzam" i]',
        'button[aria-label*="accept" i]',
    )
    for sel in selectors:
        try:
            el = await tab.find(sel, timeout=2)
            await el.click()
            _LOGGER.debug("Cookie banner dismissed via %s", sel)
            return
        except Exception:
            continue
    try:
        await tab.evaluate(
            "var b=document.getElementById('clb'); if(b){b.style.display='none';b.remove();}"
        )
        _LOGGER.debug("Cookie banner #clb hidden via JS")
    except Exception:
        pass


async def _dump_debug(tab: Any, data_dir: str, tag: str) -> None:
    try:
        png_path = os.path.join(data_dir, f"login_debug_{tag}.png")
        await tab.save_screenshot(png_path)
        html_path = os.path.join(data_dir, f"login_debug_{tag}.html")
        content = await tab.get_content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(content or "")
        _LOGGER.warning("Debug dumped: %s, %s", png_path, html_path)
    except Exception as exc:
        _LOGGER.debug("Debug dump failed: %s", exc)


async def _try_capture_error(tab: Any) -> str:
    try:
        for sel in (".validation-msg", ".validation-msg-recaptcha", ".form-validation-msg"):
            try:
                el = await tab.find(sel, timeout=2)
                txt = await tab.evaluate(
                    f"document.querySelector({json.dumps(sel)})?.textContent?.trim() || ''"
                )
                if txt:
                    return txt
            except Exception:
                continue
    except Exception:
        pass
    return f"current URL: {tab.url}"


async def selenium_login(email: str, password: str, *, timeout_s: int = 90) -> str:
    """Login entry point (kept as selenium_login for API compatibility)."""
    return await _login_async(email, password, timeout_s)


async def login_with_retry(email: str, password: str, *, attempts: int = 2) -> str:
    """Run login with simple retry on transient failures."""
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
