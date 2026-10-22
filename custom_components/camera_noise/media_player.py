"""The camera's speaker as a media player: play, stop, volume and sound.

Idle rather than off while nothing plays. Home Assistant hides a media
player's volume and source while it is off, and the volume is exactly what
wants setting before the noise starts in a dark room - so the player is
always "on", and playing or not.

Pause stops: there is nothing to resume part-way through noise, and it
gives the media card's play/pause button and Assist's "pause"/"resume"
something sensible to do.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import CameraNoiseConfigEntry
from .const import CONF_KIND, DOMAIN, SOUND_LABELS
from .talk.session import State
from .talk.transports import KIND_TAPO

_SOUND_BY_LABEL = {label: sound for sound, label in SOUND_LABELS.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: CameraNoiseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([CameraNoisePlayer(entry)])


def device_info(entry: CameraNoiseConfigEntry) -> DeviceInfo:
    tapo = entry.data.get(CONF_KIND) == KIND_TAPO
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="TP-Link" if tapo else None,
        model="Tapo camera speaker" if tapo else "Camera speaker (ONVIF)",
    )


class CameraNoisePlayer(MediaPlayerEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = "mdi:waves"
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_should_poll = False
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )
    _attr_source_list = list(SOUND_LABELS.values())

    def __init__(self, entry: CameraNoiseConfigEntry) -> None:
        self._camera = entry.runtime_data
        self._session = entry.runtime_data.session
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The volume and sound chosen last time, not a default: a default
        # after a restart is how something too loud gets played into a room
        # with a sleeping baby in it. And whether it was playing: an update
        # or a power cut in the night should not leave the room suddenly
        # quiet. Noise that was off stays off.
        last = await self.async_get_last_state()
        if last is not None:
            volume = last.attributes.get("volume_level")
            if isinstance(volume, (int, float)):
                self._session.volume = float(volume)
            sound = _SOUND_BY_LABEL.get(last.attributes.get("source"))
            if sound is not None:
                self._session.sound = sound
            # Buffering too: still noise somebody asked for, mid-reconnect.
            if last.state in (MediaPlayerState.PLAYING, MediaPlayerState.BUFFERING):
                self._session.resume()
        self.async_on_remove(self._camera.add_listener(self.async_write_ha_state))

    @property
    def state(self) -> MediaPlayerState:
        return {
            State.OFF: MediaPlayerState.IDLE,
            State.PLAYING: MediaPlayerState.PLAYING,
        }.get(self._session.state, MediaPlayerState.BUFFERING)

    @property
    def volume_level(self) -> float:
        return self._session.volume

    @property
    def source(self) -> str:
        return SOUND_LABELS[self._session.sound]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        error = self._session.last_error
        return {
            "connection": self._session.state.value,
            "last_error": error.failure.value if error else None,
            "last_error_detail": str(error) if error else None,
        }

    async def async_media_play(self) -> None:
        self._session.play()

    async def async_media_pause(self) -> None:
        self._session.stop()

    async def async_media_stop(self) -> None:
        self._session.stop()

    async def async_set_volume_level(self, volume: float) -> None:
        self._session.volume = volume
        self.async_write_ha_state()

    async def async_select_source(self, source: str) -> None:
        sound = _SOUND_BY_LABEL.get(source)
        if sound is None:
            raise ValueError(f"Unknown sound: {source}")
        self._session.sound = sound
        self.async_write_ha_state()
