"""Adding a camera: any camera with an ONVIF back channel, or a Tapo.

Two forms behind a menu, because the two ask for different things: the
standard way needs the camera's RTSP stream address and its login, and
Tapo needs only its IP address and the TP-Link account password.

Either way the camera is asked before anything is saved - a DESCRIBE for
the back channel, or the real Tapo login - so a wrong address, a refused
password or a camera with no usable speaker is found here rather than at
bedtime. Neither check plays a sound.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import CONF_KIND, CONF_STREAM_URL, CONF_TAPO_CLOUD_PASSWORD, DOMAIN
from .talk import onvif, tapo
from .talk.errors import Failure
from .talk.rtsp import credentials_in, without_credentials
from .talk.transports import KIND_ONVIF, KIND_TAPO

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

_TAPO_ERRORS = {
    Failure.UNREACHABLE: "cannot_connect",
    Failure.NEEDS_THIRD_PARTY_ACCESS: "third_party",
    Failure.REJECTED: "invalid_auth",
}

_ONVIF_ERRORS = {
    Failure.UNREACHABLE: "cannot_connect",
    Failure.REJECTED: "invalid_auth",
    Failure.UNSUPPORTED: "no_backchannel",
}


class CameraNoiseConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["onvif", "tapo"])

    async def async_step_onvif(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults = user_input or {}
        if user_input is not None:
            url = user_input[CONF_STREAM_URL].strip()
            parts = urllib.parse.urlsplit(url)
            if parts.scheme.lower() != "rtsp" or not parts.hostname:
                errors[CONF_STREAM_URL] = "invalid_url"
            else:
                # A login pasted into the address counts, as cameras' own
                # documentation writes it that way; it is stored apart from
                # the address so the password never shows in a URL.
                embedded_user, embedded_password = credentials_in(url)
                username = user_input.get(CONF_USERNAME) or embedded_user or ""
                password = user_input.get(CONF_PASSWORD) or embedded_password or ""
                stream_url = without_credentials(url)

                await self.async_set_unique_id(parts.hostname)
                self._abort_if_unique_id_configured()

                error = await self.hass.async_add_executor_job(onvif.probe, stream_url, username, password)
                if error is None:
                    return self.async_create_entry(
                        title=user_input[CONF_NAME].strip() or parts.hostname,
                        data={
                            CONF_KIND: KIND_ONVIF,
                            CONF_STREAM_URL: stream_url,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                        },
                    )
                if error.failure is Failure.UNSUPPORTED and await self.hass.async_add_executor_job(
                    tapo.looks_like_tapo, parts.hostname
                ):
                    # Tapo answers the back-channel request without having
                    # one; its speaker is on its own channel instead.
                    errors["base"] = "is_tapo"
                else:
                    errors["base"] = _ONVIF_ERRORS.get(error.failure, "unknown")

        return self.async_show_form(
            step_id="onvif",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
                    vol.Required(CONF_STREAM_URL, default=defaults.get(CONF_STREAM_URL, "")): str,
                    vol.Optional(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
                    vol.Optional(CONF_PASSWORD): _PASSWORD,
                }
            ),
            errors=errors,
        )

    async def async_step_tapo(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults = user_input or {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            if not await self.hass.async_add_executor_job(tapo.looks_like_tapo, host):
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
                errors["base"] = _TAPO_ERRORS.get(error.failure, "unknown")

        return self.async_show_form(
            step_id="tapo",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
                    vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                    vol.Required(CONF_TAPO_CLOUD_PASSWORD): _PASSWORD,
                }
            ),
            errors=errors,
        )
