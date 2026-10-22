"""Adding a camera: its address first, then only what that kind of camera needs.

The first step asks for a name and an address - an IP, or an rtsp:// URL
for anyone who has one. The camera is then asked, with no password, whether
it is a Tapo (port 8800 announces itself), and the second step follows
from the answer:

 - A Tapo: the TP-Link account password, checked with the real login.
 - Anything else: the camera's own username and password. Given only an IP,
   the camera is asked over ONVIF for its stream addresses, so nobody has
   to know the path; then each is asked for a back channel, largest first,
   and the first that has one is kept.

Nothing is saved until the camera has said yes, and no step plays a sound.
Reconfigure goes through the same checks for a camera already added - a
changed password, or an IP the router moved - and so does reauthentication,
which Home Assistant offers when the camera starts refusing its login.
"""

from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import CONF_KIND, CONF_STREAM_URL, CONF_TAPO_CLOUD_PASSWORD, DOMAIN
from .talk import onvif, onvif_device, tapo
from .talk.errors import Failure, TalkbackError
from .talk.rtsp import credentials_in, without_credentials
from .talk.transports import KIND_ONVIF, KIND_TAPO

_LOGGER = logging.getLogger(__name__)

CONF_ADDRESS = "address"

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

_TAPO_ERRORS = {
    Failure.UNREACHABLE: "cannot_connect",
    Failure.NEEDS_THIRD_PARTY_ACCESS: "third_party",
    Failure.REJECTED: "third_party",
}

_ONVIF_ERRORS = {
    Failure.UNREACHABLE: "cannot_connect",
    Failure.REJECTED: "invalid_auth",
    Failure.UNSUPPORTED: "no_backchannel",
}


def _parse_address(address: str) -> tuple[str | None, str | None]:
    """(host, stream URL) from what was typed: an IP or name, or an rtsp:// URL."""
    address = address.strip()
    if "://" in address:
        parts = urllib.parse.urlsplit(address)
        if parts.scheme.lower() != "rtsp" or not parts.hostname:
            return None, None
        return parts.hostname, address
    if not address or any(c in address for c in " /?#@"):
        return None, None
    return address, None


class CameraNoiseConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._name = ""
        self._host = ""
        # The stream URL as given, credentials and all, when one was typed.
        self._typed_url: str | None = None
        # Set once the camera could not be asked for its streams, so the
        # login step asks for the address instead.
        self._ask_for_url = False

    # --- Step 1: name and address -------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, url = _parse_address(user_input[CONF_ADDRESS])
            if host is None:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                self._name = user_input[CONF_NAME].strip() or host
                self._host = host
                self._typed_url = url
                if await self.hass.async_add_executor_job(tapo.looks_like_tapo, host):
                    return await self.async_step_tapo()
                return await self.async_step_login()

        defaults = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
                    vol.Required(CONF_ADDRESS, default=defaults.get(CONF_ADDRESS, "")): str,
                }
            ),
            errors=errors,
        )

    # --- Step 2, a Tapo ------------------------------------------------------

    async def async_step_tapo(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_TAPO_CLOUD_PASSWORD]
            error = await self.hass.async_add_executor_job(tapo.check_cloud_password, self._host, password)
            if error is None:
                return self.async_create_entry(
                    title=self._name,
                    data={CONF_KIND: KIND_TAPO, CONF_HOST: self._host, CONF_TAPO_CLOUD_PASSWORD: password},
                )
            errors["base"] = _TAPO_ERRORS.get(error.failure, "unknown")

        return self.async_show_form(
            step_id="tapo",
            data_schema=vol.Schema({vol.Required(CONF_TAPO_CLOUD_PASSWORD): _PASSWORD}),
            description_placeholders={"name": self._name, "host": self._host},
            errors=errors,
        )

    # --- Step 2, anything else -----------------------------------------------

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        typed_user, typed_password = credentials_in(self._typed_url) if self._typed_url else (None, None)

        if user_input is not None:
            username = user_input.get(CONF_USERNAME) or typed_user or ""
            password = user_input.get(CONF_PASSWORD) or typed_password or ""
            candidates, error = await self._stream_candidates(user_input, username, password)
            if candidates:
                found, error = await self._first_with_backchannel(candidates, username, password)
                if found is not None:
                    return self.async_create_entry(
                        title=self._name,
                        data={
                            CONF_KIND: KIND_ONVIF,
                            CONF_STREAM_URL: found,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                        },
                    )
            if error is not None:
                errors["base"] = error

        schema: dict[Any, Any] = {
            vol.Optional(CONF_USERNAME, default=(user_input or {}).get(CONF_USERNAME, typed_user or "")): str,
            vol.Optional(CONF_PASSWORD): _PASSWORD,
        }
        if self._ask_for_url:
            schema[vol.Required(CONF_STREAM_URL, default=(user_input or {}).get(CONF_STREAM_URL, ""))] = str
        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(schema),
            description_placeholders={"name": self._name, "host": self._host},
            errors=errors,
        )

    async def _stream_candidates(
        self, user_input: dict[str, Any], username: str, password: str
    ) -> tuple[list[str], str | None]:
        """The stream addresses to try, or why there are none."""
        if self._typed_url:
            return [without_credentials(self._typed_url)], None
        if self._ask_for_url:
            host, url = _parse_address(user_input.get(CONF_STREAM_URL, ""))
            if url is None:
                return [], "invalid_url"
            return [without_credentials(url)], None
        try:
            streams = await self.hass.async_add_executor_job(
                onvif_device.find_streams, self._host, username, password
            )
        except TalkbackError as e:
            if e.failure is Failure.REJECTED:
                return [], "invalid_auth"
            # No ONVIF to ask, or nothing readable from it: the address will
            # have to come from the person instead.
            _LOGGER.info("Could not ask %s for its streams: %s", self._host, e)
            self._ask_for_url = True
            return [], "no_stream_address"
        return [stream.uri for stream in streams], None

    async def _first_with_backchannel(
        self, candidates: list[str], username: str, password: str
    ) -> tuple[str | None, str | None]:
        """The first stream with a back channel - main streams first, since
        some cameras offer it on only one of them."""
        first_error: TalkbackError | None = None
        for url in candidates:
            error = await self.hass.async_add_executor_job(onvif.probe, url, username, password)
            if error is None:
                return url, None
            if error.failure is Failure.REJECTED:
                return None, "invalid_auth"
            first_error = first_error or error
        assert first_error is not None
        return None, _ONVIF_ERRORS.get(first_error.failure, "unknown")

    # --- Reconfigure ---------------------------------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if entry.data[CONF_KIND] == KIND_TAPO:
            return await self._reconfigure_tapo(entry, user_input)
        return await self._reconfigure_onvif(entry, user_input)

    async def _reconfigure_tapo(self, entry, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            # Blank keeps the saved one: a password field is never filled in.
            password = user_input.get(CONF_TAPO_CLOUD_PASSWORD) or entry.data[CONF_TAPO_CLOUD_PASSWORD]
            if host != entry.unique_id:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
            if not await self.hass.async_add_executor_job(tapo.looks_like_tapo, host):
                errors["base"] = "not_tapo"
            else:
                error = await self.hass.async_add_executor_job(tapo.check_cloud_password, host, password)
                if error is None:
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=host,
                        data={**entry.data, CONF_HOST: host, CONF_TAPO_CLOUD_PASSWORD: password},
                    )
                errors["base"] = _TAPO_ERRORS.get(error.failure, "unknown")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                    vol.Optional(CONF_TAPO_CLOUD_PASSWORD): _PASSWORD,
                }
            ),
            errors=errors,
        )

    async def _reconfigure_onvif(self, entry, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, url = _parse_address(user_input[CONF_STREAM_URL])
            if url is None:
                errors[CONF_STREAM_URL] = "invalid_url"
            else:
                typed_user, typed_password = credentials_in(url)
                username = user_input.get(CONF_USERNAME) or typed_user or ""
                password = user_input.get(CONF_PASSWORD) or typed_password or entry.data.get(CONF_PASSWORD, "")
                stream_url = without_credentials(url)
                if host != entry.unique_id:
                    await self.async_set_unique_id(host)
                    self._abort_if_unique_id_configured()
                error = await self.hass.async_add_executor_job(onvif.probe, stream_url, username, password)
                if error is None:
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=host,
                        data={
                            **entry.data,
                            CONF_STREAM_URL: stream_url,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                        },
                    )
                errors["base"] = _ONVIF_ERRORS.get(error.failure, "unknown")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STREAM_URL, default=entry.data[CONF_STREAM_URL]): str,
                    vol.Optional(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                    vol.Optional(CONF_PASSWORD): _PASSWORD,
                }
            ),
            errors=errors,
        )

    # --- Reauthentication ----------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Started by the camera refusing its login, not by a person."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if entry.data[CONF_KIND] == KIND_TAPO:
            return await self._reauth_tapo(entry, user_input)
        return await self._reauth_onvif(entry, user_input)

    async def _reauth_tapo(self, entry, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Blank tries the saved one again: the likelier fix is the Tapo
            # setting, switched off by a firmware update, not the password.
            password = user_input.get(CONF_TAPO_CLOUD_PASSWORD) or entry.data[CONF_TAPO_CLOUD_PASSWORD]
            error = await self.hass.async_add_executor_job(
                tapo.check_cloud_password, entry.data[CONF_HOST], password
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_TAPO_CLOUD_PASSWORD: password}
                )
            errors["base"] = _TAPO_ERRORS.get(error.failure, "unknown")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_TAPO_CLOUD_PASSWORD): _PASSWORD}),
            description_placeholders={"name": entry.title, "host": entry.data[CONF_HOST]},
            errors=errors,
        )

    async def _reauth_onvif(self, entry, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "")
            password = user_input.get(CONF_PASSWORD) or entry.data.get(CONF_PASSWORD, "")
            error = await self.hass.async_add_executor_job(
                onvif.probe, entry.data[CONF_STREAM_URL], username, password
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_USERNAME: username, CONF_PASSWORD: password}
                )
            errors["base"] = _ONVIF_ERRORS.get(error.failure, "unknown")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                    vol.Optional(CONF_PASSWORD): _PASSWORD,
                }
            ),
            description_placeholders={"name": entry.title, "host": entry.unique_id},
            errors=errors,
        )
