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

from .api import EonPolskaClient
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
            _LOGGER.info("Launching Playwright login...")
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
        while True:
            try:
                if self.coordinator:
                    await self.coordinator.keepalive()
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
        cookie = await self.ensure_cookie()
        self.client = EonPolskaClient(cookie)
        self.coordinator = EonCoordinator(
            client=self.client,
            selected_kus=self.rt.options.selected_kus,
            relogin=self.relogin,
        )
        self.stats = StatsImporter(self.rt.ha_url, self.rt.ha_token, self.state_store)

        # Connect MQTT (only if discovery enabled and broker reachable)
        if self.rt.options.mqtt_discovery:
            try:
                cfg = await MqttConfig.from_supervisor()
                self.mqtt = await MqttPublisher(cfg).__aenter__()
                _LOGGER.info("MQTT connected: %s:%s", cfg.host, cfg.port)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("MQTT unavailable (%s) — skipping discovery", exc)

        # First fetch
        try:
            await self.fetch_once()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Initial fetch failed: %s", exc)

        # Web UI
        app = build_app(
            coordinator=self.coordinator,
            state_store=self.state_store,
            cookie_store=self.cookie_store,
            trigger_login=self._on_login_request,
            trigger_fetch=self.fetch_once,
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


def main() -> None:
    rt = Runtime.from_env()
    configure_logging(rt.options.log_level)
    asyncio.run(App(rt).run())


if __name__ == "__main__":
    main()
