"""Config flow for Georgia Power."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import GaPowerApi, GaPowerAuthError, GaPowerBotDetected, GaPowerError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCHEMA = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str})


class GaPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for credentials and prove they work before creating the entry."""

    VERSION = 1

    async def _validate(self, data: dict[str, Any]) -> tuple[str | None, str | None]:
        """Returns (account_number, error_key)."""
        api = GaPowerApi(
            async_create_clientsession(self.hass), data[CONF_USERNAME], data[CONF_PASSWORD]
        )
        try:
            await api.login()
            await api.resolve_account()
        except GaPowerAuthError:
            return None, "invalid_auth"
        except GaPowerBotDetected:
            return None, "bot_detected"
        except GaPowerError:
            return None, "cannot_connect"
        except Exception:  # noqa: BLE001 - surface as generic, log the detail
            _LOGGER.exception("Unexpected error validating Georgia Power credentials")
            return None, "unknown"
        return api.account, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            account, error = await self._validate(user_input)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(account)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Georgia Power ({account[-4:]})", data=user_input
                )
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            _, error = await self._validate(data)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )
