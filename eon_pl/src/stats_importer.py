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
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .const import DOMAIN, STATS_REVISION_DAYS
from .redact import redact
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


def _hour_start_utc(ts: datetime) -> datetime:
    """UTC start of the CSV hour whose bare local start (label - 1 h) is `ts`.

    eon.pl labels each hour by the local hour it ends at and always sends 24
    rows per day. Resolving the end keeps the DST days right: on the spring
    day the row ending at the non-existent 02:00 (a zero placeholder) falls on
    the same hour as the next row; on the autumn day the repeated hour has no
    row. Any other hour is unchanged.
    """
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc)
    end = (ts + timedelta(hours=1)).replace(tzinfo=_LOCAL_TZ)
    return end.astimezone(timezone.utc) - timedelta(hours=1)


def _is_repeated_local_hour(ts_utc: datetime) -> bool:
    local = ts_utc.astimezone(_LOCAL_TZ)
    return local.replace(fold=1 - local.fold).utcoffset() != local.utcoffset()


def _hourly_points(
    rows: list[dict[str, Any]], value_key: str
) -> list[tuple[datetime, float]]:
    """[(local aware hour start, kWh)] in time order, one point per hour.

    Rows on the same hour are summed (spring placeholder); the autumn hour
    without a row gets 0 so the statistic has no gap.
    """
    # Keyed by UTC: aware datetimes sharing a tzinfo compare by wall clock.
    by_hour: dict[datetime, float] = {}
    for r in rows:
        start = _hour_start_utc(r["timestamp"])
        by_hour[start] = by_hour.get(start, 0.0) + float(r.get(value_key) or 0.0)
    hours = sorted(by_hour)
    for prev, cur in pairwise(hours):
        missing = prev + timedelta(hours=1)
        if cur - prev == timedelta(hours=2) and _is_repeated_local_hour(missing):
            by_hour[missing] = 0.0
    return [(h.astimezone(_LOCAL_TZ), by_hour[h]) for h in sorted(by_hour)]


def _rewrite_start(
    points: list[tuple[datetime, float]],
    last_sum: float,
    last_start: datetime | None,
    days: dict[date, tuple[float, float]],
) -> tuple[int | None, float]:
    """(index of the first row to import, sum before it); index None = nothing to do."""
    if last_start is None:
        return 0, 0.0
    # Re-import from the oldest checkpointed day, but only when the fetch
    # reaches the anchor: a shorter fetch would leave later hours with sums
    # that no longer follow the rewritten ones.
    if points[-1][0] >= last_start:
        for i, (ts, _) in enumerate(points):
            if ts.hour == 0 and ts <= last_start and ts.date() in days:
                return i, days[ts.date()][0]
    for i, (ts, _) in enumerate(points):
        if ts > last_start:
            return i, last_sum
    return None, last_sum


def _parse_hour_stat(
    result: Any, statistic_id: str, start: datetime
) -> tuple[bool, dict[str, float] | None]:
    """Validate a statistics_during_period answer; (False, None) when malformed."""
    if not isinstance(result, dict):
        return False, None
    rows = result.get(statistic_id) or []
    if not isinstance(rows, list):
        return False, None
    want_ms = int(start.timestamp() * 1000)
    for row in rows:
        if not isinstance(row, dict) or row.get("start") != want_ms:
            continue
        state, total = row.get("state"), row.get("sum")
        if not isinstance(state, (int, float)) or not isinstance(total, (int, float)):
            return False, None
        return True, {"state": float(state), "sum": float(total)}
    return True, None


