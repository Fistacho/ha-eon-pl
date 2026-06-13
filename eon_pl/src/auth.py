"""nodriver-based login flow for Mój E.ON.

Primary strategy (v1.2.0): hybrid login.
  1. Chrome opens EON login page, runs stealth JS, simulates mouse activity.
  2. Call grecaptcha.execute() inside the real Chrome context — the token is
     generated on the user's home-network IP, so Google scores it as a real
     browser (score 0.7-0.9 vs 0.1-0.3 from a cloud solver).
  3. Close Chrome.
  4. HTTP POST the login form with this high-score token via urllib (fast, reliable).

CapSolver (EON_CAPSOLVER_API_KEY) is kept as fallback only — ProxyLess tokens
from third-party servers consistently get scores below EON's threshold.
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

import sys
import nodriver

from .const import COOKIE_NAME, ENDPOINT_LOGIN, PAGE_DASHBOARD

_LOGGER = logging.getLogger(__name__)

# Platform-specific UA so navigator.platform and the UA string are consistent.
# reCaptcha v3 checks both — a mismatch (Linux UA on Windows platform) scores poorly.
if sys.platform == "win32":
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.6778.205 Safari/537.36"
    )
    _NAV_PLATFORM = "Win32"
else:
    # Alpine/Linux container: Chromium 131 on x86_64
    _USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.6778.205 Safari/537.36"
    )
    _NAV_PLATFORM = "Linux x86_64"

# Injected on every new document via Page.addScriptToEvaluateOnNewDocument so it
# runs before any page script (including reCAPTCHA).  Key goals:
#   1. Remove the automation/webdriver flag that Chromium normally sets.
#   2. Spoof WebGL renderer — SwiftShader ("ANGLE ... SwiftShader") is an
#      instantly-recognisable bot fingerprint; fake an Intel integrated GPU.
#   3. Restore navigator.languages to a plausible Polish-language profile.
#   4. Intercept window.grecaptcha assignment so execute() is patched before any
#      page script calls it — when reCAPTCHA api.js does window.grecaptcha={…}
#      our setter fires and wraps execute() to return window.__eon_token.
_STEALTH_JS = f"""
(function () {{
    try {{ Object.defineProperty(navigator, 'webdriver', {{get: () => undefined, configurable: true}}); }} catch (e) {{}}

    // Remove CDP injection artifacts (cdc_* properties) — reCaptcha detects these
    // to identify Chrome DevTools Protocol sessions.
    try {{
        var _cdcKeys = Object.getOwnPropertyNames(window).filter(function(k) {{
            return k.startsWith('cdc_');
        }});
        _cdcKeys.forEach(function(k) {{
            try {{ Object.defineProperty(window, k, {{get: () => undefined, configurable: true}}); }} catch(e) {{}}
        }});
    }} catch(e) {{}}

    // permissions.query — override so 'notifications' query returns 'default' (not 'denied' as in headless)
    try {{
        var _origPQ = window.navigator.permissions.query.bind(window.navigator.permissions);
        window.navigator.permissions.query = function(params) {{
            if (params && params.name === 'notifications') {{
                return Promise.resolve({{state: 'default', onchange: null}});
            }}
            return _origPQ(params);
        }};
    }} catch(e) {{}}

    // Match navigator.platform to our user-agent so reCaptcha sees a consistent profile.
    try {{ Object.defineProperty(navigator, 'platform', {{get: () => {json.dumps(_NAV_PLATFORM)}}}); }} catch (e) {{}}

    var _spoof = function (proto) {{
        try {{
            var _orig = proto.getParameter.bind(proto);
            proto.getParameter = function (p) {{
                if (p === 37445) return 'Google Inc. (Intel)';
                if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)';
                return _orig(p);
            }};
        }} catch (e) {{}}
    }};
    if (typeof WebGLRenderingContext !== 'undefined') _spoof(WebGLRenderingContext.prototype);
    try {{ if (typeof WebGL2RenderingContext !== 'undefined') _spoof(WebGL2RenderingContext.prototype); }} catch (e) {{}}

    try {{ Object.defineProperty(navigator, 'languages', {{get: () => ['pl-PL', 'pl', 'en-US', 'en']}}); }} catch (e) {{}}

    // window.chrome expected by reCAPTCHA — absent in Chromium without extension
    try {{
        if (!window.chrome) {{
            window.chrome = {{ app: {{ isInstalled: false, InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }}, RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }} }}, csi: function(){{}}, loadTimes: function(){{}}, runtime: {{}} }};
        }}
    }} catch (e) {{}}

    // Fake minimal plugin list — empty plugins[] is a known headless signal
    try {{
        var _fakePlugins = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer'].map(function(name) {{
            return {{ name: name, filename: 'internal-pdf-viewer', description: 'Portable Document Format' }};
        }});
        Object.defineProperty(navigator, 'plugins', {{ get: function() {{ return _fakePlugins; }} }});
        Object.defineProperty(navigator, 'mimeTypes', {{ get: function() {{ return []; }} }});
    }} catch (e) {{}}

    // Intercept grecaptcha property assignment — runs before reCAPTCHA api.js
    // sets window.grecaptcha, so we patch execute() on the object before any
    // page code (event listeners, ready() callbacks) captures a reference to it.
    var _gcVal = undefined;
    function _patchGC(obj) {{
        if (!obj || typeof obj.execute !== 'function') return obj;
        var _oe = obj.execute;
        obj.execute = function() {{
            if (window.__eon_token) {{
                var t = window.__eon_token;
                window.__eon_token = null;
                return Promise.resolve(t);
            }}
            return _oe.apply(obj, arguments);
        }};
        return obj;
    }}
    try {{
        Object.defineProperty(window, 'grecaptcha', {{
            configurable: true,
            enumerable: true,
            get: function() {{ return _gcVal; }},
            set: function(v) {{ _gcVal = _patchGC(v); }}
        }});
    }} catch(e) {{}}
}})();
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


