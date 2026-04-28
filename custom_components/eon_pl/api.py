"""E.ON Polska API client — session-cookie based."""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

try:
    from .const import (
        API_BASE,
        BASE_URL,
        COOKIE_NAME,
        ENDPOINT_BILLING,
        ENDPOINT_KEEPALIVE,
        ENDPOINT_METER_READINGS,
        ENDPOINT_OZE_AGR,
        ENDPOINT_OZE_DETAILS,
        ENDPOINT_OZE_REPORT,
        ENDPOINT_PH_LIST,
        OZE_REPORT_ITEM_ID,
        PAGE_DASHBOARD,
        PAGE_HISTORIA_ZUZYCIA,
    )
except ImportError:
    from eon_pl_const import (  # type: ignore[no-redef]
        API_BASE,
        BASE_URL,
        COOKIE_NAME,
        ENDPOINT_BILLING,
        ENDPOINT_KEEPALIVE,
        ENDPOINT_METER_READINGS,
        ENDPOINT_OZE_AGR,
        ENDPOINT_OZE_DETAILS,
        ENDPOINT_OZE_REPORT,
        ENDPOINT_PH_LIST,
        OZE_REPORT_ITEM_ID,
        PAGE_DASHBOARD,
        PAGE_HISTORIA_ZUZYCIA,
    )

_LOGGER = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_API_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "application/json",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Expires": "Sat, 01 Jan 2000 00:00:00 GMT",
    "Referer": PAGE_HISTORIA_ZUZYCIA,
}


class EonAuthError(Exception):
    """Session cookie expired or invalid."""


class EonApiError(Exception):
    """Unexpected API response."""


def _parse_pl_number(s: str) -> float:
    """Parse Polish-formatted number: '1 234,56' -> 1234.56. Empty -> 0.0."""
    s = (s or "").strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return 0.0
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_oze_csv(raw: bytes) -> list[dict[str, Any]]:
    """Parse OZE report CSV. Returns list of {timestamp, imported, exported, balance}.

    CSV columns (semicolon or comma separated):
      Dzień odczytu, Godzina odczytu, Energia pobrana, status danych,
      Energia wprowadzona, status danych, Bilans energii, status danych,
      Energia zbilansowana ujemna 15 min

    Hour values are 01:00..24:00 where 24:00 means end of day (00:00 next day).
    Timestamp returned represents the START of the hour (00:00..23:00 local time).
    """
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return []

    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        if len(row) < 7:
            continue
        date_str = row[0].strip()
        hour_str = row[1].strip()
        if not date_str or not hour_str:
            continue
        try:
            d = datetime.strptime(date_str, "%d.%m.%Y").date()
            hour_end = int(hour_str.split(":")[0])
            # Hour range 1..24: each row represents [hour-1, hour). Stamp at hour-1.
            hour_start = hour_end - 1
            ts = datetime.combine(d, datetime.min.time()) + timedelta(hours=hour_start)
        except (ValueError, IndexError):
            continue
        imported = _parse_pl_number(row[2])
        exported = _parse_pl_number(row[4])
        balance = _parse_pl_number(row[6]) if len(row) > 6 else (imported - exported)
        out.append(
            {
                "timestamp": ts,
                "imported_kwh": imported,
                "exported_kwh": exported,
                "balance_kwh": balance,
            }
        )
    return out


