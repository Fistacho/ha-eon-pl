# E.ON Polska (Mój E.ON) — Home Assistant integration

Custom integration for [Mój E.ON](https://eon.pl/mojeon) — Polish energy distributor portal.
Pulls hourly meter readings (import/export) plus billing/OZE summaries and feeds the
Energy Dashboard with full year-to-date statistics.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![iot_class](https://img.shields.io/badge/iot_class-cloud_polling-blue)

## What it does

- **Hourly imported / exported energy** from your bidirectional smart meter,
  pushed to Home Assistant **external statistics** (visible in the Energy Dashboard).
- **Year-to-date backfill** on first run — chunks of 60 days, ~5s total.
- **Live "last hour" sensors** (`pobrana / wprowadzona / bilans ostatnia godzina`).
- **Yearly totals** from OZE aggregate API: `pobrana / wprowadzona / bilans (bieżący rok)`.
- **Current billing-period consumption** from billing chart data.
- Self-healing **Sitecore session keepalive** every 10 min using a `/mojeon` "resurrect"
  pattern — no manual re-login under normal use.

E.ON Polska publishes hourly readings with a ~24–48 h delay, so the most recent
data point is always 1–2 days behind.

## Setup at a glance

| Where | What you provide |
| --- | --- |
| eon.pl portal | Log in once in your browser |
| DevTools → Application → Cookies → eon.pl | Copy value of `.AspNet.Cookies` |
| HA → Settings → Devices & Services → Add `E.ON Polska` | Paste the cookie |

The integration takes it from there. No password is stored — just the session cookie.

### Three ways to grab the cookie

The cookie has the `HttpOnly` flag, so JavaScript bookmarklets can't read it.
Pick whichever of these is easiest:

**1. DevTools → Application** (manual, no extras)

- Log in at <https://eon.pl/mojeon> → F12 → **Application** tab → **Cookies → eon.pl**
- Click `.AspNet.Cookies` → copy "Value"

**2. DevTools → Network** (works in any browser)

- Log in → F12 → **Network** tab → filter "mojeon"
- Click any request → **Headers → Request Headers → cookie**
- The string `.AspNet.Cookies=<value>;` is in there — copy `<value>`

**3. "Cookie-Editor" extension** (one-click)

- Install [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) for Chrome / Firefox
- On eon.pl: open the extension → click `.AspNet.Cookies` → "Copy value"

> **Why not auto-login?** eon.pl uses Google reCAPTCHA v3 with a strict score threshold.
> No realistic way to pass it from a Python HTTP client without a real browser. Manual
> cookie + 10-min keepalive keeps the session alive effectively forever in practice.

## Installation

### Via HACS (recommended)

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/fistacho/ha-eon-pl` as **Integration**
3. Install **E.ON Polska (Mój E.ON)**
4. Restart Home Assistant
5. **Settings → Devices & Services → Add Integration → E.ON Polska**

### Manual

Copy `custom_components/eon_pl/` into your HA `config/custom_components/` directory,
restart HA, then add the integration from the UI.

## Entities

For each active contract account (KU) + connection point (PPE):

| Entity | Type | Source |
| --- | --- | --- |
| `sensor.e_on_<KU>_<PPE>_zuzycie_biezacy_okres_rozliczeniowy` | total_increasing kWh | `GetBillingData` |
| `sensor.e_on_<KU>_<PPE>_pobrana_biezacy_rok` | total_increasing kWh | `GetOzeAgrData` (annual) |
| `sensor.e_on_<KU>_<PPE>_wprowadzona_biezacy_rok` | total_increasing kWh | `GetOzeAgrData` (annual) |
| `sensor.e_on_<KU>_<PPE>_bilans_biezacy_rok` | total kWh | `GetOzeAgrData` (annual) |
| `sensor.e_on_<KU>_<PPE>_pobrana_ostatnia_godzina` | total kWh | hourly CSV |
| `sensor.e_on_<KU>_<PPE>_wprowadzona_ostatnia_godzina` | total kWh | hourly CSV |
| `sensor.e_on_<KU>_<PPE>_bilans_ostatnia_godzina` | total kWh | hourly CSV |

Plus **external statistics** (don't appear as entities, only in the Energy Dashboard):

- `eon_pl:imported_<PPE>` — hourly cumulative import sum
- `eon_pl:exported_<PPE>` — hourly cumulative export sum

## Energy Dashboard

`Settings → Dashboards → Energy`:

- **Electricity grid → Add consumption** → `eon_pl:imported_<PPE>`
- **Electricity grid → Add return** → `eon_pl:exported_<PPE>`
- **Solar panels → Add solar production** → your inverter total (e.g. `sensor.total_yield`
  from the Huawei Solar integration)

HA computes self-consumption, self-sufficiency and home usage automatically.

## Multiple contracts (KUs)

eon.pl groups energy contracts under "Konto umowy" (KU). If your account has
more than one active KU, the integration creates sensors for **all of them**
by default. To narrow it down:

**Settings → Devices & Services → E.ON Polska → Configure** → pick which KU(s)
to track from the list. Leave empty to keep tracking all active ones.

Closed/inactive KUs (`IsActive=False` in the API) are always skipped.

## Cookie expiry

The `.AspNet.Cookies` value is long-lived (weeks to months). The portal-side
Sitecore session is short — but the integration refreshes it every 10 minutes
via a `/mojeon` page hit, which silently reactivates a dropped session.

If the auth cookie itself expires you'll get a `Reauthentication required` notification.
Re-paste a fresh cookie in **Devices & Services → E.ON Polska → Configure**.

## Limitations

- Hourly data has a **24–48 h publishing delay**. The integration uses
  `today − 2` as the upper bound to avoid 302 redirects.
- No automatic login (reCAPTCHA v3, see above).
- Not affiliated with E.ON Polska. Use at your own risk.

## License

MIT — see [LICENSE](./LICENSE).
