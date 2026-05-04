# E.ON Polska — Home Assistant Add-on

Automatyczne pobieranie zużycia energii z **Mój E.ON** do Home Assistant. Login `email + hasło` raz w configu addona — reszta dzieje się sama.

![iot_class](https://img.shields.io/badge/iot_class-cloud_polling-blue)
![type](https://img.shields.io/badge/type-HA_Add--on-green)

## Co robi

- **Logowanie automatyczne** — Selenium + chromium odpalane on-demand (~30 s peak), jeśli portal zaakceptuje reCAPTCHA v3.
- **Tryb ręcznego ciasteczka** — wariant bez CapSolvera: logujesz się normalnie w przeglądarce i wklejasz `.AspNet.Cookies` albo pełny nagłówek `Cookie` w Web UI.
- **Hourly imported / exported** → Home Assistant **external statistics** (Energy Dashboard).
- **Year-to-date backfill** przy pierwszym uruchomieniu (chunki 60 dni).
- **Live "ostatnia godzina"** sensors (`pobrana / wprowadzona / bilans`).
- **Roczne agregaty** + **bieżący okres rozliczeniowy** z `GetBillingData` / `GetOzeAgrData`.
- **MQTT auto-discovery** — encje pojawiają się w HA same.
- **Web UI ingress** — status sesji, ostatni login, ręczny refresh.
- Self-healing: keepalive co 5 min, automatyczne re-login co 12 h lub przy 302 do `/Logowanie`.

## Wymagania

- Home Assistant OS / Supervised z dostępem do Add-on Store
- **MQTT broker** (np. addon Mosquitto) — addon korzysta z auto-discovery
- Konto na <https://eon.pl/mojeon>

## Instalacja

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Dodaj: `https://github.com/Fistacho/ha-eon-pl`
3. Zainstaluj **E.ON Polska**
4. **Configuration**:

   ```yaml
   email: twoj@email.pl
   password: TwojeHasło
   scan_interval_hours: 6
   cookie_refresh_hours: 12
   manual_cookie_only: false # true = bez Selenium/CapSolver, tylko wklejone cookie
   selected_kus: []          # puste = wszystkie aktywne KU
   log_level: info
   mqtt_discovery: true
   ```

5. **Start**
6. **Open Web UI** — zobacz status, kliknij "Pobierz dane teraz" jeśli chcesz przyspieszyć pierwszy fetch.

Encje pojawiają się w HA przez MQTT auto-discovery w ciągu kilku sekund po pierwszym fetchu.

Jeśli automatyczne logowanie kończy się błędem reCAPTCHA, ustaw `manual_cookie_only: true`, uruchom addon, otwórz **Open Web UI** i wklej `.AspNet.Cookies` albo pełny nagłówek `Cookie` skopiowany po ręcznym zalogowaniu na `eon.pl`. Addon będzie zapisywał odnowione cookie, jeśli E.ON zwróci nowe `Set-Cookie` podczas keepalive albo pobierania danych.

## Encje (per KU+PPE)

| Encja | Typ | Źródło |
| --- | --- | --- |
| `sensor.eon_<key>_consumption_current_period` | total_increasing kWh | `GetBillingData` |
| `sensor.eon_<key>_imported_year` | total_increasing kWh | `GetOzeAgrData` |
| `sensor.eon_<key>_exported_year` | total_increasing kWh | `GetOzeAgrData` |
| `sensor.eon_<key>_balance_year` | total kWh | `GetOzeAgrData` |
| `sensor.eon_<key>_last_hour_imported` | total kWh | hourly CSV |
| `sensor.eon_<key>_last_hour_exported` | total kWh | hourly CSV |
| `sensor.eon_<key>_last_hour_balance` | total kWh | hourly CSV |

Plus **external statistics** (Energy Dashboard):

- `eon_pl:imported_<PPE>` — godzinowy kumulowany pobór
- `eon_pl:exported_<PPE>` — godzinowy kumulowany eksport

## Energy Dashboard

**Settings → Dashboards → Energy**:

- **Electricity grid → Add consumption** → `eon_pl:imported_<PPE>`
- **Electricity grid → Add return** → `eon_pl:exported_<PPE>`
- **Solar panels → Add solar production** → encja Twojego inwertera

## Architektura

```text
┌──────────────────────────────────────────┐
│ HA Add-on container                       │
│                                            │
│ ┌──────────┐  ┌─────────────────┐         │
│ │ Web UI   │  │ Main loop        │         │
│ │ (ingress)│  │  - keepalive 5min │         │
│ └─────┬────┘  │  - fetch every Nh │         │
│       │       │  - relogin every Nh│        │
│       │       │    or manual cookie │        │
│       └──────►│  - on-demand login │        │
│               └────┬─────────┬─────┘         │
│                    ▼         ▼               │
│          ┌──────────────┐  ┌─────────────┐  │
│          │ Playwright + │  │ httpx async │  │
│          │ chromium     │  │ → eon.pl    │  │
│          │ (on-demand)  │  └─────────────┘  │
│          └──────────────┘                    │
│                              │               │
│       ┌──────────────────────┴────┐          │
│       ▼                           ▼          │
│  ┌─────────┐              ┌──────────┐       │
│  │  MQTT   │ → discovery  │ HA REST  │       │
│  │ pub     │   + state    │ recorder │       │
│  └─────────┘              │ statistics│      │
│                           └──────────┘       │
└──────────────────────────────────────────────┘
```

**RAM profile:**

- Idle: ~30 MB (sam Python + httpx + aiomqtt)
- Login peak: ~500 MB przez ~30 s (chromium), potem zwolniony
- Średnio: ~30 MB

## Migracja z `custom_components/eon_pl`

Stary HACS-ready custom_component (v0.1–v0.2) został zarchiwizowany w git history (tag `v0.2.1`). Addon to kompletny rewrite:

- Stare encje `sensor.e_on_*` z entity_registry można bezpiecznie usunąć (Settings → Devices & Services → ⋮ → Entities → filter `e_on`).
- Stare `eon_pl:imported_<PPE>` external statistics **zostają w recorderze** — addon wznawia od ostatniej znanej sumy, żadnych dziur w Energy Dashboard.
- W configu HA **nie** trzeba nic dodawać — addon publikuje wszystko przez MQTT discovery.

## Limitacje

- Hourly data ma **24–48 h opóźnienia publikacji**. Addon używa `today − 3` jako górnej granicy okna.
- Wymaga MQTT brokera w HA.
- Nieafiliowany z E.ON Polska. Korzystasz na własne ryzyko.

## License

MIT — patrz [LICENSE](./LICENSE).
