# E.ON Polska — Add-on docs

Automatyczne pobieranie zużycia energii z portalu **Mój E.ON** (eon.pl) do Home Assistant Energy Dashboard.

## Configuration

```yaml
email: ""                  # Twój email do eon.pl
password: ""               # Hasło do eon.pl
scan_interval_hours: 6     # co ile godzin pobierać dane (1-24)
cookie_refresh_hours: 12   # co ile godzin re-login (1-24)
manual_cookie_only: false  # true = nie uruchamiaj przeglądarki (nodriver), używaj tylko wklejonego cookie
selected_kus: []           # lista KU ID (puste = wszystkie aktywne)
log_level: info            # debug | info | warning | error
mqtt_discovery: true       # publikuj encje przez MQTT auto-discovery
price_import_pln_kwh: 0    # zł/kWh brutto za pobraną energię (0 = bez statystyki kosztu)
value_export_pln_kwh: 0    # zł/kWh brutto za oddaną energię (0 = bez statystyki wartości)

# Opcjonalne — używane gdy Protection mode jest WŁĄCZONY i Supervisor nie
# wstrzykuje tokenu (najczęstszy scenariusz). Wystarczy raz wpisać.
mqtt_host: ""              # np. "core-mosquitto" lub IP brokera
mqtt_port: 1883
mqtt_user: ""              # user MQTT (z Mosquitto addona albo własny)
mqtt_password: ""
ha_token: ""               # Long-lived access token z Profile → Security
```

### Tryby działania

Addon ma **dwa tryby konfiguracji** w zależności od tego czy Protection mode jest włączony:

**A) Protection mode WYŁĄCZONY** (zalecane, mniej pól do wypełnienia)

- Supervisor wstrzykuje SUPERVISOR_TOKEN automatycznie.
- Addon pobiera MQTT credentials z Mosquitto addona sam.
- Pomijasz pola `mqtt_*` i `ha_token` — zostaw puste.

Aby wyłączyć: **Profile → Advanced mode = ON** → **Settings → Add-ons → E.ON Polska → Info** → toggle **Protection mode** → **OFF**.

**B) Protection mode WŁĄCZONY** (domyślne, bezpieczniejsze)

- Wpisujesz **ręcznie** dane MQTT brokera + Long-lived access token HA.
- Działa bez modyfikacji systemowych ustawień.

Pola wymagane:

- `mqtt_host` — adres brokera (Mosquitto addon: `core-mosquitto`, lub IP)
- `mqtt_user`, `mqtt_password` — z konfiguracji Mosquitto / Twojego brokera
- `ha_token` — wygeneruj w **HA Profile → Security → Long-Lived Access Tokens → Create Token**, skopiuj wartość

### email

Adres email konta na <https://eon.pl/mojeon>.

### password

Hasło. Trzymane tylko lokalnie (`/data/options.json` w kontenerze addona).

### scan_interval_hours

Jak często addon ma pobierać dane (billing, OZE, hourly readings). Default: `6` (4 razy dziennie). E.ON publikuje dane z 24–48 h opóźnieniem, częstsze pobieranie nie da świeższych danych.

### cookie_refresh_hours

Jak często wykonać automatyczne ponowne logowanie (przeglądarka sterowana przez `nodriver`) w celu odświeżenia `.AspNet.Cookies`. Default: `12`. Portal E.ON ma absolute session timeout — re-login co 12 h zapewnia że sesja nigdy nie wygasa. Zbyt częste re-logowanie zwiększa ryzyko wykrycia automatyzacji przez reCAPTCHA i ewentualnej blokady konta — nie skracaj tej wartości bez potrzeby.

### manual_cookie_only

Ustaw `true`, jeśli nie chcesz używać automatycznego logowania ani CapSolvera. Addon nie uruchomi wtedy przeglądarki przy starcie, cyklicznie ani po wygaśnięciu sesji. Trzeba otworzyć **Web UI**, zalogować się ręcznie na `eon.pl` w zwykłej przeglądarce, skopiować `.AspNet.Cookies` albo pełny nagłówek `Cookie` i wkleić go w sekcji „Ręczne wklejenie ciasteczka”.

