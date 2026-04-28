"""Config flow for E.ON Polska — manual cookie auth + KU selection."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import EonPolskaClient
from .const import CONF_COOKIE, CONF_SELECTED_KUS, DOMAIN

_SCHEMA = vol.Schema({vol.Required(CONF_COOKIE): str})


class EonPolskaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """One-step config: user pastes .AspNet.Cookies value from browser."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EonPolskaOptionsFlow:
        return EonPolskaOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            cookie = user_input[CONF_COOKIE].strip().strip('"\'')
            client = EonPolskaClient(cookie)
            try:
                valid = await client.validate_session()
            finally:
                await client.aclose()

            if valid:
                await self.async_set_unique_id(f"eon_pl_{cookie[:20]}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="E.ON Polska",
                    data={CONF_COOKIE: cookie},
                )
            errors["base"] = "invalid_cookie"

        return self.async_show_form(
            step_id="user",
            data_schema=_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict) -> FlowResult:
        return await self.async_step_user()


class EonPolskaOptionsFlow(config_entries.OptionsFlow):
    """Let the user pick which KU (numery konta umowy) to track.

    Useful when an account has more than one active contract — by default
    every active KU produces sensors; the options flow lets the user trim
    that down.
    """

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        cookie = self._entry.data[CONF_COOKIE]
        client = EonPolskaClient(cookie)
        ku_options: list[selector.SelectOptionDict] = []
        try:
            ph = await client.get_ph_list()
            for partner in ph.get("Partners") or []:
                for ku in partner.get("ContractAccounts") or []:
                    if not ku.get("IsActive", True):
                        continue
                    ku_id = str(ku.get("Id"))
                    label = (
                        f"{ku.get('KuDisplayName') or ku_id} ({ku_id})"
                    )
                    ku_options.append(
                        selector.SelectOptionDict(value=ku_id, label=label)
                    )
        except Exception:
            ku_options = []
        finally:
            await client.aclose()

        current = self._entry.options.get(CONF_SELECTED_KUS, [])

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SELECTED_KUS, default=current
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=ku_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