class EonPolskaClient:
    """HTTP client for eon.pl Mój E.ON portal."""

    def __init__(self, auth_cookie: str) -> None:
        self._auth_cookie = auth_cookie
        # Persistent client (built off-loop on first use to avoid blocking the
        # event loop on SSL certificate loading).
        self._client: httpx.AsyncClient | None = None

    def _build_client(self) -> httpx.AsyncClient:
        """Build a httpx.AsyncClient. Run via executor (loads SSL certs sync)."""
        return httpx.AsyncClient(
            headers=_API_HEADERS,
            cookies={COOKIE_NAME: self._auth_cookie},
            follow_redirects=False,
            timeout=30,
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                self._client = await loop.run_in_executor(None, self._build_client)
            except RuntimeError:
                # Not inside an event loop — build directly (e.g. CLI tests).
                self._client = self._build_client()
        return self._client

    def _get_client(self) -> httpx.AsyncClient:
        """Synchronous fallback; only safe outside the event loop."""
        if self._client is None or self._client.is_closed:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    @property
    def auth_cookie(self) -> str:
        """Return the latest .AspNet.Cookies value (server may rotate it)."""
        if self._client is not None:
            cur = self._client.cookies.get(COOKIE_NAME)
            if cur:
                return cur
        return self._auth_cookie

    def _check_auth(self, r: httpx.Response, url: str) -> None:
        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("location", "")
            low = location.lower()
            if "logowanie" in low:
                raise EonAuthError("Session expired — paste a fresh '.AspNet.Cookies'")
            # Sitecore endpoints (GenerateOzeReport, etc.) redirect to a portal
            # page like /mojeon/Historia-zuzycia when the Sitecore session is
            # not warmed up. Treat that as a recoverable auth-like error so the
            # caller can retry after a keepalive/page-warmup.
            if "/mojeon/" in low:
                raise EonAuthError(
                    f"Sitecore session needs warmup (redirected to {location})"
                )
            raise EonApiError(f"Unexpected redirect from {url} to {location}")
        if r.status_code == 401:
            raise EonAuthError("Unauthorized — invalid session cookie")

    async def _get(self, url: str, **params: Any) -> Any:
        client = await self._ensure_client()
        r = await client.get(url, params=params or None)
        self._check_auth(r, url)
        if r.status_code != 200:
            raise EonApiError(f"HTTP {r.status_code} from {url}")
        try:
            return r.json()
        except Exception as exc:
            raise EonApiError(f"Non-JSON response from {url}: {r.text[:200]}") from exc

    async def keepalive(self) -> bool:
        """Keep the session alive — and resurrect a dropped Sitecore session.

        Three-step flow:
          1. /api/keepalive — pings the API session.
          2. /mojeon (dashboard) — *resurrects* the portal/Sitecore session.
             This is the magic page: even when /Historia-zuzycia would 302 to
             /Logowanie, hitting /mojeon first reactivates the portal session
             using just .AspNet.Cookies, after which /api/sitecore/* works.
          3. /Historia-zuzycia — primes the specific Sitecore route used by
             GenerateOzeReport.
        """
        page_headers = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        try:
            client = await self._ensure_client()
            r1 = await client.get(ENDPOINT_KEEPALIVE)
            r2 = await client.get(PAGE_DASHBOARD, headers=page_headers)
            r3 = await client.get(PAGE_HISTORIA_ZUZYCIA, headers=page_headers)
            return (
                r1.status_code == 200
                and r2.status_code == 200
                and r3.status_code == 200
            )
        except Exception:
            return False

    async def validate_session(self) -> bool:
        try:
            data = await self._get(ENDPOINT_PH_LIST)
            return data.get("Partners") is not None
        except (EonAuthError, EonApiError):
            return False

    async def get_ph_list(self) -> dict[str, Any]:
        return await self._get(ENDPOINT_PH_LIST)

    async def get_billing_data(
        self, ku: str, ppe: str, year: int | None = None
    ) -> dict[str, Any]:
        if year is None:
            year = datetime.now().year
        return await self._get(
            ENDPOINT_BILLING,
            ku=ku, ppe=ppe,
            yearFrom=year - 3, yearTo=year + 1, cycle=12,
        )

    async def get_oze_agr_data(
        self, ku: str, ppe: str, year: int | None = None
    ) -> dict[str, Any]:
        if year is None:
            year = datetime.now().year
        return await self._get(
            ENDPOINT_OZE_AGR,
            ku=ku, ppe=ppe,
            yearFrom=year - 3, yearTo=year + 1,
        )

    async def get_oze_details(self) -> dict[str, Any]:
        return await self._get(ENDPOINT_OZE_DETAILS)

    async def get_meter_readings(self) -> dict[str, Any]:
        return await self._get(ENDPOINT_METER_READINGS)

    async def get_daily_readings(
        self,
        ku: str,
        ppe: str,
        date_from: date,
        date_to: date,
    ) -> list[dict[str, Any]]:
        """Fetch hourly energy readings for a date range as parsed CSV rows.

        eon.pl limits a single report to 180 days. Hours are returned as the
        START of each hourly bucket (local time), with imported/exported/balance
        in kWh.
        """
        if (date_to - date_from).days > 180:
            raise EonApiError("Date range exceeds 180 days (eon.pl limit)")

        client = await self._ensure_client()
        params = {
            "FormData.ItemId": OZE_REPORT_ITEM_ID,
            "FormData.Ku": ku,
            "FormData.SelectedPpe": ppe,
            "FormData.DateFrom": date_from.strftime("%d.%m.%Y"),
            "FormData.DateTo": date_to.strftime("%d.%m.%Y"),
            "FormData.ReportType": "Csv",
        }
        r = await client.get(
            ENDPOINT_OZE_REPORT,
            params=params,
            headers={"Referer": PAGE_HISTORIA_ZUZYCIA},
        )
        self._check_auth(r, ENDPOINT_OZE_REPORT)
        if r.status_code != 200:
            raise EonApiError(f"HTTP {r.status_code} from OzeReport")

        ctype = (r.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            # Sitecore returns the login page as HTML when session is half-dead.
            raise EonAuthError("OzeReport returned HTML — session needs refresh")
        return _parse_oze_csv(r.content)

    async def get_payments(self, report_type: str = "reports") -> dict[str, Any]:
        return await self._get(f"{API_BASE}/getpaymentsdata", type=report_type)
