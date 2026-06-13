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

# Realistic desktop Chrome UA — Alpine's Chromium runs on aarch64 but reporting
# x86_64 is less suspicious. Chrome/131 = December 2024 stable release.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.6778.205 Safari/537.36"
)

# Injected on every new document via Page.addScriptToEvaluateOnNewDocument so it
# runs before any page script (including reCAPTCHA).  Key goals:
#   1. Remove the automation/webdriver flag that Chromium normally sets.
#   2. Spoof WebGL renderer — SwiftShader ("ANGLE ... SwiftShader") is an
#      instantly-recognisable bot fingerprint; fake an Intel integrated GPU.
#   3. Restore navigator.languages to a plausible Polish-language profile.
#   4. Intercept window.grecaptcha assignment so execute() is patched before any
#      page script calls it — when reCAPTCHA api.js does window.grecaptcha={…}
#      our setter fires and wraps execute() to return window.__eon_token.
_STEALTH_JS = """
(function () {
    try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true}); } catch (e) {}

    var _spoof = function (proto) {
        try {
            var _orig = proto.getParameter.bind(proto);
            proto.getParameter = function (p) {
                if (p === 37445) return 'Google Inc. (Intel)';
                if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)';
                return _orig(p);
            };
        } catch (e) {}
    };
    if (typeof WebGLRenderingContext !== 'undefined') _spoof(WebGLRenderingContext.prototype);
    try { if (typeof WebGL2RenderingContext !== 'undefined') _spoof(WebGL2RenderingContext.prototype); } catch (e) {}

    try { Object.defineProperty(navigator, 'languages', {get: () => ['pl-PL', 'pl', 'en-US', 'en']}); } catch (e) {}

    // Intercept grecaptcha property assignment — runs before reCAPTCHA api.js
    // sets window.grecaptcha, so we patch execute() on the object before any
    // page code (event listeners, ready() callbacks) captures a reference to it.
    var _gcVal = undefined;
    function _patchGC(obj) {
        if (!obj || typeof obj.execute !== 'function') return obj;
        var _oe = obj.execute;
        obj.execute = function() {
            if (window.__eon_token) {
                var t = window.__eon_token;
                window.__eon_token = null;
                return Promise.resolve(t);
            }
            return _oe.apply(obj, arguments);
        };
        return obj;
    }
    try {
        Object.defineProperty(window, 'grecaptcha', {
            configurable: true,
            enumerable: true,
            get: function() { return _gcVal; },
            set: function(v) { _gcVal = _patchGC(v); }
        });
    } catch(e) {}
})();
"""