Addon zapisuje odnowioną sesję, jeśli E.ON zwróci nowe `Set-Cookie` podczas keepalive albo pobierania danych. Gdy cookie mimo tego wygaśnie, addon nadal będzie działał w trybie zdegradowanym i poprosi w logach o wklejenie świeżego ciasteczka.

### selected_kus

Lista numerów konta umowy (KU) do śledzenia. Pozostaw puste żeby śledzić wszystkie aktywne KU.

Aby znaleźć ID swojego KU: po pierwszym uruchomieniu addona otwórz **Web UI** → tam są wylistowane wszystkie aktywne KU + PPE.

### price_import_pln_kwh / value_export_pln_kwh

Cena (zł/kWh, brutto) używana do statystyk kosztu w Energy Dashboard — patrz [Koszt energii](#koszt-energii). Default: `0` = wyłączone (addon importuje wtedy tylko kWh, jak dotychczas).

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

## Jak to działa i ryzyko

To jest **nieoficjalny scraper**, nie integracja oparta o publiczne API E.ON Polska — portal Mój E.ON nie udostępnia go dla użytkowników indywidualnych. Addon:

- Loguje się do `eon.pl/mojeon` automatyzując prawdziwą przeglądarkę Chromium przez **`nodriver`** (sterowanie po Chrome DevTools Protocol, nie WebDriver/Selenium) — po to, by przejść reCAPTCHA v3, które blokuje zwykłe zapytania HTTP.
- Opcjonalnie (`capsolver_api_key`) korzysta z płatnej usługi **capsolver.com** jako fallback, gdy token z lokalnej przeglądarki dostanie zbyt niski wynik reCAPTCHA. Do CapSolvera wysyłane są wyłącznie: adres strony logowania (`https://eon.pl/mojeon/Logowanie` — publiczny URL, nie login/cookie), publiczny `site_key` reCAPTCHA osadzony w tej stronie, nazwa akcji (`login`) oraz User-Agent przeglądarki. **CapSolver nigdy nie otrzymuje Twojego emaila, hasła ani ciasteczka sesji** — te dane addon wysyła wyłącznie bezpośrednio do eon.pl.
- Działa na **Twoim własnym koncie** Mój E.ON, przy pomocy loginu i hasła, które sam podajesz w opcjach addona.

To narusza literę regulaminu Mój E.ON — pkt XII.2 (obowiązek zabezpieczenia loginu/hasła przed osobami trzecimi; tu login/hasło trafiają do kodu strony trzeciej, choć nie do CapSolvera) oraz pkt XII.7 (zakaz „nadużywania Serwisu”, co automatyzacja logowania może naruszać w interpretacji E.ON). Konsekwencją może być **zablokowanie konta** lub sesji przez E.ON — używasz addona **na własne ryzyko i odpowiedzialność**, projekt nie jest afiliowany z E.ON Polska ani przez niego wspierany.

Częste logowanie zwiększa ryzyko wykrycia jako bot i zablokowania — dlatego addon domyślnie reużywa zapisanego ciasteczka sesji i re-loguje się tylko co `cookie_refresh_hours` (domyślnie 12 h) albo po realnym wygaśnięciu sesji.

### Tryb bez usług zewnętrznych — `manual_cookie_only`

Jeśli nie chcesz polegać na automatyzacji przeglądarki ani na CapSolverze, ustaw `manual_cookie_only: true`. Wtedy addon **nigdy** nie uruchamia Chromium i nie kontaktuje się z CapSolverem — logujesz się ręcznie w swojej zwykłej przeglądarce na `eon.pl`, kopiujesz ciasteczko `.AspNet.Cookies` (DevTools → Application → Cookies) i wklejasz je w Web UI addona. Addon nadal będzie próbował odświeżyć to ciasteczko z odpowiedzi `Set-Cookie` serwera podczas normalnego pobierania danych, ale gdy sesja wygaśnie ostatecznie, poprosi Cię (w logu i przez powiadomienie HA) o wklejenie nowego ciasteczka. To najbezpieczniejszy wariant pod względem regulaminu — nie ma tu żadnej automatyzacji logowania, tylko ręczne działanie właściciela konta.

### Uzasadnienie uprawnień addona

| Uprawnienie | Po co jest używane |
| --- | --- |
| `hassio_api: true` + `services: [mqtt:want]` | Odczyt danych logowania do brokera MQTT wystawionych przez Supervisor (`GET /services/mqtt`), gdy nie są dostępne przez wstrzyknięte zmienne środowiskowe. Używane tylko do odczytu configu MQTT. |
| `hassio_role` (pominięty, więc domyślne `default`) | Najniższa dostępna rola (least privilege) — kod woła wyłącznie `GET /services/mqtt`, a ten endpoint jest wystawiony przez wzorzec `api_bypass` Supervisora (`/services.*`), sprawdzany niezależnie od `hassio_role`/`role_access`, więc rola `default` w zupełności wystarcza. Do 1.2.2 addon jawnie deklarował `hassio_role: manager` — szerzej niż potrzeba; od 1.2.2 klucz jest usunięty (linter Supervisora traktuje jawne `default` jako zbędne, bo to wartość domyślna schematu). |
| `homeassistant_api: true` | Import statystyk godzinowych do recordera HA przez WebSocket `recorder/import_statistics` (`stats_importer.py`), gdy używany jest token wstrzyknięty przez Supervisor zamiast ręcznego `ha_token`. |
| `ingress: true` (bez `ports:`) | Panel Web UI (status sesji, ręczny refetch/relogin, wklejanie cookie) dostępny wyłącznie przez uwierzytelnioną sesję HA. Endpointy `web_server.py` (w tym `/set_cookie`) nie mają własnej autoryzacji, więc nie są wystawiane bezpośrednio na porcie hosta. |

## Energy Dashboard

External statistics są wysyłane przez WebSocket `recorder/import_statistics` do recordera HA:

- `eon_pl:imported_<PPE>` — godzinowy kumulowany pobór z sieci (kWh)
- `eon_pl:exported_<PPE>` — godzinowy kumulowany oddanie do sieci (kWh)
- `eon_pl:imported_cost_<PPE>` — kumulowany koszt poboru (PLN), tylko gdy `price_import_pln_kwh` > 0
- `eon_pl:exported_compensation_<PPE>` — kumulowana wartość energii oddanej (PLN), tylko gdy `value_export_pln_kwh` > 0

Konfiguruj w **Settings → Dashboards → Energy → Electricity grid**.

### Koszt energii

Energy Dashboard **nie liczy** kosztu ze stałej ceny („Użyj stałej ceny”) dla statystyk zewnętrznych (`eon_pl:...`) — robi to tylko dla encji. Dlatego addon może sam zaimportować koszt jako osobne statystyki w PLN: wartość każdej godziny = kWh z tej godziny × cena z opcji.

Podepnij je przy źródle sieci (**Electricity grid**):

- pobór `eon_pl:imported_<PPE>` → opcja „Użyj encji/statystyki śledzącej całkowite koszty” → `eon_pl:imported_cost_<PPE>` (w `energy` prefs: `stat_cost`),
- oddanie `eon_pl:exported_<PPE>` → analogicznie → `eon_pl:exported_compensation_<PPE>` (w `energy` prefs: `stat_compensation`).

#### Jak policzyć `price_import_pln_kwh` z faktury

Zsumuj wszystkie **stawki zmienne za kWh** (netto) z faktury i pomnóż przez VAT **1,23**. Przykład (stawki przykładowe, weź swoje z faktury):

| Składnik (zł/kWh netto) | Stawka |
| --- | --- |
| energia czynna | 0,5000 |
| opłata sieciowa zmienna (dystrybucja) | 0,3000 |
| opłata jakościowa | 0,0300 |
| opłata OZE | 0,0040 |
| opłata kogeneracyjna | 0,0060 |
| **suma netto** | **0,8400** |

`price_import_pln_kwh = 0,8400 × 1,23 = 1,0332` → wpisz `1.0332`.

#### Jak policzyć `value_export_pln_kwh` (net-metering)

W net-meteringu ze współczynnikiem **0,8** każda oddana kWh daje 0,8 kWh do odebrania bez opłat za energię, sieciową zmienną i jakościową, więc:

`value_export_pln_kwh = 0,8 × (energia + sieciowa zmienna + jakościowa) × 1,23`

Dla stawek z tabeli: `0,8 × (0,5000 + 0,3000 + 0,0300) × 1,23 = 0,81672` → wpisz `0.81672`. (Przy współczynniku 0,7 zamień 0,8 na 0,7. W net-billingu wpisz średnią cenę, po której rozliczana jest energia oddana.) To przybliżenie: realny net-metering rozlicza kilowatogodziny w okresie 12 miesięcy, a nie złotówki.

#### Ograniczenia

- **Opłaty stałe nie są wliczone** (handlowa, sieciowa stała, przejściowa, abonamentowa, mocowa ryczałtowa) — statystyki liczą tylko część zależną od kWh.
- **Zmiana ceny działa tylko na nowe godziny.** Sumy już zaimportowane nie są przeliczane — od momentu zmiany kolejne godziny są doliczane po nowej cenie do dotychczasowej sumy. Każda statystyka (kWh i PLN) ma własną kotwicę sumy, więc włączenie ceny nie wpływa na statystyki kWh.
- Po pierwszym włączeniu ceny (zapis opcji = restart addonu) pierwszy fetch pobiera dane od 1 stycznia bieżącego roku, więc koszt zostanie policzony wstecz po bieżącej cenie.
- Wyjątek: aktualizacja do 1.3.2 jednorazowo przelicza od 1 stycznia wszystkie statystyki (kWh i PLN), żeby poprawić godziny dni zmiany czasu (marzec: zdublowana godzina, październik: brakująca). Koszt dni starszych niż 30 dni liczony jest wtedy po bieżącej cenie.
- Taryfy wielostrefowe (G12, G12w…) — jedna cena dla wszystkich godzin, więc wpisz średnią ważoną albo traktuj wynik jako szacunek.

## Troubleshooting

### Login nie działa

Sprawdź **Logs** addona. Najczęstsze przyczyny:

- zły email / hasło → `Login did not redirect to dashboard`
- portal E.ON zmienił layout strony logowania → selectory w `auth.py` wymagają aktualizacji (zgłoś issue)
- captcha-blok → odczekaj 30 min, spróbuj ponownie
- bez CapSolvera → ustaw `manual_cookie_only: true` i wklej `.AspNet.Cookies` albo pełny nagłówek `Cookie` w Web UI

### Brak sieci / DNS po restarcie hosta

Od 1.2.3 addon nie kończy pracy, gdy sieć lub DNS hosta nie są jeszcze gotowe (np. tuż po restarcie): czeka i ponawia zapytanie z rosnącym odstępem (5 s → max 5 min). W logu pojawi się jedno ostrzeżenie z przyczyną, a po odzyskaniu połączenia komunikat `connection restored`. Brak sieci nigdy nie uruchamia logowania przeglądarką — tylko odpowiedź portalu "sesja nieważna" (401/przekierowanie do logowania/brak danych).

### Brak danych godzinowych

E.ON publikuje hourly readings z 24–48 h opóźnieniem. Dane sprzed 3+ dni powinny być zawsze dostępne.

### MQTT timeout

Sprawdź czy addon Mosquitto (lub inny broker) działa. Addon korzysta z poświadczeń wystawianych automatycznie przez Supervisor — żadna ręczna konfiguracja po stronie addona nie jest potrzebna.

## Source

<https://github.com/Fistacho/ha-eon-pl>
