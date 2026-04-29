"""E.ON Polska — endpoints and constants (addon edition)."""
from __future__ import annotations

BASE_URL = "https://eon.pl/mojeon"
API_BASE = f"{BASE_URL}/api"

ENDPOINT_PH_LIST = f"{API_BASE}/GetPHList"
ENDPOINT_BILLING = f"{API_BASE}/oze/GetBillingData"
ENDPOINT_OZE_AGR = f"{API_BASE}/oze/GetOzeAgrData"
ENDPOINT_OZE_DETAILS = f"{API_BASE}/oze/GetOzeDetails"
ENDPOINT_METER_READINGS = f"{API_BASE}/GetMeterReadingsForKU"
ENDPOINT_PAYMENTS = f"{API_BASE}/getpaymentsdata"
ENDPOINT_KEEPALIVE = f"{API_BASE}/keepalive"
ENDPOINT_LOGIN = f"{BASE_URL}/Logowanie"
ENDPOINT_OZE_REPORT = f"{API_BASE}/sitecore/OzeReport/GenerateOzeReport"
PAGE_DASHBOARD = BASE_URL
PAGE_HISTORIA_ZUZYCIA = f"{BASE_URL}/Historia-zuzycia"

OZE_REPORT_ITEM_ID = "{FF7D98E7-D452-4C49-ABB0-A12A2BFB38BE}"
COOKIE_NAME = ".AspNet.Cookies"

DOMAIN = "eon_pl"

# eon.pl publishes hourly readings with a 24-48h delay. We aim for today-2;
# if a chunk including the last day comes back as a 302 (data not yet
# published) the per-chunk retry will still surface earlier days.
HOURLY_DATE_OFFSET_DAYS = 2
STATS_REPORT_MAX_DAYS = 60
STATS_BACKFILL_FROM_YEAR_START = True
STATS_BACKFILL_DAYS_FALLBACK = 30
KEEPALIVE_INTERVAL_MINUTES = 5

# MQTT discovery prefix — Home Assistant default is "homeassistant".
MQTT_DISCOVERY_PREFIX = "homeassistant"
MQTT_STATE_PREFIX = "eon_pl"
