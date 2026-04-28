"""E.ON Polska integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import EonPolskaClient
from .const import CONF_COOKIE, DOMAIN
from .coordinator import EonPolskaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.info("eon_pl: setting up entry %s", entry.entry_id)
    cookie = entry.data.get(CONF_COOKIE)
    if not cookie:
        _LOGGER.error("eon_pl: no cookie in config entry data")
        return False

    client = EonPolskaClient(cookie)
    coordinator = EonPolskaCoordinator(hass, entry, client)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _LOGGER.info("eon_pl: triggering first refresh...")
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.info(
        "eon_pl: first refresh done, contracts=%d",
        len((coordinator.data or {}).get("contracts", {})),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry whenever the user changes options (e.g. picks a
    # different set of KUs in the options flow) so sensors get rebuilt.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("eon_pl: entry setup complete")
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_unload()
    return unloaded
