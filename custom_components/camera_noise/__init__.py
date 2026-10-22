"""Camera Noise: white, pink and ocean noise through a camera's own speaker.

One config entry per camera, holding one NoiseSession that the media
player, the switch and the sleep timer all drive - so they can never
disagree about whether the noise is playing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .talk.errors import Failure, TalkbackError
from .talk.session import NoiseSession, State
from .talk.transports import factory_for

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.NUMBER, Platform.SWITCH]

# Refusals a new login can fix - and nothing else will, since they are
# never retried.
_NEEDS_LOGIN = (Failure.REJECTED, Failure.NEEDS_THIRD_PARTY_ACCESS)


class CameraNoise:
    """A camera's session, its sleep timer, and the entities that show them."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._listeners: list[Callable[[], None]] = []
        self.session = NoiseSession(factory_for(entry.data), on_change=self._changed_from_any_thread)
        # How long the noise plays once started; 0 plays it until stopped.
        self.sleep_minutes = 0.0
        # When the sleep timer stops the noise, while one is running.
        self.stop_at: datetime | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._reported: TalkbackError | None = None
        self._closed = False

    def play(self) -> None:
        if self.session.state is not State.OFF:
            return
        self.session.play()
        self._set_timer(self._timer_from_now())

    def resume(self, stop_at: datetime | None) -> None:
        """Play again what was playing when Home Assistant stopped - unless
        its sleep timer ran out while Home Assistant was down."""
        if stop_at is not None and stop_at <= dt_util.utcnow():
            return
        self.session.resume()
        self._set_timer(stop_at)

    def stop(self) -> None:
        self._set_timer(None)
        self.session.stop()

    def set_sleep_minutes(self, minutes: float) -> None:
        self.sleep_minutes = minutes
        if self.session.state is not State.OFF:
            # Set while it plays, it counts from now - as on a radio.
            self._set_timer(self._timer_from_now())
            self._notify()

    def close(self) -> None:
        """Stop for good - an unload, or Home Assistant stopping - without
        showing it as stopped.

        Shown, "stopped" is what would be saved for after the restart, and
        the noise would not come back. Until they go, the entities keep
        showing what was playing.
        """
        self._closed = True
        self._set_timer(None)
        self.session.stop()

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _timer_from_now(self) -> datetime | None:
        if not self.sleep_minutes:
            return None
        return dt_util.utcnow() + timedelta(minutes=self.sleep_minutes)

    def _set_timer(self, stop_at: datetime | None) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        self.stop_at = stop_at
        if stop_at is not None:
            self._cancel_timer = async_track_point_in_utc_time(self._hass, self._timer_ended, stop_at)

    @callback
    def _timer_ended(self, now: datetime) -> None:
        self._cancel_timer = None
        self.stop()

    def _changed_from_any_thread(self) -> None:
        # The session calls back on its transport's and timer's threads as
        # well as the event loop; entities may only be written on the loop.
        self._hass.loop.call_soon_threadsafe(self._notify)

    @callback
    def _notify(self) -> None:
        if self._closed:
            return
        if self.session.state is State.OFF:
            # However it ended - stopped, a refusal - there is nothing left
            # for the timer to stop.
            self._set_timer(None)
        error = self.session.last_error
        if error is not None and error is not self._reported and error.failure in _NEEDS_LOGIN:
            # Said where a person will see it, rather than found out at
            # bedtime: Settings shows the camera wants its login again.
            self._reported = error
            self._entry.async_start_reauth(self._hass)
        for listener in list(self._listeners):
            listener()


type CameraNoiseConfigEntry = ConfigEntry[CameraNoise]


async def async_setup_entry(hass: HomeAssistant, entry: CameraNoiseConfigEntry) -> bool:
    camera = entry.runtime_data = CameraNoise(hass, entry)

    @callback
    def _stopping(event: Event) -> None:
        # Ends the camera's session properly - an ONVIF camera is told, a
        # Tapo's socket closed - rather than cut off by the process going.
        camera.close()

    entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _stopping))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CameraNoiseConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Nothing may keep sending once the integration has gone.
        entry.runtime_data.close()
    return unloaded
