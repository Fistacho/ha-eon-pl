"""MQTT publisher with Home Assistant auto-discovery."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiomqtt
import httpx

from .const import DOMAIN, MQTT_DISCOVERY_PREFIX, MQTT_STATE_PREFIX

_LOGGER = logging.getLogger(__name__)


# ---------- Supervisor MQTT credentials ----------

@dataclass
class MqttConfig:
    host: str
    port: int
    username: str | None
    password: str | None

    @classmethod
    def from_env(cls) -> "MqttConfig | None":
        """Try Supervisor's auto-injected MQTT env vars (no token required).

        For addons with `services: [mqtt:want]` Supervisor sets MQTT_HOST,
        MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD before launching the
        container — works even with Protection mode ON.
        """
        host = os.environ.get("MQTT_HOST", "").strip()
        if not host:
            return None
        return cls(
            host=host,
            port=int(os.environ.get("MQTT_PORT", "1883") or 1883),
            username=os.environ.get("MQTT_USERNAME") or None,
            password=os.environ.get("MQTT_PASSWORD") or None,
        )

    @classmethod
    async def from_supervisor(cls) -> "MqttConfig":
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN not set — cannot read MQTT config")
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            d = r.json().get("data", {})
        return cls(
            host=d.get("host", "core-mosquitto"),
            port=int(d.get("port", 1883)),
            username=d.get("username") or None,
            password=d.get("password") or None,
        )

    @classmethod
    def from_options(cls, host: str, port: int, user: str, password: str) -> "MqttConfig":
        """Build config from addon options (used when Supervisor token is unavailable)."""
        return cls(
            host=host,
            port=int(port or 1883),
            username=user or None,
            password=password or None,
        )


# ---------- Discovery + state ----------

# (key_in_state, name_pl, unit, device_class, state_class, icon)
_SENSORS: list[tuple[str, str, str, str, str, str]] = [
    ("consumption_current_period",
     "Zużycie (bieżący okres rozliczeniowy)", "kWh", "energy", "total_increasing",
     "mdi:lightning-bolt"),
    ("imported_year",
     "Pobrana (bieżący rok)", "kWh", "energy", "total_increasing",
     "mdi:transmission-tower-import"),
    ("exported_year",
     "Wprowadzona (bieżący rok)", "kWh", "energy", "total_increasing",
     "mdi:transmission-tower-export"),
    ("balance_year",
     "Bilans (bieżący rok)", "kWh", "energy", "total",
     "mdi:scale-balance"),
    ("last_hour_imported",
     "Pobrana (ostatnia godzina)", "kWh", "energy", "total",
     "mdi:transmission-tower-import"),
    ("last_hour_exported",
     "Wprowadzona (ostatnia godzina)", "kWh", "energy", "total",
     "mdi:transmission-tower-export"),
    ("last_hour_balance",
     "Bilans (ostatnia godzina)", "kWh", "energy", "total",
     "mdi:scale-balance"),
]


def _device_payload(key: str, ku: dict[str, Any], ppe: dict[str, Any]) -> dict[str, Any]:
    ku_name = ku.get("KuDisplayName") or str(ku.get("Id"))
    ppe_name = ppe.get("PPEDisplayName") or str(ppe.get("Id"))
    return {
        "identifiers": [f"{DOMAIN}_{key}"],
        "name": f"E.ON {ku_name} ({ppe_name})",
        "manufacturer": "E.ON Polska",
        "model": (ku.get("ProductIcon") or "energy").replace("icon-", ""),
        "configuration_url": "https://eon.pl/mojeon",
    }


def _state_topic(key: str) -> str:
    return f"{MQTT_STATE_PREFIX}/{key}/state"


def _availability_topic(key: str) -> str:
    return f"{MQTT_STATE_PREFIX}/{key}/availability"


def _discovery_topic(key: str, sensor_key: str) -> str:
    return f"{MQTT_DISCOVERY_PREFIX}/sensor/{DOMAIN}_{key}/{sensor_key}/config"


class MqttPublisher:
    def __init__(self, cfg: MqttConfig) -> None:
        self._cfg = cfg
        self._client: aiomqtt.Client | None = None

    async def __aenter__(self) -> "MqttPublisher":
        self._client = aiomqtt.Client(
            hostname=self._cfg.host,
            port=self._cfg.port,
            username=self._cfg.username,
            password=self._cfg.password,
            identifier=f"{DOMAIN}_addon",
        )
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def publish_discovery(self, contracts: dict[str, dict[str, Any]]) -> None:
        assert self._client is not None
        for key, c in contracts.items():
            device = _device_payload(key, c["ku"], c["ppe"])
            for sensor_key, name, unit, dev_class, state_class, icon in _SENSORS:
                payload = {
                    "name": name,
                    "unique_id": f"{DOMAIN}_{key}_{sensor_key}",
                    "object_id": f"eon_{key}_{sensor_key}",
                    "state_topic": _state_topic(key),
                    "availability_topic": _availability_topic(key),
                    "value_template": (
                        "{{ value_json." + sensor_key + " }}"
                    ),
                    "json_attributes_topic": _state_topic(key),
                    "unit_of_measurement": unit,
                    "device_class": dev_class,
                    "state_class": state_class,
                    "icon": icon,
                    "device": device,
                }
                await self._client.publish(
                    _discovery_topic(key, sensor_key),
                    json.dumps(payload, ensure_ascii=False),
                    retain=True,
                )
        _LOGGER.info("MQTT discovery published for %d contract(s)", len(contracts))

    async def publish_state(
        self,
        contracts: dict[str, dict[str, Any]],
        last_hour: dict[str, dict[str, Any]],
    ) -> None:
        assert self._client is not None
        for key, c in contracts.items():
            payload = self._state_payload(c, last_hour.get(key))
            await self._client.publish(
                _state_topic(key),
                json.dumps(payload, ensure_ascii=False),
                retain=True,
            )
            await self._client.publish(
                _availability_topic(key), "online", retain=True
            )
        _LOGGER.debug("MQTT state published for %d contract(s)", len(contracts))

    async def publish_offline(self, contracts: dict[str, dict[str, Any]]) -> None:
        if self._client is None:
            return
        for key in contracts:
            await self._client.publish(_availability_topic(key), "offline", retain=True)

    @staticmethod
    def _state_payload(
        contract: dict[str, Any], last_hour: dict[str, Any] | None
    ) -> dict[str, Any]:
        billing = contract.get("billing")
        oze = contract.get("oze")
        out: dict[str, Any] = {
            "consumption_current_period": _billing_consumption_current_period(billing),
            "imported_year": _current_year_total(oze, "P"),
            "exported_year": _current_year_total(oze, "O"),
            "balance_year": _current_year_total(oze, "B"),
            "last_hour_imported": (last_hour or {}).get("imported_kwh"),
            "last_hour_exported": (last_hour or {}).get("exported_kwh"),
            "last_hour_balance": (last_hour or {}).get("balance_kwh"),
            "last_hour_timestamp": _ts(last_hour),
        }
        return out


# ---------- helpers ----------

def _ts(d: dict[str, Any] | None) -> str | None:
    if not d:
        return None
    t = d.get("timestamp")
    return t.isoformat() if isinstance(t, datetime) else None


def _pl_to_float(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _current_year_total(oze: dict | None, key: str) -> float | None:
    if not isinstance(oze, dict):
        return None
    year = str(datetime.now().year)
    for entry in oze.get("Results", []) or []:
        if str(entry.get("Year")) != year:
            continue
        total = 0.0
        any_data = False
        for row in entry.get("Data", []) or []:
            v = _pl_to_float(row.get(key))
            if v is not None:
                total += v
                any_data = True
        return total if any_data else None
    return None


def _billing_consumption_current_period(billing: dict | None) -> float | None:
    if not isinstance(billing, dict):
        return None
    chart = billing.get("ChartsData") or {}
    cats = chart.get("Categories") or []
    results = chart.get("Results") or []
    if not cats or not results:
        return None
    last_cat_idx = len(cats) - 1
    for r in results:
        if r.get("X") == last_cat_idx and r.get("LegendId") == 0:
            return _pl_to_float(r.get("Y"))
    return None
