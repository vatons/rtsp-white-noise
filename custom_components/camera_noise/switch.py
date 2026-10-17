"""The noise as a plain on/off switch, for automations and voice.

The media player has no "off" - see media_player.py - so "turn on the
nursery noise" and a bedtime automation's switch.turn_on need this. Same
session underneath, so the two always agree.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CameraNoiseConfigEntry
from .media_player import device_info
from .talk.session import State


async def async_setup_entry(
    hass: HomeAssistant, entry: CameraNoiseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([CameraNoiseSwitch(entry)])


class CameraNoiseSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "noise"
    _attr_icon = "mdi:waves"
    _attr_should_poll = False

    def __init__(self, entry: CameraNoiseConfigEntry) -> None:
        self._camera = entry.runtime_data
        self._session = entry.runtime_data.session
        self._attr_unique_id = f"{entry.entry_id}_noise"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._camera.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        # Connecting and reconnecting count: that is still noise somebody
        # asked for, and can turn off from here.
        return self._session.state is not State.OFF

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._session.play()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._session.stop()