class StatsImporter:
    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        state: StateStore,
        price_import_pln_kwh: float = 0.0,
        value_export_pln_kwh: float = 0.0,
    ) -> None:
        self._url = ha_url.rstrip("/")
        self._token = ha_token
        self._state = state
        # PLN per kWh; 0 (or less) disables the matching cost statistic.
        self._price_import = price_import_pln_kwh
        self._value_export = value_export_pln_kwh

    async def import_hourly(
        self, fresh_rows: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Import imported/exported energy (kWh) and optional cost (PLN) stats per PPE."""
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
            # Cost statistics for Energy Dashboard (stat_cost / stat_compensation).
            # HA does not price external statistics itself, so we push PLN sums.
            # A price change only affects days first imported after it (a revised
            # day keeps the price it was first imported with).
            if self._price_import > 0:
                await self._import_one(
                    statistic_id=_stat_id("imported_cost", ppe),
                    name=f"E.ON Polska — koszt poboru {ppe}",
                    rows=rows,
                    value_key="imported_kwh",
                    factor=self._price_import,
                    unit="PLN",
                )
            if self._value_export > 0:
                await self._import_one(
                    statistic_id=_stat_id("exported_compensation", ppe),
                    name=f"E.ON Polska — wartość oddanej {ppe}",
                    rows=rows,
                    value_key="exported_kwh",
                    factor=self._value_export,
                    unit="PLN",
                )

    async def _import_one(
        self,
        statistic_id: str,
        name: str,  # kept for API compat / log labeling, not sent in payload
        rows: list[dict[str, Any]],
        value_key: str,
        factor: float = 1.0,
        unit: str = "kWh",
    ) -> None:
        """Import one cumulative statistic: per-hour value = row[value_key] × factor.

        Hours already imported are re-imported (overwritten by start) from the
        oldest day with a checkpoint, so portal revisions of provisional days
        reach the recorder; older hours are left untouched.
        """
        last_sum, last_start = self._state.stats_anchor(statistic_id)
        days = self._state.stats_checkpoints(statistic_id)
        points = _hourly_points(rows, value_key)
        if not points:
            return

        full_reimport = False
        start_idx: int | None = None
        running = 0.0
        prices: dict[date, float] = {}
        if last_start is not None and self._state.full_reimport_pending(statistic_id):
            base = await self._full_reimport_base(statistic_id, points, last_start)
            if base is not None:
                # Old checkpoint sums predate the rewrite: keep only their
                # prices; a checkpoint is stored again only when recomputed.
                full_reimport, start_idx, running = True, 0, base
                prices, days = {d: f for d, (_, f) in days.items()}, {}
        if start_idx is None:
            start_idx, running = _rewrite_start(points, last_sum, last_start, days)
        if start_idx is None:
            return

        stats: list[dict[str, Any]] = []
        for ts_local, kwh in points[start_idx:]:
            ts_utc = ts_local.astimezone(timezone.utc)
            day = ts_local.date()
            if ts_local.hour == 0:
                # A revised day keeps the price it was first imported with.
                days[day] = (running, days[day][1] if day in days else prices.get(day, factor))
            v = kwh * (days[day][1] if day in days else prices.get(day, factor))
            running += v
            stats.append({"start": ts_utc.isoformat(), "state": v, "sum": running})
        latest_ts = points[-1][0].astimezone(timezone.utc)

        # HA >= 2025.11 schema; HA 2026.11 warns when mean_type or unit_class
        # is missing. mean_type 0 = no mean; unit_class "energy" lets HA
        # convert kWh, PLN has no converter (None).
        metadata = {
            "mean_type": 0,
            "has_sum": True,
            "name": name,
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_class": "energy" if unit == "kWh" else None,
            "unit_of_measurement": unit,
        }
        ok, err = await self._ws_import(metadata, stats)
        if not ok:
            _LOGGER.warning("import_statistics %s failed: %s",
                            redact(statistic_id), redact(err))
            return

        keep_from = points[-1][0].date() - timedelta(days=STATS_REVISION_DAYS)
        self._state.update_stats_anchor(
            statistic_id,
            running,
            latest_ts,
            {d: cp for d, cp in days.items() if d >= keep_from},
        )
        if full_reimport:
            self._state.clear_full_reimport(statistic_id)
            _LOGGER.warning("One-time re-import of %s from %s done (%d rows)",
                            redact(statistic_id), points[0][0].date(), len(stats))
        _LOGGER.info("Imported %d rows for %s (running sum=%.3f)",
                     len(stats), redact(statistic_id), running)

    async def _full_reimport_base(
        self,
        statistic_id: str,
        points: list[tuple[datetime, float]],
        last_start: datetime,
    ) -> float | None:
        """Sum before the first row for the one-time re-import, or None to skip it.

        Statistics written by eon_pl <= 1.3.0 may hold provisional values, and
        by <= 1.3.1 misplaced DST hours (kWh and cost alike); they are rewritten
        once from a fetch that reaches the anchor and starts on 1 January (first
        fetch after start) or at HA's first hour of the statistic (data begin
        later in the year). The base is taken from HA's own row for that first
        hour, so the rewrite joins older history without a jump.
        """
        first, last = points[0][0], points[-1][0]
        if last < last_start:
            return None
        first_utc = first.astimezone(timezone.utc)
        ok, row = await self._ws_hour_stat(statistic_id, first_utc)
        if ok and row is not None and (first.month, first.day, first.hour) != (1, 1, 0):
            # Not a year-start fetch: rewrite only when HA holds nothing older
            # (otherwise the older hours would stay unrepaired: wait).
            ok, older = await self._ws_hour_stat(statistic_id, first_utc - timedelta(hours=1))
            if ok and older is not None:
                return None
        if not ok:
            _LOGGER.warning("%s: HA statistics unavailable — one-time re-import "
                            "postponed to the next start", redact(statistic_id))
            return None
        if row is None:
            _LOGGER.warning("%s: HA has no hour %s — one-time re-import skipped",
                            redact(statistic_id), first_utc.isoformat())
            self._state.clear_full_reimport(statistic_id)
            return None
        return row["sum"] - row["state"]

    async def _ws_hour_stat(
        self, statistic_id: str, start: datetime
    ) -> tuple[bool, dict[str, float] | None]:
        """HA's recorded {state, sum} for the hour starting at `start` (UTC)."""
        ok, result = await self._ws_call({
            "type": "recorder/statistics_during_period",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
            "period": "hour",
            "types": ["state", "sum"],
            "statistic_ids": [statistic_id],
        })
        if not ok:
            _LOGGER.warning("statistics_during_period %s failed: %s",
                            redact(statistic_id), redact(result))
            return False, None
        return _parse_hour_stat(result, statistic_id, start)

    async def _ws_import(
        self, metadata: dict[str, Any], stats: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """Push one statistic via the WebSocket recorder/import_statistics command."""
        ok, result = await self._ws_call({
            "type": "recorder/import_statistics",
            "metadata": metadata,
            "stats": stats,
        })
        return ok, "" if ok else str(result)

    async def _ws_call(self, command: dict[str, Any]) -> tuple[bool, Any]:
        """Send one WebSocket command; (True, result) or (False, error text)."""
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

                    # Step 2: send the command.
                    cmd_id = 1
                    await ws.send_json({"id": cmd_id, **command})

                    # Wait for the matching result (skip event/feed messages).
                    while True:
                        msg = await ws.receive_json(timeout=30)
                        if msg.get("id") != cmd_id:
                            continue
                        if msg.get("type") == "result":
                            if msg.get("success"):
                                return True, msg.get("result")
                            return False, json.dumps(msg.get("error") or msg)
                        return False, f"unexpected message: {msg}"
        except asyncio.TimeoutError:
            return False, "WebSocket timeout"
        except aiohttp.ClientError as exc:
            return False, f"WebSocket connection failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"unexpected error: {exc}"