# Returns JSON string "{\"siteKey\":\"...\",\"action\":\"...\"}" so tab.evaluate()
# gives back a plain Python string that json.loads() can parse.
_JS_EXTRACT_SITE_KEY = """
(function() {
    var siteKey = null, action = null;

    var scripts = document.querySelectorAll('script[src*="recaptcha"]');
    for (var s of scripts) {
        var m = s.src.match(/[?&]render=([^&\\s]+)/);
        if (m && m[1] !== 'explicit') { siteKey = m[1]; break; }
    }
    var el = document.querySelector('[data-sitekey]');
    if (el && !siteKey) siteKey = el.getAttribute('data-sitekey');

    var inline = document.querySelectorAll('script:not([src])');
    for (var s of inline) {
        var text = s.textContent || '';
        var m = text.match(/grecaptcha\\.execute\\s*\\([^,]+,\\s*\\{[^}]*action\\s*:\\s*['\"](\\w+)['\"]/);
        if (m) { action = m[1]; break; }
    }

    return JSON.stringify({siteKey: siteKey, action: action});
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


def _capsolver_get_token(api_key: str, page_url: str, site_key: str, action: str = "login") -> str:
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

    _LOGGER.info("Capsolver: creating task for %s (action=%s)", page_url, action)
    result = _post("https://api.capsolver.com/createTask", {
        "clientKey": api_key,
        "task": {
            "type": "ReCaptchaV3TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": action,
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


async def _login_async(email: str, password: str, timeout_s: int, capsolver_key: str = "") -> str:
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
        f"--user-agent={_USER_AGENT}",
    ]:
        try:
            cfg.add_argument(arg)
        except ValueError:
            pass

    # nodriver by default disables site isolation (--disable-features=IsolateOrigins,
    # site-per-process).  reCAPTCHA cross-origin iframes may need site isolation to
    # execute correctly — remove that flag if accessible.
    try:
        _args = list(getattr(cfg, "browser_args", []))
        _filtered = [a for a in _args if "IsolateOrigins" not in a and "site-per-process" not in a]
        if len(_filtered) < len(_args):
            cfg.browser_args = _filtered
            _LOGGER.debug("Removed site-isolation-disable flag from browser args")
    except Exception:
        pass

    try:
        browser = await nodriver.start(cfg)
    except Exception as exc:
        raise LoginError(f"Failed to launch chromium: {exc}") from exc

    try:
        # Inject stealth JS BEFORE the login page loads.
        # Strategy: navigate to about:blank first to obtain a tab reference, register
        # addScriptToEvaluateOnNewDocument (runs on EVERY new document incl. iframes),
        # then navigate to the login page — script executes before reCAPTCHA scripts.
        try:
            from nodriver.cdp import page as cdp_page  # noqa: PLC0415
            _setup_tab = await browser.get("about:blank")
            await _setup_tab.send(
                cdp_page.add_script_to_evaluate_on_new_document(source=_STEALTH_JS)
            )
            _LOGGER.info("Stealth JS registered — will run before reCAPTCHA (WebGL spoof + webdriver hide)")
        except Exception as exc:
            _LOGGER.warning("Stealth JS registration failed: %s", exc)

        _LOGGER.info("nodriver: navigating to %s", ENDPOINT_LOGIN)
        tab = await browser.get(ENDPOINT_LOGIN)

        # Belt-and-suspenders: also evaluate directly in main frame context
        # in case addScriptToEvaluateOnNewDocument didn't persist across navigation.
        try:
            await tab.evaluate(_STEALTH_JS)
        except Exception:
            pass

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

        # Capsolver — obtain and inject pre-solved token, then trigger form submission
        # directly via EON's own submitRecaptcha() function instead of relying on the
        # browser to call grecaptcha.execute().
        #
        # EON's flow: submitForm() → recaptchaLoad() → grecaptcha.execute() →
        #             submitRecaptcha(token) → sets input[name="Token"] → dispatches
        #             'recaptchaSubmit' event → listener submits form.
        # We short-circuit directly to submitRecaptcha(token), bypassing the whole
        # grecaptcha chain which is where detection/interception was failing.
        capsolver_key = capsolver_key or os.environ.get("EON_CAPSOLVER_API_KEY", "")
        submitted_via_capsolver = False
        if capsolver_key:
            site_info_raw = await tab.evaluate(_JS_EXTRACT_SITE_KEY)
            try:
                site_info = json.loads(site_info_raw) if isinstance(site_info_raw, str) else {}
            except Exception:
                site_info = {}
            site_key = site_info.get("siteKey")
            action = site_info.get("action") or "login"
            if site_key:
                _LOGGER.info(
                    "Capsolver: solving reCAPTCHA v3 (site_key=%s..., action=%s)",
                    site_key[:8], action,
                )
                token = await asyncio.to_thread(
                    _capsolver_get_token, capsolver_key, ENDPOINT_LOGIN, site_key, action
                )
                submit_js = f"""
(function() {{
    var token = {json.dumps(token)};

    // Primary: call EON's own submitRecaptcha() — this is what normally gets
    // called after grecaptcha.execute() resolves.  Sets input[name="Token"]
    // and dispatches 'recaptchaSubmit' which triggers the actual form submit.
    if (typeof submitRecaptcha === 'function') {{
        submitRecaptcha(token);
        return;
    }}

    // Fallback A: set Token field + dispatch recaptchaSubmit manually
    var tf = document.querySelector('input[name="Token"]');
    if (tf) {{ tf.value = token; tf.dispatchEvent(new Event('change', {{bubbles: true}})); }}
    var form = document.querySelector('form#login-form');
    if (form) {{ form.dispatchEvent(new Event('recaptchaSubmit')); return; }}

    // Fallback B: window.__eon_token for our grecaptcha.execute intercept
    window.__eon_token = token;
    if (window.grecaptcha && typeof window.grecaptcha.execute === 'function') {{
        var _oe = window.grecaptcha.execute;
        window.grecaptcha.execute = function() {{
            var t = window.__eon_token; window.__eon_token = null;
            if (t) return Promise.resolve(t);
            return _oe.apply(this, arguments);
        }};
    }}
}})();
"""
                await tab.evaluate(submit_js)
                submitted_via_capsolver = True
                _LOGGER.info("Capsolver: token submitted via submitRecaptcha()")
            else:
                _LOGGER.warning("Capsolver: could not extract reCAPTCHA site key from page")

        if not submitted_via_capsolver:
            submit_el = await tab.find('button[data-test-id="login-button"]', timeout=10)
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


async def selenium_login(email: str, password: str, *, timeout_s: int = 90, capsolver_key: str = "") -> str:
    """Login entry point (kept as selenium_login for API compatibility)."""
    return await _login_async(email, password, timeout_s, capsolver_key=capsolver_key)


async def login_with_retry(email: str, password: str, *, attempts: int = 2, capsolver_key: str = "") -> str:
    """Run login with simple retry on transient failures."""
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await selenium_login(email, password, capsolver_key=capsolver_key)
        except LoginError as exc:
            last_exc = exc
            _LOGGER.warning("Login attempt %d/%d failed: %s", i, attempts, exc)
            if i < attempts:
                await asyncio.sleep(5)
    assert last_exc is not None
    raise last_exc
