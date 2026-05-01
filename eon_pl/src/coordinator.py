"""Coordinate fetching for E.ON Polska — addon edition.

Periodically pulls billing, OZE and hourly readings; tracks per-PPE state.
On EonAuthError it asks the auth module to refresh the cookie via Playwright.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from .api import EonApiError, EonAuthError, EonPolskaClient
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
        _LOGGER.info("GetPHList ok, partners=%d HasOze=%s",
                     len(partners), self.ph.get("HasOze"))

        new_contracts: dict[str, dict[str, Any]] = {}
        new_fresh: dict[str, list[dict[str, Any]]] = {}

        for partner in partners:
            for ku in partner.get("ContractAccounts", []):
                if not _is_active(ku):
                    continue
                ku_id = str(ku["Id"])
                if self._selected_kus and ku_id not in self._selected_kus:
                    continue
                for ppe in ku.get("PPEList", []):
                    ppe_id = str(ppe["Id"])
                    key = f"{ku_id}_{ppe_id}"
                    cd: dict[str, Any] = {
                        "ku": ku,
                        "ppe": ppe,
                        "billing": None,
                        "oze": None,
                        "meter": None,
                    }
                    try:
                        cd["billing"] = await self._with_relogin(
                            lambda: self._client.get_billing_data(ku_id, ppe_id),
                            what=f"billing[{key}]",
                        )
                    except (EonApiError, EonAuthError) as exc:
                        _LOGGER.warning("Billing unavailable for %s: %s", key, exc)

                    if self.ph.get("HasOze"):
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

                    rows = await self._fetch_hourly(ku_id, ppe_id)
                    if rows:
                        new_fresh[key] = rows
                        rows.sort(key=lambda r: r["timestamp"])
                        self.last_hour[key] = rows[-1]

                    new_contracts[key] = cd

        self.contracts = new_contracts
        self.fresh_rows = new_fresh
        _LOGGER.info("Fetch done, contracts=%d, fresh hourly rows=%d",
                     len(new_contracts), sum(len(v) for v in new_fresh.values()))

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
