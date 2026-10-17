"""Camera Noise: white, pink and ocean noise through a camera's own speaker.

One config entry per camera, holding one NoiseSession that both the media
player and the switch drive - so they can never disagree about whether
the noise is playing.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .talk.session import NoiseSession
from .talk.transports import factory_for

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.SWITCH]


class CameraNoise:
    """A camera's session, and the entities that show it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._listeners: list[Callable[[], None]] = []
        self.session = NoiseSession(factory_for(entry.data), on_change=self._changed_from_any_thread)

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _changed_from_any_thread(self) -> None:
        # The session calls back on its transport's and timer's threads as
        # well as the event loop; entities may only be written on the loop.
        self._hass.loop.call_soon_threadsafe(self._notify)

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


type CameraNoiseConfigEntry = ConfigEntry[CameraNoise]


async def async_setup_entry(hass: HomeAssistant, entry: CameraNoiseConfigEntry) -> bool:
    entry.runtime_data = CameraNoise(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CameraNoiseConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Nothing may keep sending once the integration has gone.
        entry.runtime_data.session.stop()
    return unloaded
