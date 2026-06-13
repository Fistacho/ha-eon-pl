"""
Local auth test — runs the EON login flow on Windows using local Chrome.

Usage:
    set EON_EMAIL=twoj@email.pl
    set EON_PASSWORD=haslo
    set EON_CAPSOLVER_API_KEY=opcjonalny_klucz   (lub zostaw puste aby testować bez)
    python test_local_auth.py

Requires: pip install nodriver
Chrome must be installed at default location or set CHROMIUM_BIN.
"""
import asyncio
import logging
import os
import sys

# Add src to path so we can import auth
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "eon_pl", "src"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOG = logging.getLogger("test_auth")


async def main():
    email = os.environ.get("EON_EMAIL", "")
    password = os.environ.get("EON_PASSWORD", "")
    capsolver = os.environ.get("EON_CAPSOLVER_API_KEY", "")

    if not email or not password:
        print("ERROR: Set EON_EMAIL and EON_PASSWORD environment variables first.")
        print("  set EON_EMAIL=twoj@email.pl")
        print("  set EON_PASSWORD=haslo")
        sys.exit(1)

    # Point to local Chrome on Windows
    if not os.environ.get("CHROMIUM_BIN"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Chromium\Application\chromium.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                os.environ["CHROMIUM_BIN"] = c
                _LOG.info("Using Chrome: %s", c)
                break
        else:
            _LOG.warning("Chrome not found at default locations — nodriver will try auto-detect")

    # Override data dir for debug screenshots
    os.environ["EON_DATA_DIR"] = os.path.dirname(__file__)

    if capsolver:
        _LOG.info("Capsolver key provided — will test with CapSolver")
    else:
        _LOG.info("No CapSolver key — testing native browser reCaptcha only")

    try:
        from auth import login_with_retry
        cookie = await login_with_retry(email, password, attempts=1, capsolver_key=capsolver)
        _LOG.info("SUCCESS! Cookie obtained (len=%d)", len(cookie))
        _LOG.info("Cookie starts: %s...", cookie[:40])
    except Exception as exc:
        _LOG.error("FAILED: %s", exc)
        _LOG.info("Check login_debug_*.png and login_debug_*.html in this directory")
        sys.exit(1)


asyncio.run(main())
