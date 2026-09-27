# E.ON Polska — Home Assistant Add-on

**Nieoficjalny scraper** portalu **Mój E.ON** (eon.pl) do Home Assistant Energy Dashboard. To nie jest integracja oparta o publiczne API — E.ON Polska nie udostępnia takiego API dla klientów indywidualnych. Login `email + hasło` raz w configu addona — reszta dzieje się sama.

![iot_class](https://img.shields.io/badge/iot_class-cloud_polling-blue)
![type](https://img.shields.io/badge/type-HA_Add--on-green)

**Ten projekt nie jest afiliowany, sponsorowany ani wspierany przez E.ON Polska.** Przeczytaj sekcję [Jak to działa i ryzyko](#jak-to-działa-i-ryzyko) przed instalacją.

## Co robi

- **Logowanie automatyczne** — przeglądarka Chromium sterowana przez [`nodriver`](https://github.com/ultrafunkamsterdam/nodriver) (Chrome DevTools Protocol, nie Selenium/WebDriver) odpalana on-demand (~30 s peak), jeśli portal zaakceptuje reCAPTCHA v3.
- **Tryb ręcznego ciasteczka** — wariant bez automatyzacji przeglądarki i bez CapSolvera: logujesz się normalnie w przeglądarce i wklejasz `.AspNet.Cookies` albo pełny nagłówek `Cookie` w Web UI.
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
   manual_cookie_only: false # true = bez automatyzacji przeglądarki/CapSolver, tylko wklejone cookie
   selected_kus: []          # puste = wszystkie aktywne KU
   log_level: info
   mqtt_discovery: true
   ```

5. **Start**
6. **Open Web UI** — zobacz status, kliknij "Pobierz dane teraz" jeśli chcesz przyspieszyć pierwszy fetch.

Encje pojawiają się w HA przez MQTT auto-discovery w ciągu kilku sekund po pierwszym fetchu.

Jeśli automatyczne logowanie kończy się błędem reCAPTCHA, ustaw `manual_cookie_only: true`, uruchom addon, otwórz **Open Web UI** i wklej `.AspNet.Cookies` albo pełny nagłówek `Cookie` skopiowany po ręcznym zalogowaniu na `eon.pl`. Addon będzie zapisywał odnowione cookie, jeśli E.ON zwróci nowe `Set-Cookie` podczas keepalive albo pobierania danych.

## Jak to działa i ryzyko

Portal Mój E.ON nie ma publicznego API dla klientów indywidualnych, więc addon **udaje przeglądarkę**:

- Otwiera prawdziwy Chromium sterowany przez `nodriver` (protokół CDP, taki jak DevTools — nie Selenium/WebDriver), wypełnia formularz logowania i przechodzi reCAPTCHA v3, po czym zapisuje ciasteczko sesji (`.AspNet.Cookies`) i re-używa go między restartami.
- Sesja jest odświeżana najrzadziej jak się da — domyślnie re-login tylko co `cookie_refresh_hours` (12 h) albo gdy sesja realnie wygaśnie — częste logowanie zwiększa ryzyko, że E.ON rozpozna automatyzację i zablokuje konto/sesję.
- **CapSolver (opcjonalny, `capsolver_api_key`)** — płatna usługa rozwiązująca reCAPTCHA, używana tylko jako fallback gdy token z lokalnej przeglądarki dostanie za niski wynik. Do CapSolvera trafiają wyłącznie: publiczny adres strony logowania eon.pl, publiczny `site_key` reCAPTCHA i User-Agent — **nigdy Twój email, hasło ani ciasteczko sesji**.
- Addon działa na **Twoim własnym koncie** — login i hasło podajesz sam w opcjach addona i nigdzie poza żądaniami do eon.pl (i publicznymi metadanymi do CapSolvera, jeśli włączony) nie są wysyłane.

To, mimo działania na własnym koncie, **może naruszać regulamin Mój E.ON** — w szczególności pkt XII.2 (zabezpieczenie loginu/hasła przed osobami trzecimi) oraz pkt XII.7 (zakaz nadużywania Serwisu, pod który E.ON może podciągnąć zautomatyzowane logowanie). Realna konsekwencja to możliwa **blokada konta lub sesji** przez E.ON. Używasz addona na własne ryzyko — projekt nie jest afiliowany z E.ON Polska.

Szczegóły uprawnień add-onu (Supervisor/HA API) i dokładny opis trybu `manual_cookie_only` — patrz [DOCS.md](./eon_pl/DOCS.md).

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
│          │ nodriver +   │  │ httpx async │  │
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
