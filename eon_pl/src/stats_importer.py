"""Push hourly readings into HA recorder via Supervisor REST API.

Uses POST /api/services/recorder/import_statistics with the SUPERVISOR_TOKEN
that HA injects when ``homeassistant_api: true`` is set in addon config.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .const import DOMAIN
from .state_store import StateStore

# eon.pl publishes hourly readings stamped in Polish local time. Convert to
# UTC before pushing to HA's recorder, which only accepts UTC timestamps.
_LOCAL_TZ = ZoneInfo("Europe/Warsaw")

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

        # The `recorder.import_statistics` REST service does NOT accept the
        # `name` field that the Python API (async_add_external_statistics)
        # uses — sending it triggers HTTP 400 schema validation error.
        # Statistic name is taken from the discovery / device registry.
        payload = {
            "statistic_id": statistic_id,
            "source": DOMAIN,
            "unit_of_measurement": "kWh",
            "has_mean": False,
            "has_sum": True,
            "stats": stats,
        }
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{self._url}/api/services/recorder/import_statistics",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if r.status_code >= 400:
                _LOGGER.warning(
                    "import_statistics %s failed: HTTP %s — body: %s",
                    statistic_id, r.status_code, r.text[:500],
                )
                return

        if latest_ts is not None:
            self._state.update_stats_anchor(statistic_id, running, latest_ts)
        _LOGGER.info("Imported %d rows for %s (running sum=%.3f)",
                     len(stats), statistic_id, running)