async def _mouse_move(tab: Any, to_x: int, to_y: int, from_x: int = -1, from_y: int = -1, *, steps: int = 10) -> None:
    """Simulate gradual mouse movement to (to_x, to_y) via CDP Input events."""
    try:
        from nodriver.cdp import input_ as cdp_input  # noqa: PLC0415
        if from_x < 0:
            from_x = max(0, to_x - random.randint(80, 280))
        if from_y < 0:
            from_y = max(0, to_y + random.randint(-140, 140))
        for i in range(1, steps + 1):
            t = i / steps
            x = int(from_x + (to_x - from_x) * t + random.randint(-6, 6))
            y = int(from_y + (to_y - from_y) * t + random.randint(-4, 4))
            x = max(0, min(1365, x))
            y = max(0, min(767, y))
            await tab.send(cdp_input.dispatch_mouse_event(
                type_="mouseMoved", x=x, y=y, modifiers=0, buttons=0,
            ))
            await asyncio.sleep(random.uniform(0.02, 0.06))
    except Exception as exc:
        _LOGGER.debug("_mouse_move failed: %s", exc)


async def _simulate_page_interaction(tab: Any) -> None:
    """Simulate ~4s of natural mouse activity — critical for reCaptcha v3 scoring."""
    try:
        from nodriver.cdp import input_ as cdp_input  # noqa: PLC0415
        x, y = random.randint(400, 900), random.randint(200, 450)
        for _ in range(random.randint(12, 20)):
            nx = x + random.randint(-180, 180)
            ny = y + random.randint(-120, 120)
            nx = max(60, min(1305, nx))
            ny = max(60, min(707, ny))
            await _mouse_move(tab, nx, ny, x, y, steps=random.randint(5, 9))
            x, y = nx, ny
            await asyncio.sleep(random.uniform(0.07, 0.30))
        # Small scroll — simulates user reading the page
        await tab.send(cdp_input.dispatch_mouse_event(
            type_="mouseWheel", x=683, y=400,
            delta_x=0.0, delta_y=float(random.randint(40, 90)), modifiers=0,
        ))
        await asyncio.sleep(random.uniform(0.4, 0.9))
        await tab.send(cdp_input.dispatch_mouse_event(
            type_="mouseWheel", x=683, y=400,
            delta_x=0.0, delta_y=float(-random.randint(20, 50)), modifiers=0,
        ))
    except Exception as exc:
        _LOGGER.debug("_simulate_page_interaction failed: %s", exc)


async def _get_element_center(tab: Any, selector: str) -> tuple[int, int] | None:
    """Return viewport center (x, y) of the first matching DOM element."""
    try:
        raw = await tab.evaluate(
            "(function(){var el=document.querySelector("
            + json.dumps(selector)
            + ");if(!el)return null;"
            "var r=el.getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});})()"
        )
        if raw and isinstance(raw, str):
            d = json.loads(raw)
            return int(d["x"]), int(d["y"])
    except Exception as exc:
        _LOGGER.debug("_get_element_center(%s): %s", selector, exc)
    return None


