# E.ON Polska — Add-on docs

Automatyczne pobieranie zużycia energii z portalu **Mój E.ON** (eon.pl) do Home Assistant Energy Dashboard.

## Configuration

```yaml
email: ""                  # Twój email do eon.pl
password: ""               # Hasło do eon.pl
scan_interval_hours: 6     # co ile godzin pobierać dane (1-24)
cookie_refresh_hours: 12   # co ile godzin re-login (1-24)
selected_kus: []           # lista KU ID (puste = wszystkie aktywne)
log_level: info            # debug | info | warning | error
mqtt_discovery: true       # publikuj encje przez MQTT auto-discovery
```

### email

Adres email konta na <https://eon.pl/mojeon>.

### password

Hasło. Trzymane tylko lokalnie (`/data/options.json` w kontenerze addona).

### scan_interval_hours

Jak często addon ma pobierać dane (billing, OZE, hourly readings). Default: `6` (4 razy dziennie). E.ON publikuje dane z 24–48 h opóźnieniem, częstsze pobieranie nie da świeższych danych.

### cookie_refresh_hours

Jak często wykonać Playwright re-login w celu odświeżenia `.AspNet.Cookies`. Default: `12`. Portal E.ON ma absolute session timeout — re-login co 12 h zapewnia że sesja nigdy nie wygasa.

### selected_kus

Lista numerów konta umowy (KU) do śledzenia. Pozostaw puste żeby śledzić wszystkie aktywne KU.

Aby znaleźć ID swojego KU: po pierwszym uruchomieniu addona otwórz **Web UI** → tam są wylistowane wszystkie aktywne KU + PPE.

## Web UI

Po starcie addona kliknij **Open Web UI**. Zobaczysz:

- ostatni login + ostatni fetch
- status cookie (zapisany / brak)
- liczniki (ostatnia godzina) per KU+PPE
- przyciski: **Zaloguj ponownie** / **Pobierz dane teraz**

## MQTT

Addon wymaga skonfigurowanego MQTT brokera (np. addon Mosquitto). Encje są publikowane przez Home Assistant **auto-discovery** — pojawiają się w HA same po pierwszym fetchu.

Tematy MQTT:

- `homeassistant/sensor/eon_pl_<key>_<sensor>/config` — discovery
- `eon_pl/<key>/state` — JSON state
- `eon_pl/<key>/availability` — `online` / `offline`

## Energy Dashboard

External statistics są wysyłane przez REST API do recorder HA:

- `eon_pl:imported_<PPE>` — godzinowy kumulowany pobór z sieci
- `eon_pl:exported_<PPE>` — godzinowy kumulowany oddanie do sieci

Konfiguruj w **Settings → Dashboards → Energy → Electricity grid**.

## Troubleshooting

### Login nie działa

Sprawdź **Logs** addona. Najczęstsze przyczyny:

- zły email / hasło → `Login did not redirect to dashboard`
- portal E.ON zmienił layout strony logowania → selectory w `auth.py` wymagają aktualizacji (zgłoś issue)
- captcha-blok → odczekaj 30 min, spróbuj ponownie

### Brak danych godzinowych

E.ON publikuje hourly readings z 24–48 h opóźnieniem. Dane sprzed 3+ dni powinny być zawsze dostępne.

### MQTT timeout

Sprawdź czy addon Mosquitto (lub inny broker) działa. Addon korzysta z poświadczeń wystawianych automatycznie przez Supervisor — żadna ręczna konfiguracja po stronie addona nie jest potrzebna.

## Source

<https://github.com/Fistacho/ha-eon-pl>
