"""Adding a camera: its address and, for a Tapo, the TP-Link account password.

The password is checked against the camera before anything is saved - the
real login, stopping short of opening a talk session - so a typo or the
missing Tapo setting is found here rather than at bedtime.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import CONF_KIND, CONF_TAPO_CLOUD_PASSWORD, DOMAIN
from .talk import tapo
from .talk.errors import Failure
from .talk.transports import KIND_TAPO

_ERRORS = {
    Failure.UNREACHABLE: "cannot_connect",
    Failure.NEEDS_THIRD_PARTY_ACCESS: "third_party",
    Failure.REJECTED: "invalid_auth",
}


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_TAPO_CLOUD_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


class CameraNoiseConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            looks_like_tapo = await self.hass.async_add_executor_job(tapo.looks_like_tapo, host)
            if not looks_like_tapo:
                errors["base"] = "not_tapo"
            else:
                error = await self.hass.async_add_executor_job(
                    tapo.check_cloud_password, host, user_input[CONF_TAPO_CLOUD_PASSWORD]
                )
                if error is None:
                    return self.async_create_entry(
                        title=user_input[CONF_NAME].strip() or host,
                        data={
                            CONF_KIND: KIND_TAPO,
                            CONF_HOST: host,
                            CONF_TAPO_CLOUD_PASSWORD: user_input[CONF_TAPO_CLOUD_PASSWORD],
                        },
                    )
                errors["base"] = _ERRORS.get(error.failure, "unknown")

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
        )