async def _cdp_click(tab: Any, x: int, y: int) -> bool:
    """Send trusted CDP mousedown + mouseup at (x, y). Returns True on success.

    CDP Input.dispatchMouseEvent creates isTrusted=true events — unlike
    element.click() which calls el.click() via Runtime and creates isTrusted=false.
    reCaptcha v3 uses isTrusted to detect synthetic clicks.
    Note: button must be MouseButton enum (not a plain string) — nodriver calls .to_json() on it.
    """
    try:
        from nodriver.cdp import input_ as cdp_input  # noqa: PLC0415
        for etype in ("mousePressed", "mouseReleased"):
            await tab.send(cdp_input.dispatch_mouse_event(
                type_=etype, x=float(x), y=float(y),
                button=cdp_input.MouseButton.LEFT, click_count=1, modifiers=0,
            ))
            await asyncio.sleep(random.uniform(0.04, 0.10))
        _LOGGER.debug("_cdp_click(%d,%d) OK", x, y)
        return True
    except Exception as exc:
        _LOGGER.warning("_cdp_click(%d,%d) failed: %s — will fall back", x, y, exc)
        return False


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
            "type": "ReCaptchaV3EnterpriseTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": action,
            "userAgent": _USER_AGENT,
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
            sol = result["solution"]
            token = sol["gRecaptchaResponse"]
            score = sol.get("score", "n/a")
            _LOGGER.info("Capsolver: token received (len=%d, score=%s)", len(token), score)
            return token

    raise LoginError("Capsolver: timeout after 90s")


