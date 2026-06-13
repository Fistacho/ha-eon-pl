"""
Local auth test — runs the EON login flow on Windows using local Chrome.

Usage:
    python test_local_auth.py

Credentials are loaded automatically from test_local.env (KEY=VALUE format).
You can also set them as environment variables before running.

Required vars:
    EON_EMAIL       — email address for eon.pl
    EON_PASSWORD    — password for eon.pl

Optional:
    EON_CAPSOLVER_API_KEY — capsolver.com API key (leave empty to test native only)
    CHROMIUM_BIN          — path to Chrome/Chromium executable
"""
import asyncio
import logging
import os
import sys

# Add eon_pl/ to path so 'src' is importable as a package (auth.py uses relative imports)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "eon_pl"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOG = logging.getLogger("test_auth")


def _load_dotenv(path: str) -> None:
    """Load KEY=VALUE pairs from a file into os.environ (never overwrites existing vars)."""
    if not os.path.exists(path):
        return
    loaded: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    if loaded:
        _LOG.info("Loaded from %s: %s", os.path.basename(path), ", ".join(loaded))


async def main() -> None:
    # Load credentials from test_local.env (sits next to this script)
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_local.env")
    _load_dotenv(env_file)

    email = os.environ.get("EON_EMAIL", "")
    password = os.environ.get("EON_PASSWORD", "")
    capsolver = os.environ.get("EON_CAPSOLVER_API_KEY", "")

    if not email or not password:
        print("ERROR: EON_EMAIL and EON_PASSWORD must be set.")
        print("  Create test_local.env with:")
        print("    EON_EMAIL=twoj@email.pl")
        print("    EON_PASSWORD=haslo")
        sys.exit(1)

    # On Windows: always detect local Chrome (overrides any Linux path from test_local.env)
    if sys.platform == "win32":
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

    # Debug screenshots go next to this script
    os.environ["EON_DATA_DIR"] = os.path.dirname(os.path.abspath(__file__))

    if capsolver:
        _LOG.info("CapSolver key provided — attempt 1: native, attempt 2: CapSolver")
    else:
        _LOG.info("No CapSolver key — testing native browser reCaptcha only")

    try:
        from src.auth import login_with_retry  # type: ignore[import]
        cookie = await login_with_retry(email, password, attempts=2, capsolver_key=capsolver)
        _LOG.info("SUCCESS! Cookie obtained (len=%d)", len(cookie))
        _LOG.info("Cookie starts: %s...", cookie[:40])
    except Exception as exc:
        _LOG.error("FAILED: %s", exc)
        _LOG.info("Check login_debug_*.png and login_debug_*.html in this directory")
        sys.exit(1)


asyncio.run(main())
