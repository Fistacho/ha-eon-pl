"""Coordinate fetching for E.ON Polska — addon edition.

Periodically pulls billing, OZE and hourly readings; tracks per-PPE state.
On EonAuthError it asks the auth module to refresh the cookie via Playwright.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from .api import (
    EonApiError,
    EonAuthError,
    EonPolskaClient,
    parse_chart_rows,
    parse_history_meters,
    parse_meter_readings,
)
from .const import (
    HOURLY_DATE_OFFSET_DAYS,
    STATS_BACKFILL_DAYS_FALLBACK,
    STATS_BACKFILL_FROM_YEAR_START,
    STATS_REPORT_MAX_DAYS,
)

_LOGGER = logging.getLogger(__name__)

# Callback the coordinator invokes when the cookie is dead and a fresh one is
# needed. Returns the new cookie. Provided by main loop.
ReloginFn = Callable[[], Awaitable[str]]


def _is_active(ku: dict[str, Any]) -> bool:
    return bool(ku.get("IsActive", True))


class EonCoordinator:
    """Single-tenant coordinator: holds latest fetched state."""

    def __init__(
        self,
        client: EonPolskaClient,
        selected_kus: list[str],
        relogin: ReloginFn | None = None,
    ) -> None:
        self._client = client
        self._selected_kus = {str(x) for x in selected_kus}
        self._relogin = relogin
        self._last_anchor: dict[str, datetime] = {}

        # Latest data
        self.ph: dict[str, Any] = {}
        self.contracts: dict[str, dict[str, Any]] = {}
        self.last_hour: dict[str, dict[str, Any]] = {}
        # All freshly-fetched hourly rows since last fetch (for stats import)
        self.fresh_rows: dict[str, list[dict[str, Any]]] = {}

    @property
    def client(self) -> EonPolskaClient:
        return self._client

    async def _with_relogin(
        self, fn: Callable[[], Awaitable[Any]], *, what: str
    ) -> Any:
        """Run fn(); on auth failure trigger Playwright relogin and retry once."""
        try:
            return await fn()
        except EonAuthError as exc:
            if not self._relogin:
                raise
            _LOGGER.warning("%s: auth failed (%s) — re-login via Playwright", what, exc)
            new_cookie = await self._relogin()
            self._client.set_cookie(new_cookie)
            return await fn()

    async def keepalive(self) -> bool:
        return await self._client.keepalive()

    async def fetch(self) -> None:
        """Pull GetPHList + per-contract data + hourly readings."""
        _LOGGER.info("Fetching E.ON data...")

        # Warmup so Sitecore endpoints accept requests
        await self._client.keepalive()

        try:
            ph = await self._with_relogin(self._client.get_ph_list, what="GetPHList")
        except EonApiError as exc:
            _LOGGER.error("GetPHList failed: %s", exc)
            return

        self.ph = ph or {}
        partners = self.ph.get("Partners") or []
        has_oze = bool(self.ph.get("HasOze"))
        _LOGGER.info("GetPHList ok, partners=%d HasOze=%s", len(partners), has_oze)

        new_contracts: dict[str, dict[str, Any]] = {}
        new_fresh: dict[str, list[dict[str, Any]]] = {}

        for partner in partners:
            for ku in partner.get("ContractAccounts", []):
                if not _is_active(ku):
                    continue
                ku_id = str(ku["Id"])
                if self._selected_kus and ku_id not in self._selected_kus:
                    continue

                # Non-OZE consumption charts are per-KU (+meter), not per-PPE.
                ku_consumption: dict[str, Any] | None = None
                if not has_oze:
                    ku_consumption = await self._fetch_consumption(ku_id)

                for ppe_index, ppe in enumerate(ku.get("PPEList", [])):
                    ppe_id = str(ppe["Id"])
                    key = f"{ku_id}_{ppe_id}"
                    cd: dict[str, Any] = {
                        "ku": ku,
                        "ppe": ppe,
                        "has_oze": has_oze,
                        "billing": None,
                        "oze": None,
                        "meter": None,
                        "consumption": None,
                    }
                    if has_oze:
                        try:
                            cd["billing"] = await self._with_relogin(
                                lambda: self._client.get_billing_data(ku_id, ppe_id),
                                what=f"billing[{key}]",
                            )
                        except (EonApiError, EonAuthError) as exc:
                            _LOGGER.warning("Billing unavailable for %s: %s", key, exc)

                        try:
                            cd["oze"] = await self._with_relogin(
                                lambda: self._client.get_oze_agr_data(ku_id, ppe_id),
                                what=f"oze[{key}]",
                            )
                        except (EonApiError, EonAuthError) as exc:
                            _LOGGER.warning("OZE unavailable for %s: %s", key, exc)

                    try:
                        cd["meter"] = await self._with_relogin(
                            self._client.get_meter_readings,
                            what=f"meter[{key}]",
                        )
                    except (EonApiError, EonAuthError) as exc:
                        _LOGGER.debug("Meter unavailable for %s: %s", key, exc)

                    if has_oze:
                        rows = await self._fetch_hourly(ku_id, ppe_id)
                        if rows:
                            new_fresh[key] = rows
                            rows.sort(key=lambda r: r["timestamp"])
                            self.last_hour[key] = rows[-1]
                    elif ppe_index == 0:
                        # Attach KU-level consumption to the first PPE only so a
                        # multi-PPE KU doesn't duplicate sensors and statistics.
                        cd["consumption"] = self._build_consumption(
                            ku_consumption, cd["meter"]
                        )
                        rows = self._consumption_stat_rows(ku_consumption)
                        if rows:
                            new_fresh[key] = rows

                    new_contracts[key] = cd

        self.contracts = new_contracts
        self.fresh_rows = new_fresh
        _LOGGER.info("Fetch done, contracts=%d, fresh stat rows=%d",
                     len(new_contracts), sum(len(v) for v in new_fresh.values()))

    async def _fetch_consumption(self, ku_id: str) -> dict[str, Any]:
        """Non-OZE data: CompareYear + Details consumption charts for one KU.

        Note: both the Historia-zuzycia meter list and GetMeterReadingsForKU
        follow the portal's current KU context, so multi-KU accounts are
        best-effort (the portal has no per-KU OZE flag either).
        """
        out: dict[str, Any] = {
            "year_rows": [], "details_rows": [],
            "meter_id": None, "meter_serial": None,
        }
        try:
            cy = await self._with_relogin(
                lambda: self._client.get_compare_year_data(ku_id),
                what=f"compare_year[{ku_id}]",
            )
            out["year_rows"] = parse_chart_rows(cy)
        except (EonApiError, EonAuthError) as exc:
            _LOGGER.warning("CompareYear unavailable for %s: %s", ku_id, exc)

        meters: list[dict[str, Any]] = []
        try:
            html = await self._with_relogin(
                self._client.get_history_page, what="history_page"
            )
            meters = [m for m in parse_history_meters(html) if m["active"]]
        except (EonApiError, EonAuthError) as exc:
            _LOGGER.warning("History page unavailable: %s", exc)

        if meters:
            out["meter_id"] = meters[0]["meter_id"]
            out["meter_serial"] = meters[0]["serial"]
            try:
                det = await self._with_relogin(
                    lambda: self._client.get_details_chart_data(
                        ku_id, meters[0]["meter_id"]
                    ),
                    what=f"details[{ku_id}]",
                )
                out["details_rows"] = parse_chart_rows(det)
            except (EonApiError, EonAuthError) as exc:
                _LOGGER.warning("Details chart unavailable for %s: %s", ku_id, exc)
        else:
            _LOGGER.warning("No active meter found on Historia-zuzycia for %s", ku_id)
        return out

    @staticmethod
    def _build_consumption(
        ku_consumption: dict[str, Any] | None, meter: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Flatten chart + meter data into the per-contract sensor payload."""
        cons = dict(ku_consumption or {})
        year_rows = cons.get("year_rows") or []
        if year_rows:
            cons["current_period_kwh"] = year_rows[-1]["kwh"]
            cons["current_period_date"] = year_rows[-1]["date"].isoformat()
        readings = [
            r for r in parse_meter_readings(meter)
            if r.get("energy_type") in (None, "Pobrana")
        ]
        if readings:
            latest = readings[0]
            cons["meter_reading_kwh"] = latest["value_kwh"]
            cons["meter_reading_date"] = latest["date"].isoformat()
            cons["meter_reading_type"] = latest["read_type"]
            if not cons.get("meter_serial"):
                cons["meter_serial"] = latest.get("serial")
        return cons

    @staticmethod
    def _consumption_stat_rows(
        ku_consumption: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Details increments → statistics rows (same shape as hourly rows).

        The newest chart category still accumulates until the next meter
        reading closes it, so it is held back until a newer one appears.
        """
        details = (ku_consumption or {}).get("details_rows") or []
        return [
            {
                "timestamp": datetime.combine(r["date"], datetime.min.time()),
                "imported_kwh": r["kwh"],
                "exported_kwh": 0.0,
                "balance_kwh": r["kwh"],
            }
            for r in details[:-1]
        ]

    async def _fetch_hourly(self, ku_id: str, ppe_id: str) -> list[dict[str, Any]]:
        """Fetch hourly readings for one PPE in chunks ≤ STATS_REPORT_MAX_DAYS days."""
        key = f"{ku_id}_{ppe_id}"
        date_from, date_to = self._stats_window(key)
        if date_from > date_to:
            return []

        rows: list[dict[str, Any]] = []
        cur_from = date_from
        while cur_from <= date_to:
            cur_to = min(cur_from + timedelta(days=STATS_REPORT_MAX_DAYS - 1), date_to)
            chunk = await self._fetch_chunk_with_retry(ku_id, ppe_id, cur_from, cur_to)
            if chunk is None:
                break
            rows.extend(chunk)
            cur_from = cur_to + timedelta(days=1)
        if rows:
            rows.sort(key=lambda r: r["timestamp"])
            self._last_anchor[key] = rows[-1]["timestamp"]
        return rows

    async def _fetch_chunk_with_retry(
        self, ku_id: str, ppe_id: str, date_from: date, date_to: date
    ) -> list[dict[str, Any]] | None:
        for attempt in (1, 2):
            await self._client.keepalive()
            try:
                return await self._with_relogin(
                    lambda: self._client.get_daily_readings(
                        ku_id, ppe_id, date_from, date_to
                    ),
                    what=f"hourly[{ppe_id}][{date_from}..{date_to}]",
                )
            except EonAuthError as exc:
                if attempt == 1:
                    _LOGGER.info("hourly: dropped session (%s) — retry", exc)
                    continue
                _LOGGER.warning("hourly unavailable for %s (%s..%s): %s",
                                ppe_id, date_from, date_to, exc)
                return None
            except EonApiError as exc:
                _LOGGER.warning("hourly error for %s (%s..%s): %s",
                                ppe_id, date_from, date_to, exc)
                return None
        return None

    def _stats_window(self, key: str) -> tuple[date, date]:
        today = date.today()
        date_to = today - timedelta(days=HOURLY_DATE_OFFSET_DAYS)

        anchor = self._last_anchor.get(key)
        if anchor is not None:
            return anchor.date() - timedelta(days=3), date_to

        if STATS_BACKFILL_FROM_YEAR_START:
            return date(today.year, 1, 1), date_to
        return today - timedelta(days=STATS_BACKFILL_DAYS_FALLBACK), date_to