def _http_login(email: str, password: str, capsolver_key: str) -> str:
    """Login via direct HTTP POST — no browser required. Blocking; call via asyncio.to_thread.

    Flow:
      1. GET /mojeon/Logowanie  → ASP.NET session cookie + CSRF token
      2. CapSolver              → reCaptcha v3 token
      3. POST /mojeon/Logowanie → .AspNet.Cookies returned on success

    Uses urllib.request (stdlib) with a persistent cookie jar to preserve the
    ASP.NET anti-forgery cookie between GET and POST.
    """
    import http.cookiejar  # noqa: PLC0415
    import urllib.parse   # noqa: PLC0415

    if not capsolver_key:
        raise LoginError("API login requires a CapSolver key")

    base_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # Step 1 — GET login page
    _LOGGER.info("HTTP login: fetching login page for CSRF token...")
    req = urllib.request.Request(ENDPOINT_LOGIN, headers=base_headers)
    with opener.open(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    m = re.search(r'name="__RequestVerificationToken"[^>]+value="([^"]+)"', html)
    if not m:
        raise LoginError("CSRF token not found on login page")
    csrf = m.group(1)
    _LOGGER.debug("HTTP login: CSRF token length=%d", len(csrf))

    # Step 2 — reCaptcha token via CapSolver
    site_key_m = re.search(r"grecaptcha\.execute\([\"']([^\"']+)[\"']", html)
    site_key = site_key_m.group(1) if site_key_m else "6Ldn14wqAAAAAJf4ZGIjbV_QX6-1ao8wMwgNeyVY"
    _LOGGER.info("HTTP login: requesting CapSolver token (site_key=%s...)", site_key[:8])
    token = _capsolver_get_token(capsolver_key, ENDPOINT_LOGIN, site_key, "login")

    # Step 3 — POST login form
    post_data = urllib.parse.urlencode({
        "UserName": email,
        "Password": password,
        "Token": token,
        "Step": "1",
        "__RequestVerificationToken": csrf,
    }).encode("utf-8")
    post_headers = {
        **base_headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": ENDPOINT_LOGIN,
        "Origin": "https://eon.pl",
    }
    _LOGGER.info("HTTP login: POSTing login form...")
    req = urllib.request.Request(ENDPOINT_LOGIN, data=post_data, headers=post_headers)
    with opener.open(req, timeout=30) as resp:
        resp_url = resp.geturl()
        resp_html = resp.read().decode("utf-8", errors="replace")
    _LOGGER.info("HTTP login: POST response url=%s", resp_url)
    _LOGGER.debug(
        "HTTP login: cookies in jar: %s",
        [(c.name, c.value[:8] + "...") for c in jar if c.value],
    )

    # Extract cookie from jar
    for c in jar:
        if c.name == COOKIE_NAME:
            if c.value:
                _LOGGER.info("HTTP login: success — %s cookie obtained", COOKIE_NAME)
                return c.value

    # Diagnose failure — log form validation text
    vm_m = re.search(r'class="[^"]*validation-msg[^"]*"[^>]*>([^<]+)<', resp_html)
    err_text = vm_m.group(1).strip() if vm_m else ""
    if err_text:
        raise LoginError(f"HTTP login: server rejected — {err_text}")
    if "recaptcha-error" in resp_html:
        raise LoginError("HTTP login: reCaptcha rejected (score too low)")
    raise LoginError(
        f"HTTP login: {COOKIE_NAME} not found after POST (url={resp_url})"
    )


async def _api_login_async(email: str, password: str, capsolver_key: str) -> str:
    """Async wrapper for _http_login."""
    return await asyncio.to_thread(_http_login, email, password, capsolver_key)


async def _hybrid_login_async(email: str, password: str, timeout_s: int) -> str:
    """Hybrid login: Chrome generates a high-score reCaptcha token, HTTP POST submits it.

    This solves the isTrusted=false problem of CDP-injected clicks:
      - Chrome runs on the home network IP → real browser fingerprint → high score
      - We extract the token from Chrome via tab.evaluate() (no click needed)
      - HTTP POST handles the form submission (no CDP click, no isTrusted issue)
    """
    import http.cookiejar  # noqa: PLC0415
    import urllib.parse    # noqa: PLC0415

    chromium_path = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium-browser")
    debug_dir = os.environ.get("EON_DATA_DIR", "/data")

    _LOGGER.info("Hybrid login: launching Chrome to generate reCaptcha token...")
    await asyncio.to_thread(_kill_stale_chromium)

    cfg = nodriver.Config()
    cfg.browser_executable_path = chromium_path
    cfg.headless = False
    _chrome_profile = os.path.join(debug_dir, "chrome_profile")
    os.makedirs(_chrome_profile, exist_ok=True)
    cfg.user_data_dir = _chrome_profile
    _base_args = [
        "--disable-dev-shm-usage",
        "--window-size=1366,768",
        "--lang=pl-PL",
        "--password-store=basic",
        f"--user-agent={_USER_AGENT}",
    ]
    import sys as _sys  # noqa: PLC0415
    if _sys.platform != "win32":
        _base_args += ["--ozone-platform=x11", "--use-gl=angle", "--use-angle=swiftshader"]
    for arg in _base_args:
        try:
            cfg.add_argument(arg)
        except ValueError:
            pass

    browser = await nodriver.start(config=cfg)
    tab = await browser.get("about:blank")
    try:
        from nodriver.cdp import page as cdp_page  # noqa: PLC0415
        await tab.send(cdp_page.add_script_to_evaluate_on_new_document(_STEALTH_JS))
        _LOGGER.info("Hybrid login: navigating to EON login page...")
        await tab.get(ENDPOINT_LOGIN)

        # Simulate brief human activity (score boost)
        _LOGGER.info("Hybrid login: simulating user activity for score boost...")
        await _simulate_page_interaction(tab)

        # Wait for grecaptcha.execute to be ready
        _gc_ready = False
        for _ in range(30):
            try:
                _gc_ready = bool(await tab.evaluate(
                    "typeof window.grecaptcha==='object'"
                    "&&typeof window.grecaptcha.execute==='function'"
                ))
            except Exception:
                pass
            if _gc_ready:
                break
            await asyncio.sleep(0.5)
        if not _gc_ready:
            raise LoginError("Hybrid login: grecaptcha.execute not available within 15s")

        # Call grecaptcha.execute() from within the real Chrome context.
        # Our interceptor passes through to the original execute() when __eon_token is null.
        site_key_raw = await tab.evaluate(_JS_EXTRACT_SITE_KEY)
        try:
            site_info = json.loads(site_key_raw) if isinstance(site_key_raw, str) else {}
        except Exception:
            site_info = {}
        site_key = site_info.get("siteKey") or "6Ldn14wqAAAAAJf4ZGIjbV_QX6-1ao8wMwgNeyVY"
        action = site_info.get("action") or "login"

        # Kick off grecaptcha.execute() via polling — avoids nodriver awaitPromise issues.
        # Store result in window.__eon_result (token) or window.__eon_error (string).
        _LOGGER.info("Hybrid login: calling grecaptcha.execute() in Chrome (site_key=%s...)...", site_key[:8])
        await tab.evaluate(
            "window.__eon_token = null; window.__eon_result = null; window.__eon_error = null;"
            + (
                f"grecaptcha.ready(function() {{"
                f"  grecaptcha.execute({json.dumps(site_key)}, {{action: {json.dumps(action)}}})"
                f"    .then(function(t) {{ window.__eon_result = t || 'null'; }})"
                f"    .catch(function(e) {{ window.__eon_error = 'ERR:' + String(e); }});"
                f"}})"
            )
        )
        recaptcha_token = None
        for _i in range(30):  # up to 15 s
            await asyncio.sleep(0.5)
            try:
                val = await tab.evaluate("window.__eon_result || window.__eon_error || null")
            except Exception:
                val = None
            if val and isinstance(val, str):
                if val.startswith("ERR:"):
                    raise LoginError(f"Hybrid login: grecaptcha.execute failed — {val}")
                if val != "null" and len(val) >= 100:
                    recaptcha_token = val
                    break
                _LOGGER.debug("Hybrid login: execute returned '%s' — retrying...", val[:50])
        if not recaptcha_token:
            raise LoginError("Hybrid login: reCaptcha token not received within 15s")
        _LOGGER.info("Hybrid login: token obtained from Chrome (len=%d)", len(recaptcha_token))

        # Extract ASP.NET anti-forgery cookie from Chrome
        from nodriver.cdp import network as cdp_net  # noqa: PLC0415
        chrome_cookies = await tab.send(cdp_net.get_cookies())
        csrf_cookie = None
        for c in chrome_cookies:
            if c.name and c.name.startswith(".AspNet.Antiforgery"):
                csrf_cookie = (c.name, c.value)
                break

    finally:
        try:
            browser.stop()
        except Exception:
            pass

    # Step 2: HTTP POST with the real Chrome token
    _LOGGER.info("Hybrid login: POSTing form with Chrome-generated token...")

    # GET login page to get CSRF form field
    base_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(ENDPOINT_LOGIN, headers=base_headers)
    with opener.open(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    m = re.search(r'name="__RequestVerificationToken"[^>]+value="([^"]+)"', html)
    if not m:
        raise LoginError("Hybrid login: CSRF token not found on login page")
    csrf = m.group(1)

    # Override the antiforgery cookie from Chrome if available — ensures the cookie and form field match
    if csrf_cookie:
        _LOGGER.debug("Hybrid login: injecting Chrome antiforgery cookie %s", csrf_cookie[0])
        import http.cookiejar as hcj  # noqa: PLC0415
        for c in list(jar):
            if c.name.startswith(".AspNet.Antiforgery"):
                c.value = csrf_cookie[1]

    post_data = urllib.parse.urlencode({
        "UserName": email,
        "Password": password,
        "Token": recaptcha_token,
        "Step": "1",
        "__RequestVerificationToken": csrf,
    }).encode("utf-8")
    post_headers = {
        **base_headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": ENDPOINT_LOGIN,
        "Origin": "https://eon.pl",
    }
    req = urllib.request.Request(ENDPOINT_LOGIN, data=post_data, headers=post_headers)
    with opener.open(req, timeout=30) as resp:
        resp_url = resp.geturl()
        resp_html = resp.read().decode("utf-8", errors="replace")
    _LOGGER.info("Hybrid login: POST response url=%s", resp_url)

    for c in jar:
        if c.name == COOKIE_NAME and c.value:
            _LOGGER.info("Hybrid login: success — %s cookie obtained", COOKIE_NAME)
            return c.value

    vm_m = re.search(r'class="[^"]*validation-msg[^"]*"[^>]*>([^<]+)<', resp_html)
    err_text = vm_m.group(1).strip() if vm_m else ""
    if err_text:
        raise LoginError(f"Hybrid login: server rejected — {err_text}")
    raise LoginError(
        f"Hybrid login: {COOKIE_NAME} not found after POST (url={resp_url})"
    )


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
    # Persistent profile so _GRECAPTCHA cookies and reCaptcha reputation build up
    # between sessions.  This is the single most effective way to improve v3 score
    # over time without a proxy.  nodriver won't clean the dir when uses_custom_data_dir=True.
    _chrome_profile = os.path.join(debug_dir, "chrome_profile")
    os.makedirs(_chrome_profile, exist_ok=True)
    cfg.user_data_dir = _chrome_profile
    # nodriver manages several flags internally and raises ValueError if we try
    # to add them via add_argument — silently skip those.
    _base_args = [
        "--disable-dev-shm-usage",
        "--window-size=1366,768",
        "--lang=pl-PL",
        "--password-store=basic",
        f"--user-agent={_USER_AGENT}",
    ]
    # Linux/container: Xvfb requires ozone x11 and software rendering via SwiftShader.
    # Windows: skip these — real Chrome uses the native GPU, giving a much better
    # reCaptcha score and faster CDP response times.
    import sys as _sys  # noqa: PLC0415
    if _sys.platform != "win32":
        _base_args += [
            "--ozone-platform=x11",
            "--use-gl=angle",
            "--use-angle=swiftshader",
        ]
    for arg in _base_args:
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

        # Human interaction — reCaptcha v3 scores sessions based on mouse movement,
        # scroll, and dwell time BEFORE clicking submit. Without any mouse events
        # the session scores near 0 regardless of browser fingerprint.
        await asyncio.sleep(random.uniform(1.5, 2.5))
        _LOGGER.info("Simulating human mouse interaction (reCaptcha score boost)...")
        await _simulate_page_interaction(tab)
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Email field — move mouse there first, then click
        try:
            email_el = await tab.find('input#UserName', timeout=timeout_s)
        except Exception as exc:
            await _dump_debug(tab, debug_dir, "email_field_missing")
            raise LoginError(f"Email field not found within {timeout_s}s") from exc

        _coords = await _get_element_center(tab, 'input#UserName')
        if _coords:
            await _mouse_move(tab, *_coords)
            await asyncio.sleep(random.uniform(0.1, 0.25))
            if not await _cdp_click(tab, *_coords):
                await email_el.click()
        else:
            await email_el.click()
        await asyncio.sleep(random.uniform(0.3, 0.6))
        for ch in email:
            await email_el.send_keys(ch)
            await asyncio.sleep(random.uniform(0.05, 0.13))

        await asyncio.sleep(random.uniform(0.3, 0.7))

        # Password field — move mouse there first
        pw_el = await tab.find('input[name="Password"]', timeout=10)
        _coords = await _get_element_center(tab, 'input[name="Password"]')
        if _coords:
            await _mouse_move(tab, *_coords)
            await asyncio.sleep(random.uniform(0.1, 0.25))
            if not await _cdp_click(tab, *_coords):
                await pw_el.click()
        else:
            await pw_el.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))
        for ch in password:
            await pw_el.send_keys(ch)
            await asyncio.sleep(random.uniform(0.05, 0.13))

        # Extra dwell: reCaptcha v3 builds the session score over time.
        # Staying on the page ~15 more seconds after typing gives the scoring
        # model more data — equivalent to a user pausing to review before clicking.
        await asyncio.sleep(random.uniform(0.5, 1.0))
        _LOGGER.info("Dwell pause — letting reCaptcha session score accumulate...")
        await _simulate_page_interaction(tab)  # another ~4s of mouse activity

        # Cookie banner
        await _dismiss_cookie_banner(tab)

        # Wait for grecaptcha.execute to become available.
        # YETT.js blocks /google/ scripts until page "load" fires; after that
        # reCaptcha api.js loads async, so we must wait before clicking.
        _LOGGER.info("Waiting for grecaptcha.execute...")
        _gc_ready = False
        for _i in range(20):  # up to 10 s
            try:
                _gc_ready = bool(await tab.evaluate(
                    "typeof window.grecaptcha==='object'"
                    "&&typeof window.grecaptcha.execute==='function'"
                ))
            except Exception:
                pass
            if _gc_ready:
                break
            await asyncio.sleep(0.5)
        _LOGGER.info("grecaptcha.execute %s", "ready" if _gc_ready else "NOT ready — proceeding anyway")

        # Capsolver: obtain a pre-solved token and stage it via window.__eon_token.
        # EON's flow: button click → submitForm() → recaptchaLoad() →
        #   grecaptcha.ready() → grecaptcha.execute() [our patch returns __eon_token]
        #   → .then(t => submitRecaptcha(t)) → form submits naturally.
        # NOTE: do NOT fall back to env var here — the caller controls whether CapSolver
        # is used (empty string means "native only"; env var is resolved in login_with_retry).
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
                await tab.evaluate(f"window.__eon_token = {json.dumps(token)};")
                _LOGGER.info("Capsolver: token staged as window.__eon_token")
            else:
                _LOGGER.warning("Capsolver: could not extract reCAPTCHA site key — using native")

        # Move mouse to button, then click via trusted CDP Input (not el.click())
        submit_el = await tab.find('button[data-test-id="login-button"]', timeout=10)
        _btn_coords = await _get_element_center(tab, 'button[data-test-id="login-button"]')
        if _btn_coords:
            await _mouse_move(tab, *_btn_coords)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            _LOGGER.info("Clicking login button (CDP trusted click)...")
            if not await _cdp_click(tab, *_btn_coords):
                _LOGGER.info("CDP click failed — fallback el.click()...")
                await submit_el.click()
        else:
            _LOGGER.info("Clicking login button (fallback el.click)...")
            await submit_el.click()

        # Diagnostics: 2s after click — was __eon_token consumed? what does the form say?
        await asyncio.sleep(2.0)
        try:
            _tok_state = await tab.evaluate(
                "JSON.stringify({val: window.__eon_token === null ? 'null' : "
                "(window.__eon_token === undefined ? 'undefined' : 'SET'), "
                "type: typeof window.__eon_token})"
            )
            _LOGGER.info("__eon_token state 2s after click: %s", _tok_state)
            _vm_text = await tab.evaluate(
                "document.querySelector('.validation-msg')?.textContent?.trim() || '(empty)'"
            )
            _LOGGER.info("validation-msg 2s after click: %s", _vm_text)
            _rc_err = await tab.evaluate(
                "document.querySelector('#recaptcha-error-banner')?.textContent?.trim() || '(none)'"
            )
            _LOGGER.info("recaptcha-error-banner 2s after click: %s", _rc_err)
            _gc_type = await tab.evaluate(
                "typeof window.grecaptcha + ' / execute=' + typeof (window.grecaptcha && window.grecaptcha.execute)"
            )
            _LOGGER.info("grecaptcha type 2s after click: %s", _gc_type)
        except Exception as _diag_exc:
            _LOGGER.debug("Diagnostics failed: %s", _diag_exc)

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
    """Run login with retry.

    Strategy (in order):
      1. Hybrid: Chrome generates real reCaptcha token on home IP → HTTP POST (fastest, best score)
      2. HTTP + CapSolver: pure HTTP, no browser (requires CapSolver key, lower score)
      3. Browser + CapSolver: full browser automation with CapSolver token injection
      4. Browser native: full browser automation with native reCaptcha
    """
    capsolver_key = capsolver_key or os.environ.get("EON_CAPSOLVER_API_KEY", "")
    timeout_s = 90
    last_exc: Exception | None = None
    attempt_num = 0

    # Attempt 1: Hybrid (Chrome token + HTTP POST) — best score, no click needed
    attempt_num += 1
    _LOGGER.info("Login attempt %d: hybrid (Chrome token + HTTP POST)", attempt_num)
    try:
        return await _hybrid_login_async(email, password, timeout_s)
    except LoginError as exc:
        last_exc = exc
        _LOGGER.warning("Hybrid login failed: %s", exc)
        await asyncio.sleep(3)

    if capsolver_key:
        # Attempt 2: HTTP + CapSolver (no browser, faster but lower score)
        attempt_num += 1
        _LOGGER.info("Login attempt %d: HTTP + CapSolver (no browser)", attempt_num)
        try:
            return await _api_login_async(email, password, capsolver_key)
        except LoginError as exc:
            last_exc = exc
            _LOGGER.warning("HTTP+CapSolver login failed: %s", exc)
            await asyncio.sleep(3)

    # Remaining attempts: full browser
    browser_attempts = max(1, attempts - attempt_num)
    for i in range(1, browser_attempts + 1):
        attempt_num += 1
        key = capsolver_key if i > 1 else ""
        mode = "browser+CapSolver" if key else "browser native reCaptcha"
        _LOGGER.info("Login attempt %d: %s", attempt_num, mode)
        try:
            return await selenium_login(email, password, capsolver_key=key)
        except LoginError as exc:
            last_exc = exc
            _LOGGER.warning("Login attempt %d failed: %s", attempt_num, exc)
            if i < browser_attempts:
                await asyncio.sleep(5)

    assert last_exc is not None
    raise last_exc
