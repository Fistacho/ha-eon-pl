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

_COOKIE_SCHEMA = vol.Schema({vol.Required(CONF_COOKIE): str})


def _active_ku_options(ph: dict[str, Any]) -> list[selector.SelectOptionDict]:
    """Build multi-select options from a GetPHList response."""
    out: list[selector.SelectOptionDict] = []
    for partner in ph.get("Partners") or []:
        for ku in partner.get("ContractAccounts") or []:
            if not ku.get("IsActive", True):
                continue
            ku_id = str(ku.get("Id"))
            label = f"{ku.get('KuDisplayName') or ku_id} ({ku_id})"
            out.append(selector.SelectOptionDict(value=ku_id, label=label))
    return out


class EonPolskaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Two-step config: paste .AspNet.Cookies, then pick which KUs to track."""

    VERSION = 1

    def __init__(self) -> None:
        self._cookie: str | None = None
        self._ku_options: list[selector.SelectOptionDict] = []

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
            ph: dict[str, Any] | None = None
            try:
                if await client.validate_session():
                    ph = await client.get_ph_list()
            except Exception:
                ph = None
            finally:
                await client.aclose()

            if ph is not None:
                await self.async_set_unique_id(f"eon_pl_{cookie[:20]}")
                self._abort_if_unique_id_configured()

                self._cookie = cookie
                self._ku_options = _active_ku_options(ph)

                # 0 active KUs is unusual but valid — finish without filtering.
                # 1 active KU — no point asking, just create the entry.
                if len(self._ku_options) <= 1:
                    return self.async_create_entry(
                        title="E.ON Polska",
                        data={CONF_COOKIE: cookie},
                        options={CONF_SELECTED_KUS: []},
                    )
                return await self.async_step_select_kus()

            errors["base"] = "invalid_cookie"

        return self.async_show_form(
            step_id="user",
            data_schema=_COOKIE_SCHEMA,
            errors=errors,
            description_placeholders={"portal_url": "https://eon.pl/mojeon"},
        )

    async def async_step_select_kus(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="E.ON Polska",
                data={CONF_COOKIE: self._cookie},
                options={CONF_SELECTED_KUS: user_input.get(CONF_SELECTED_KUS, [])},
            )

        default_all = [opt["value"] for opt in self._ku_options]
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SELECTED_KUS, default=default_all
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._ku_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="select_kus", data_schema=schema)

    async def async_step_reauth(self, entry_data: dict) -> FlowResult:
        return await self.async_step_user()


class EonPolskaOptionsFlow(config_entries.OptionsFlow):
    """Let the user change which KUs to track after the entry is set up."""

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
            ku_options = _active_ku_options(ph)
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
