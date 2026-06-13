"""Test hybrid login: Chrome generates reCaptcha token → HTTP POST submits form."""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "eon_pl"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOG = logging.getLogger("test_hybrid")


def _load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


async def main() -> None:
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_local.env")
    _load_dotenv(env_file)

    email = os.environ.get("EON_EMAIL", "")
    password = os.environ.get("EON_PASSWORD", "")

    if not email or not password:
        print("ERROR: EON_EMAIL and EON_PASSWORD must be set in test_local.env")
        sys.exit(1)

    # On Windows: detect Chrome
    if sys.platform == "win32":
        for c in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]:
            if os.path.exists(c):
                os.environ["CHROMIUM_BIN"] = c
                _LOG.info("Using Chrome: %s", c)
                break

    os.environ["EON_DATA_DIR"] = os.path.dirname(os.path.abspath(__file__))

    _LOG.info("Testing hybrid login (Chrome token + HTTP POST)...")
    try:
        from src.auth import _hybrid_login_async  # type: ignore[import]
        cookie = await _hybrid_login_async(email, password, timeout_s=90)
        _LOG.info("SUCCESS! Cookie obtained (len=%d)", len(cookie))
        _LOG.info("Cookie starts: %s...", cookie[:40])
    except Exception as exc:
        _LOG.error("FAILED: %s", exc)
        sys.exit(1)


asyncio.run(main())
