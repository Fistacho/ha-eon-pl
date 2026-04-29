"""Push hourly readings into HA recorder via WebSocket API.

The HA recorder exposes `recorder/import_statistics` only on the WebSocket
API — not as a REST service. The REST `/api/services/recorder/...` endpoint
returned 400 in v1.1.1/1.1.2 because that service simply isn't registered
on the REST side. We connect over ws:// using a long-lived access token
(or SUPERVISOR_TOKEN) and send the import command directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .const import DOMAIN
from .state_store import StateStore

# eon.pl publishes hourly readings stamped in Polish local time. Convert to
# UTC before pushing to HA's recorder, which only accepts UTC timestamps.
_LOCAL_TZ = ZoneInfo("Europe/Warsaw")


def _ws_url(http_url: str) -> str:
    """Convert an HA HTTP URL to its WebSocket counterpart."""
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):].rstrip("/") + "/api/websocket"
    return "ws://" + http_url.removeprefix("http://").rstrip("/") + "/api/websocket"

_LOGGER = logging.getLogger(__name__)


def _stat_id(kind: str, ppe: str) -> str:
    return f"{DOMAIN}:{kind}_{ppe}"


class StatsImporter:
    def __init__(self, ha_url: str, ha_token: str, state: StateStore) -> None:
        self._url = ha_url.rstrip("/")
        self._token = ha_token
        self._state = state

    async def import_hourly(
        self, fresh_rows: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Import imported/exported energy stats per PPE."""
        if not self._token:
            _LOGGER.warning("HA token missing, skipping statistics import")
            return

        for key, rows in fresh_rows.items():
            ppe = key.split("_", 1)[-1]
            if not rows:
                continue
            await self._import_one(
                statistic_id=_stat_id("imported", ppe),
                name=f"E.ON Polska — pobrana {ppe}",
                rows=rows,
                value_key="imported_kwh",
            )
            await self._import_one(
                statistic_id=_stat_id("exported", ppe),
                name=f"E.ON Polska — wprowadzona {ppe}",
                rows=rows,
                value_key="exported_kwh",
            )

    async def _import_one(
        self,
        statistic_id: str,
        name: str,  # kept for API compat / log labeling, not sent in payload
        rows: list[dict[str, Any]],
        value_key: str,
    ) -> None:
        last_sum, last_start = self._state.stats_anchor(statistic_id)
        running = last_sum
        stats: list[dict[str, Any]] = []
        latest_ts: datetime | None = None
        for r in rows:
            ts: datetime = r["timestamp"]
            # CSV timestamps from eon.pl are bare local Polish time. Localize
            # to Europe/Warsaw, then convert to UTC for the recorder.
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_LOCAL_TZ)
            ts_utc = ts.astimezone(timezone.utc)
            if last_start is not None and ts_utc <= last_start:
                continue
            v = float(r.get(value_key) or 0.0)
            running += v
            stats.append({"start": ts_utc.isoformat(), "state": v, "sum": running})
            latest_ts = ts_utc
        if not stats:
            return

        metadata = {
            "has_mean": False,
            "has_sum": True,
            "name": name,
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": "kWh",
        }
        ok, err = await self._ws_import(metadata, stats)
        if not ok:
            _LOGGER.warning("import_statistics %s failed: %s", statistic_id, err)
            return

        if latest_ts is not None:
            self._state.update_stats_anchor(statistic_id, running, latest_ts)
        _LOGGER.info("Imported %d rows for %s (running sum=%.3f)",
                     len(stats), statistic_id, running)

    async def _ws_import(
        self, metadata: dict[str, Any], stats: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """Push one statistic via the WebSocket recorder/import_statistics command."""
        ws_url = _ws_url(self._url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, timeout=30) as ws:
                    # Step 1: HA sends auth_required → we reply with the token.
                    greet = await ws.receive_json(timeout=10)
                    if greet.get("type") != "auth_required":
                        return False, f"unexpected greeting: {greet}"
                    await ws.send_json({"type": "auth", "access_token": self._token})
                    auth = await ws.receive_json(timeout=10)
                    if auth.get("type") != "auth_ok":
                        return False, f"auth failed: {auth}"

                    # Step 2: send the import command.
                    cmd_id = 1
                    await ws.send_json({
                        "id": cmd_id,
                        "type": "recorder/import_statistics",
                        "metadata": metadata,
                        "stats": stats,
                    })

                    # Wait for the matching result (skip event/feed messages).
                    while True:
                        msg = await ws.receive_json(timeout=30)
                        if msg.get("id") != cmd_id:
                            continue
                        if msg.get("type") == "result":
                            if msg.get("success"):
                                return True, ""
                            return False, json.dumps(msg.get("error") or msg)
                        return False, f"unexpected message: {msg}"
        except asyncio.TimeoutError:
            return False, "WebSocket timeout"
        except aiohttp.ClientError as exc:
            return False, f"WebSocket connection failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"unexpected error: {exc}"
