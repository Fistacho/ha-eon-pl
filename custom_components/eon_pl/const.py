DOMAIN = "eon_pl"
CONF_COOKIE = "auth_cookie"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
# Options flow: list of KU IDs (numery konta umowy) the user wants tracked.
# Empty / missing => all active KUs (legacy behavior).
CONF_SELECTED_KUS = "selected_kus"

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
PAGE_DASHBOARD = BASE_URL  # /mojeon
PAGE_HISTORIA_ZUZYCIA = f"{BASE_URL}/Historia-zuzycia"

OZE_REPORT_ITEM_ID = "{FF7D98E7-D452-4C49-ABB0-A12A2BFB38BE}"

SCAN_INTERVAL_HOURS = 6
# Sitecore session can drop after ~15 min idle (independent from .AspNet.Cookies),
# which makes /api/sitecore/* endpoints redirect to /Logowanie. We refresh more
# often than that so GenerateOzeReport keeps working between scheduled updates.
KEEPALIVE_INTERVAL_MINUTES = 10
COOKIE_NAME = ".AspNet.Cookies"

# On first run we backfill all hourly readings from the start of the current
# calendar year. eon.pl caps a single report at 180 days, so we chunk the
# request when needed.
STATS_BACKFILL_FROM_YEAR_START = True
# eon.pl says 180 days but in practice the Sitecore session "drains" with each
# /api/sitecore/* request. Smaller chunks + a fresh /mojeon resurrect before
# every chunk is what makes long backfills succeed.
STATS_REPORT_MAX_DAYS = 60
STATS_BACKFILL_DAYS_FALLBACK = 30
