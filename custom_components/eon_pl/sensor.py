"""E.ON Polska sensors."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EonPolskaCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class EonSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Any], Any]
    available_fn: Callable[[Any], bool] = lambda d: d is not None


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
    """Sum given key (P/O/B) across the months of the current year from GetOzeAgrData."""
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
    """Pull current billing-period consumption from GetBillingData chart data."""
    if not isinstance(billing, dict):
        return None
    chart = billing.get("ChartsData") or {}
    cats = chart.get("Categories") or []
    results = chart.get("Results") or []
    if not cats or not results:
        return None
    last_cat_idx = len(cats) - 1
    # LegendId 0 = energia pobrana
    for r in results:
        if r.get("X") == last_cat_idx and r.get("LegendId") == 0:
            return _pl_to_float(r.get("Y"))
    return None


BILLING_SENSORS: tuple[EonSensorDescription, ...] = (
    EonSensorDescription(
        key="consumption_current_period",
        name="Zużycie (bieżący okres rozliczeniowy)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:lightning-bolt",
        value_fn=_billing_consumption_current_period,
        available_fn=lambda d: _billing_consumption_current_period(d) is not None,
    ),
)


OZE_SENSORS: tuple[EonSensorDescription, ...] = (
    EonSensorDescription(
        key="oze_imported_year",
        name="Pobrana (bieżący rok)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:transmission-tower-import",
        value_fn=lambda d: _current_year_total(d, "P"),
        available_fn=lambda d: _current_year_total(d, "P") is not None,
    ),
    EonSensorDescription(
        key="oze_exported_year",
        name="Wprowadzona (bieżący rok)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:transmission-tower-export",
        value_fn=lambda d: _current_year_total(d, "O"),
        available_fn=lambda d: _current_year_total(d, "O") is not None,
    ),
    EonSensorDescription(
        key="oze_balance_year",
        name="Bilans (bieżący rok)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:scale-balance",
        value_fn=lambda d: _current_year_total(d, "B"),
        available_fn=lambda d: _current_year_total(d, "B") is not None,
    ),
)


HOURLY_SENSORS: tuple[EonSensorDescription, ...] = (
    EonSensorDescription(
        key="last_hour_imported",
        name="Pobrana (ostatnia godzina)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:transmission-tower-import",
        value_fn=lambda d: d.get("imported_kwh") if isinstance(d, dict) else None,
        available_fn=lambda d: isinstance(d, dict),
    ),
    EonSensorDescription(
        key="last_hour_exported",
        name="Wprowadzona (ostatnia godzina)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:transmission-tower-export",
        value_fn=lambda d: d.get("exported_kwh") if isinstance(d, dict) else None,
        available_fn=lambda d: isinstance(d, dict),
    ),
    EonSensorDescription(
        key="last_hour_balance",
        name="Bilans (ostatnia godzina)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:scale-balance",
        value_fn=lambda d: d.get("balance_kwh") if isinstance(d, dict) else None,
        available_fn=lambda d: isinstance(d, dict),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EonPolskaCoordinator = hass.data[DOMAIN][entry.entry_id]
    contracts = (coordinator.data or {}).get("contracts", {})
    _LOGGER.info("eon_pl sensor.async_setup_entry: %d contract(s)", len(contracts))

    entities: list[EonSensorEntity] = []

    for contract_key, contract in contracts.items():
        ku = contract["ku"]
        ppe = contract["ppe"]
        if not ku.get("IsActive", True):
            _LOGGER.info("eon_pl: skip inactive KU %s", ku.get("Id"))
            continue
        ku_id = ku["Id"]
        ppe_id = ppe["Id"]
        ku_name = ku.get("KuDisplayName") or ku_id
        ppe_name = ppe.get("PPEDisplayName") or ppe_id

        device_info = {
            "identifiers": {(DOMAIN, f"{ku_id}_{ppe_id}")},
            "name": f"E.ON {ku_name} ({ppe_name})",
            "manufacturer": "E.ON Polska",
            "model": (ku.get("ProductIcon") or "energy").replace("icon-", ""),
            "entry_type": "service",
        }

        for desc in BILLING_SENSORS:
            entities.append(EonSensorEntity(
                coordinator=coordinator,
                description=desc,
                contract_key=contract_key,
                data_source="billing",
                device_info=device_info,
                ku_id=ku_id,
                ppe_id=ppe_id,
            ))

        if coordinator.data.get("ph", {}).get("HasOze"):
            for desc in OZE_SENSORS:
                entities.append(EonSensorEntity(
                    coordinator=coordinator,
                    description=desc,
                    contract_key=contract_key,
                    data_source="oze",
                    device_info=device_info,
                    ku_id=ku_id,
                    ppe_id=ppe_id,
                ))

        for desc in HOURLY_SENSORS:
            entities.append(EonSensorEntity(
                coordinator=coordinator,
                description=desc,
                contract_key=contract_key,
                data_source="last_hour",
                device_info=device_info,
                ku_id=ku_id,
                ppe_id=ppe_id,
            ))

    async_add_entities(entities)


class EonSensorEntity(CoordinatorEntity[EonPolskaCoordinator], SensorEntity):
    """A single E.ON sensor."""

    entity_description: EonSensorDescription

    def __init__(
        self,
        coordinator: EonPolskaCoordinator,
        description: EonSensorDescription,
        contract_key: str,
        data_source: str,
        device_info: dict,
        ku_id: str,
        ppe_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._contract_key = contract_key
        self._data_source = data_source  # 'billing' | 'oze' | 'last_hour'
        self._ku_id = ku_id
        self._ppe_id = ppe_id
        self._attr_unique_id = f"eon_pl_{ku_id}_{ppe_id}_{description.key}"
        self._attr_device_info = device_info

    @property
    def _source_data(self) -> Any:
        if self._data_source == "last_hour":
            return self.coordinator.last_hour.get(self._contract_key)
        contracts = self.coordinator.data.get("contracts", {})
        return (contracts.get(self._contract_key) or {}).get(self._data_source)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self._source_data)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.entity_description.available_fn(self._source_data)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._data_source != "last_hour":
            return None
        d = self._source_data
        if not isinstance(d, dict):
            return None
        ts = d.get("timestamp")
        return {"timestamp": ts.isoformat() if isinstance(ts, datetime) else None}
