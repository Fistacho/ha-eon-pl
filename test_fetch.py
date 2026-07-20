"""Test the full fetch pipeline with a pasted session cookie.

No Playwright, no MQTT, no HA — just the HTTP client + coordinator against
the live portal. Reads EON_COOKIE from test_local.env (KEY=VALUE format).

Usage:
    python test_fetch.py

KU/PPE identifiers are masked in output so logs are safe to share.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "eon_pl"))

logging.basicConfig(
    level=logging.DEBUG if "-v" in sys.argv else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOG = logging.getLogger("test_fetch")


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


def _mask(value: object) -> str:
    s = str(value)
    return "…" + s[-3:] if len(s) > 3 else s


async def main() -> int:
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_local.env")
    _load_dotenv(env_file)
    cookie = os.environ.get("EON_COOKIE", "")
    if not cookie:
        print("ERROR: EON_COOKIE must be set in test_local.env")
        print("(log in at eon.pl, copy the .AspNet.Cookies value from DevTools)")
        return 1

    from src.api import EonPolskaClient
    from src.coordinator import EonCoordinator
    from src.mqtt_publisher import MqttPublisher

    client = EonPolskaClient(cookie)
    try:
        if not await client.validate_session():
            print("ERROR: session invalid — paste a fresh .AspNet.Cookies value")
            return 1
        coord = EonCoordinator(client, selected_kus=[], relogin=None)
        await coord.fetch()

        print()
        print(f"HasOze: {coord.ph.get('HasOze')}")
        print(f"contracts: {len(coord.contracts)}")
        for key, cd in coord.contracts.items():
            ku_id, ppe_id = key.split("_", 1)
            print(f"\n--- contract {_mask(ku_id)}_{_mask(ppe_id)} "
                  f"has_oze={cd.get('has_oze')} ---")
            payload = MqttPublisher._state_payload(cd, coord.last_hour.get(key))
            for k, v in payload.items():
                print(f"  {k}: {v}")
            rows = coord.fresh_rows.get(key) or []
            print(f"  stat rows: {len(rows)}")
            if rows:
                print(f"  first: {rows[0]}")
                print(f"  last:  {rows[-1]}")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
