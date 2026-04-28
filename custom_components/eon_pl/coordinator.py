"""DataUpdateCoordinator for E.ON Polska."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EonApiError, EonAuthError, EonPolskaClient
from .const import (
    CONF_SELECTED_KUS,
    DOMAIN,
    KEEPALIVE_INTERVAL_MINUTES,
    SCAN_INTERVAL_HOURS,
    STATS_BACKFILL_DAYS_FALLBACK,
    STATS_BACKFILL_FROM_YEAR_START,
    STATS_REPORT_MAX_DAYS,
)

_LOGGER = logging.getLogger(__name__)


def _stat_id(kind: str, ppe: str) -> str:
    return f"{DOMAIN}:{kind}_{ppe}"


def _is_active(ku: dict[str, Any]) -> bool:
    """E.ON marks closed contracts with IsActive=False; skip those."""
    return bool(ku.get("IsActive", True))


class EonPolskaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch E.ON data periodically; keep session alive between fetches."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EonPolskaClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=SCAN_INTERVAL_HOURS),
        )
        self.client = client
        self._entry = entry
        self._unsub_keepalive = None
        self._last_hour: dict[str, dict[str, Any]] = {}
        self._warmed_up = False

    @property
    def last_hour(self) -> dict[str, dict[str, Any]]:
        return self._last_hour

    async def async_config_entry_first_refresh(self) -> None:
        await super().async_config_entry_first_refresh()
        self._start_keepalive()

    async def async_unload(self) -> None:
        if self._unsub_keepalive:
            self._unsub_keepalive()
            self._unsub_keepalive = None
        await self.client.aclose()

    def _start_keepalive(self) -> None:
        if self._unsub_keepalive:
            self._unsub_keepalive()
        self._unsub_keepalive = async_track_time_interval(
            self.hass,
            self._async_keepalive,
            timedelta(minutes=KEEPALIVE_INTERVAL_MINUTES),
        )

    async def _async_keepalive(self, _now: Any = None) -> None:
        ok = await self.client.keepalive()
        if ok:
            _LOGGER.debug("E.ON keepalive OK (with page warmup)")
            self._persist_cookie_if_changed()
        else:
            _LOGGER.warning("E.ON keepalive failed — session may be expiring")

    def _persist_cookie_if_changed(self) -> None:
        from .const import CONF_COOKIE

        latest = self.client.auth_cookie
        stored = self._entry.data.get(CONF_COOKIE)
        if latest and latest != stored:
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, CONF_COOKIE: latest},
            )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._fetch()
        except EonAuthError as exc:
            raise ConfigEntryAuthFailed(
                "E.ON session expired. Open integration settings and paste a new "
                "'.AspNet.Cookies' value from your browser."
            ) from exc

    async def _fetch(self) -> dict[str, Any]:
        _LOGGER.info("eon_pl: _fetch start")

        # Sitecore endpoints (e.g. GenerateOzeReport) need the Historia-zuzycia
        # page visited at least once per session before they accept requests.
        # `keepalive` does that GET; run it once on the first fetch after
        # setup so the year-start backfill doesn't silently 302 to login.
        if not self._warmed_up:
            warmup_ok = await self.client.keepalive()
            _LOGGER.info("eon_pl: initial warmup keepalive=%s", warmup_ok)
            self._warmed_up = warmup_ok

        try:
            ph = await self.client.get_ph_list()
        except EonApiError as exc:
            _LOGGER.error("eon_pl: GetPHList failed: %s", exc)
            raise UpdateFailed(f"E.ON API error: {exc}") from exc

        partners = ph.get("Partners") or []
        _LOGGER.info(
            "eon_pl: GetPHList ok, partners=%d HasOze=%s",
            len(partners), ph.get("HasOze"),
        )

        result: dict[str, Any] = {"ph": ph, "contracts": {}}

        # Optional whitelist from options flow. Empty / missing => all active.
        selected = self._entry.options.get(CONF_SELECTED_KUS) or []
        selected_set: set[str] = {str(x) for x in selected}

        for partner in partners:
            for ku in partner.get("ContractAccounts", []):
                if not _is_active(ku):
                    _LOGGER.info(
                        "eon_pl: skip inactive KU %s (%s)",
                        ku.get("KuDisplayName"), ku.get("Id"),
                    )
                    continue
                ku_id = ku["Id"]
                if selected_set and str(ku_id) not in selected_set:
                    _LOGGER.info(
                        "eon_pl: skip KU %s (%s) — not in selected_kus",
                        ku.get("KuDisplayName"), ku_id,
                    )
                    continue
                _LOGGER.info(
                    "eon_pl: processing KU %s (%s)",
                    ku.get("KuDisplayName"), ku_id,
                )
                for ppe_entry in ku.get("PPEList", []):
                    ppe_id = ppe_entry["Id"]
                    key = f"{ku_id}_{ppe_id}"
                    contract_data: dict[str, Any] = {
                        "ku": ku,
                        "ppe": ppe_entry,
                        "billing": None,
                        "oze": None,
                        "meter": None,
                    }

                    try:
                        contract_data["billing"] = await self.client.get_billing_data(
                            ku_id, ppe_id
                        )
                    except (EonApiError, EonAuthError) as exc:
                        _LOGGER.warning("Billing data unavailable for %s: %s", key, exc)

                    if ph.get("HasOze"):
                        try:
                            contract_data["oze"] = await self.client.get_oze_agr_data(
                                ku_id, ppe_id
                            )
                        except (EonApiError, EonAuthError) as exc:
                            _LOGGER.warning("OZE data unavailable for %s: %s", key, exc)

                    try:
                        contract_data["meter"] = await self.client.get_meter_readings()
                    except (EonApiError, EonAuthError) as exc:
                        _LOGGER.debug("Meter readings unavailable: %s", exc)

                    # Daily readings + statistics import. Failures here must NOT
                    # break the rest of the update — last_hour stays best-effort.
                    try:
                        await self._refresh_hourly(ku_id, ppe_id)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.exception(
                            "Hourly refresh failed for %s: %s", key, exc
                        )

                    result["contracts"][key] = contract_data

        # Don't persist cookie inside the fetch — `async_update_entry`
        # can trigger a config-entry reload, which would loop back into
        # this coordinator. Cookie rotation is persisted only by the
        # 30-min keepalive callback (outside the fetch path).
        _LOGGER.info(
            "eon_pl: _fetch done, %d contract(s) populated",
            len(result["contracts"]),
        )
        return result

    async def _fetch_chunk_with_retry(
        self, ku_id: str, ppe_id: str, date_from: date, date_to: date
    ) -> list[dict[str, Any]] | None:
        """Fetch a CSV chunk.

        eon.pl's Sitecore session "drains" with each report request, so we
        run a /mojeon-resurrect keepalive *before every chunk* — that
        consistently keeps GenerateOzeReport answering with CSV instead of
        302-ing to the portal. On a transient auth error we keepalive again
        and retry once.
        """
        for attempt in (1, 2):
            await self.client.keepalive()
            try:
                return await self.client.get_daily_readings(
                    ku_id, ppe_id, date_from, date_to
                )
            except EonAuthError as exc:
                if attempt == 1:
                    _LOGGER.info(
                        "eon_pl: report endpoint dropped session (%s) — retrying",
                        exc,
                    )
                    continue
                _LOGGER.warning(
                    "Hourly readings unavailable for %s (%s..%s): %s",
                    ppe_id, date_from, date_to, exc,
                )
                return None
            except EonApiError as exc:
                _LOGGER.warning(
                    "Hourly readings error for %s (%s..%s): %s",
                    ppe_id, date_from, date_to, exc,
                )
                return None
        return None

    async def _refresh_hourly(self, ku_id: str, ppe_id: str) -> None:
        """Fetch hourly readings and (best-effort) push them to HA statistics.

        Always populates ``last_hour`` even if the recorder API misbehaves,
        so the live sensors stay populated.
        """
        date_from, date_to = self._stats_window(ppe_id)
        if date_from > date_to:
            return

        rows: list[dict[str, Any]] = []
        cur_from = date_from
        while cur_from <= date_to:
            cur_to = min(cur_from + timedelta(days=STATS_REPORT_MAX_DAYS - 1), date_to)
            chunk = await self._fetch_chunk_with_retry(ku_id, ppe_id, cur_from, cur_to)
            if chunk is None:
                break
            rows.extend(chunk)
            cur_from = cur_to + timedelta(days=1)

        if not rows:
            return

        rows.sort(key=lambda r: r["timestamp"])
        self._last_hour[f"{ku_id}_{ppe_id}"] = rows[-1]

        try:
            await self._push_statistics(ppe_id, rows)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Statistics import failed for %s (sensors still updated): %s",
                ppe_id, exc,
            )

    def _stats_window(self, ppe_id: str) -> tuple[date, date]:
        """Return (date_from, date_to) for the hourly report.

        eon.pl publishes hourly readings with a 24–48h delay; asking for a
        date that the operator hasn't published yet makes the report endpoint
        302 to /Historia-zuzycia. Stay 2 days behind to be safe.

        On first run we backfill from January 1st of the current year so the
        Energy Dashboard has a full year-to-date history. On subsequent runs
        we only re-pull the last few days to backfill late publications.
        """
        today = date.today()
        date_to = today - timedelta(days=2)

        # Inspect HA recorder for the latest known statistic timestamp.
        last_anchor = self._last_known_stat_start(ppe_id)
        if last_anchor is not None:
            local_anchor = dt_util.as_local(last_anchor).date()
            return local_anchor - timedelta(days=3), date_to

        if STATS_BACKFILL_FROM_YEAR_START:
            return date(today.year, 1, 1), date_to
        return today - timedelta(days=STATS_BACKFILL_DAYS_FALLBACK), date_to

    def _last_known_stat_start(self, ppe_id: str) -> datetime | None:
        """Return the latest known stats anchor, or None if recorder API fails."""
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                get_last_statistics,
            )
        except ImportError:
            return None

        recorder = get_instance(self.hass)
        imported_id = _stat_id("imported", ppe_id)
        try:
            last = recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, imported_id, True, {"sum"}
            )
        except Exception:  # noqa: BLE001
            return None
        # async_add_executor_job returns a coroutine — but we're in a sync
        # context here. Caller awaits via _stats_window which is sync; defer
        # to a heavier path: signal "no anchor known" and let _push_statistics
        # query lazily inside its async context.
        return None

    async def _push_statistics(
        self, ppe_id: str, rows: list[dict[str, Any]]
    ) -> None:
        """Push hourly imported/exported readings to HA external statistics."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            get_last_statistics,
        )

        recorder = get_instance(self.hass)
        imported_id = _stat_id("imported", ppe_id)
        exported_id = _stat_id("exported", ppe_id)

        last_imp = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, imported_id, True, {"sum", "start"}
        )
        last_exp = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, exported_id, True, {"sum", "start"}
        )

        last_imp_start = _last_start(last_imp.get(imported_id))
        last_exp_start = _last_start(last_exp.get(exported_id))
        imp_sum = float(_last_sum(last_imp.get(imported_id)) or 0.0)
        exp_sum = float(_last_sum(last_exp.get(exported_id)) or 0.0)

        imp_stats: list[dict[str, Any]] = []
        exp_stats: list[dict[str, Any]] = []

        for row in rows:
            ts_local = row["timestamp"].replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            ts_utc = dt_util.as_utc(ts_local)
            imp = float(row["imported_kwh"])
            exp = float(row["exported_kwh"])
            if last_imp_start is None or ts_utc > last_imp_start:
                imp_sum += imp
                imp_stats.append({"start": ts_utc, "state": imp, "sum": imp_sum})
            if last_exp_start is None or ts_utc > last_exp_start:
                exp_sum += exp
                exp_stats.append({"start": ts_utc, "state": exp, "sum": exp_sum})

        if imp_stats:
            async_add_external_statistics(
                self.hass,
                {
                    "has_mean": False,
                    "has_sum": True,
                    "name": f"E.ON Polska — pobrana {ppe_id}",
                    "source": DOMAIN,
                    "statistic_id": imported_id,
                    "unit_of_measurement": "kWh",
                },
                imp_stats,
            )
        if exp_stats:
            async_add_external_statistics(
                self.hass,
                {
                    "has_mean": False,
                    "has_sum": True,
                    "name": f"E.ON Polska — wprowadzona {ppe_id}",
                    "source": DOMAIN,
                    "statistic_id": exported_id,
                    "unit_of_measurement": "kWh",
                },
                exp_stats,
            )
        _LOGGER.debug(
            "Imported %d/%d statistics rows for %s",
            len(imp_stats), len(exp_stats), ppe_id,
        )


def _last_start(stats: list[dict[str, Any]] | None) -> datetime | None:
    if not stats:
        return None
    raw = stats[0].get("end") or stats[0].get("start")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    return raw


def _last_sum(stats: list[dict[str, Any]] | None) -> float | None:
    if not stats:
        return None
    return stats[0].get("sum")
