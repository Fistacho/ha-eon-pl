"""Addon entrypoint.

Lifecycle:
  1. Load options + persisted state.
  2. Resurrect saved cookie OR Playwright login.
  3. Connect to MQTT, publish discovery for known contracts (after first fetch).
  4. Periodic loop:
        - keepalive every 5 min
        - fetch every scan_interval_hours
        - re-login every cookie_refresh_hours (or on auth failure)
  5. Web UI on ingress port 8099 for status + manual triggers.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

from aiohttp import web

from .api import EonAuthError, EonPolskaClient
from .auth import LoginError, login_with_retry
from .config import Runtime, configure_logging
from .const import KEEPALIVE_INTERVAL_MINUTES
from .cookie_store import CookieStore
from .coordinator import EonCoordinator
from .mqtt_publisher import MqttConfig, MqttPublisher
from .state_store import StateStore
from .stats_importer import StatsImporter
from .web_server import build_app

_LOGGER = logging.getLogger(__name__)


class App:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self.cookie_store = CookieStore(runtime.data_dir)
        self.state_store = StateStore(runtime.data_dir)
        self.client: EonPolskaClient | None = None
        self.coordinator: EonCoordinator | None = None
        self.mqtt: MqttPublisher | None = None
        self.stats: StatsImporter | None = None
        self._fetch_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()

    # ---------------- bootstrap ----------------

    async def ensure_cookie(self) -> str:
        """Return a valid cookie. Try persisted one first; fallback to Playwright."""
        cookie, _ = self.cookie_store.load()
        if cookie:
            client = EonPolskaClient(cookie)
            try:
                if await client.validate_session():
                    _LOGGER.info("Resuming with persisted cookie")
                    await client.aclose()
                    return cookie
            finally:
                await client.aclose()
            _LOGGER.info("Persisted cookie is dead, will Playwright-login")
        return await self.relogin()

    async def relogin(self) -> str:
        """Always launches Playwright. Saves the new cookie."""
        async with self._login_lock:
            _LOGGER.info("Launching Selenium login...")
            cookie = await login_with_retry(
                self.rt.options.email, self.rt.options.password
            )
            self.cookie_store.save(cookie)
            self.state_store.record_login(datetime.now(timezone.utc))
            return cookie

    # ---------------- main loops ----------------

    async def fetch_once(self) -> None:
        async with self._fetch_lock:
            assert self.coordinator is not None
            ok = False
            try:
                await self.coordinator.fetch()
                ok = bool(self.coordinator.contracts)
                if ok and self.mqtt is not None:
                    await self.mqtt.publish_discovery(self.coordinator.contracts)
                    await self.mqtt.publish_state(
                        self.coordinator.contracts, self.coordinator.last_hour
                    )
                if ok and self.stats is not None:
                    await self.stats.import_hourly(self.coordinator.fresh_rows)
            finally:
                self.state_store.record_fetch(datetime.now(timezone.utc), ok)

    async def loop_keepalive(self) -> None:
        import time as _time
        _auth_fails = 0
        _relogin_backoff = 0  # seconds; 0 = not yet failed
        _next_relogin_at = 0.0
        while True:
            try:
                if self.coordinator:
                    await self.coordinator.keepalive()
                    if _auth_fails:
                        _LOGGER.info("keepalive: session restored")
                    _auth_fails = 0
                    _relogin_backoff = 0
                    _next_relogin_at = 0.0
            except EonAuthError as exc:
                _auth_fails += 1
                _LOGGER.warning("keepalive: session expired (%s) — fail #%d", exc, _auth_fails)
                if _auth_fails >= 3 and _time.monotonic() >= _next_relogin_at:
                    _LOGGER.warning(
                        "keepalive: triggering re-login (backoff was %.0f min)",
                        _relogin_backoff / 60,
                    )
                    try:
                        cookie = await self.relogin()
                        if self.client is not None:
                            self.client.set_cookie(cookie)
                        _auth_fails = 0
                        _relogin_backoff = 0
                        _next_relogin_at = 0.0
                        await self.fetch_once()
                    except Exception as rel_exc:  # noqa: BLE001
                        # Exponential backoff: 15 min → 30 → 60 → 120 → 240 max
                        _relogin_backoff = min(max(_relogin_backoff * 2, 15 * 60), 4 * 3600)
                        _next_relogin_at = _time.monotonic() + _relogin_backoff
                        _LOGGER.error(
                            "keepalive: re-login failed: %s. Next attempt in %.0f min",
                            rel_exc, _relogin_backoff / 60,
                        )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("keepalive error: %s", exc)
            await asyncio.sleep(KEEPALIVE_INTERVAL_MINUTES * 60)

    async def loop_fetch(self) -> None:
        interval = self.rt.options.scan_interval_hours * 3600
        while True:
            await asyncio.sleep(interval)
            try:
                await self.fetch_once()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Periodic fetch failed: %s", exc)

    async def loop_relogin(self) -> None:
        interval = self.rt.options.cookie_refresh_hours * 3600
        while True:
            await asyncio.sleep(interval)
            try:
                cookie = await self.relogin()
                if self.client is not None:
                    self.client.set_cookie(cookie)
                _LOGGER.info("Periodic re-login OK")
            except LoginError as exc:
                _LOGGER.error("Periodic re-login failed: %s", exc)

    # ---------------- run ----------------

    async def run(self) -> None:
        _LOGGER.info("E.ON Polska addon starting...")
        cookie = ""
        try:
            cookie = await self.ensure_cookie()
        except LoginError as exc:
            _LOGGER.error(
                "Initial login failed: %s — starting in degraded mode, "
                "keepalive will retry automatically.",
                exc,
            )
        self.client = EonPolskaClient(cookie)
        self.coordinator = EonCoordinator(
            client=self.client,
            selected_kus=self.rt.options.selected_kus,
            relogin=self.relogin,
        )
        # Statistics importer — prefer user-provided long-lived token, fall
        # back to Supervisor token if it was injected.
        if self.rt.options.ha_token:
            stats_url = "http://homeassistant:8123"
            stats_token = self.rt.options.ha_token
            _LOGGER.info("Stats importer using user-provided long-lived token")
        else:
            stats_url = self.rt.ha_url
            stats_token = self.rt.ha_token
            if not stats_token:
                _LOGGER.warning(
                    "Stats importer has no token — set ha_token in addon options "
                    "(Profile → Security → Long-Lived Access Tokens)"
                )
        self.stats = StatsImporter(stats_url, stats_token, self.state_store)

        # Connect MQTT (only if discovery enabled and broker reachable).
        # Resolution order:
        #   1. addon options (mqtt_host) — explicit user choice
        #   2. Supervisor-injected env vars (MQTT_HOST etc.) — works without
        #      SUPERVISOR_TOKEN, so it's the "automatic" path
        #   3. Supervisor REST /services/mqtt — needs SUPERVISOR_TOKEN
        if self.rt.options.mqtt_discovery:
            cfg: MqttConfig | None = None
            if self.rt.options.mqtt_host:
                cfg = MqttConfig.from_options(
                    self.rt.options.mqtt_host,
                    self.rt.options.mqtt_port,
                    self.rt.options.mqtt_user,
                    self.rt.options.mqtt_password,
                )
                _LOGGER.info("MQTT using addon options (host=%s port=%s)",
                             cfg.host, cfg.port)
            else:
                cfg = MqttConfig.from_env()
                if cfg is not None:
                    _LOGGER.info("MQTT using Supervisor env vars (host=%s)", cfg.host)
                else:
                    try:
                        cfg = await MqttConfig.from_supervisor()
                        _LOGGER.info("MQTT using Supervisor service (host=%s)", cfg.host)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning(
                            "MQTT unavailable (%s) — set mqtt_host/user/password "
                            "in addon options to use a broker manually", exc,
                        )

            if cfg is not None:
                try:
                    self.mqtt = await MqttPublisher(cfg).__aenter__()
                    _LOGGER.info("MQTT connected: %s:%s", cfg.host, cfg.port)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("MQTT connection failed: %s", exc)
                    self.mqtt = None

        # First fetch — skip if no valid session yet (degraded mode)
        if cookie:
            try:
                await self.fetch_once()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Initial fetch failed: %s", exc)
        else:
            _LOGGER.warning("Skipping initial fetch — no valid session, waiting for keepalive re-login")

        # Web UI
        app = build_app(
            coordinator=self.coordinator,
            state_store=self.state_store,
            cookie_store=self.cookie_store,
            trigger_login=self._on_login_request,
            trigger_fetch=self.fetch_once,
            set_cookie=self._on_set_cookie,
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8099)
        await site.start()
        _LOGGER.info("Web UI listening on 0.0.0.0:8099")

        # Background loops
        asyncio.create_task(self.loop_keepalive())
        asyncio.create_task(self.loop_fetch())
        asyncio.create_task(self.loop_relogin())

        # Block forever
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()
        _LOGGER.info("Shutting down")
        await runner.cleanup()
        if self.mqtt is not None:
            await self.mqtt.publish_offline(self.coordinator.contracts if self.coordinator else {})
            await self.mqtt.__aexit__(None, None, None)
        if self.client is not None:
            await self.client.aclose()

    async def _on_login_request(self) -> None:
        cookie = await self.relogin()
        if self.client is not None:
            self.client.set_cookie(cookie)

    async def _on_set_cookie(self, cookie_value: str) -> None:
        """Validate and apply a cookie pasted manually by the user."""
        client = EonPolskaClient(cookie_value)
        try:
            valid = await client.validate_session()
        finally:
            await client.aclose()
        if not valid:
            raise ValueError(
                "Sesja nieważna — ciasteczko wygasło lub jest nieprawidłowe. "
                "Zaloguj się jeszcze raz i skopiuj .AspNet.Cookies od nowa."
            )
        self.cookie_store.save(cookie_value)
        self.state_store.record_login(datetime.now(timezone.utc))
        if self.client is not None:
            self.client.set_cookie(cookie_value)
        _LOGGER.info("Manual cookie saved and validated — starting fetch")
        await self.fetch_once()


def main() -> None:
    rt = Runtime.from_env()
    configure_logging(rt.options.log_level)
    asyncio.run(App(rt).run())


if __name__ == "__main__":
    main()
