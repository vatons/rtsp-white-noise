"""The sleep timer: how long the noise plays for once it starts.

A setting rather than a countdown, so it can be set before bedtime and
applies however the noise is turned on - the player, the switch, an
automation, voice. Changed while the noise plays, it counts from then. 0
plays the noise until it is stopped.

Built in rather than left to an automation's delay, which a restart
cancels: with the noise coming back after a restart, the turn-off would
never come. The time the noise will stop shows on the media player, and
survives a restart with it.
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberMode, RestoreNumber
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CameraNoiseConfigEntry
from .media_player import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: CameraNoiseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([CameraNoiseSleepTimer(entry)])


class CameraNoiseSleepTimer(RestoreNumber):
    _attr_has_entity_name = True
    _attr_translation_key = "sleep_timer"
    _attr_icon = "mdi:timer-outline"
    _attr_should_poll = False
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    # Twelve hours: the longest night anyone asks for.
    _attr_native_max_value = 720
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: CameraNoiseConfigEntry) -> None:
        self._camera = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_sleep_timer"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            # The setting only. A timer already running when Home Assistant
            # stopped comes back with the player, at the time it had.
            self._camera.sleep_minutes = last.native_value

    @property
    def native_value(self) -> float:
        return self._camera.sleep_minutes

    async def async_set_native_value(self, value: float) -> None:
        self._camera.set_sleep_minutes(value)
        self.async_write_ha_state()
