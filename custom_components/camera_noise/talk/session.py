"""One camera's noise, from Play to Stop, across as many connections as it takes.

A port of the session half of Peekaboo's TalkbackService, without the
Android: a new generator per connection at the current volume and sound,
volume and sound changed live, and a dropout reconnected with backoff
rather than ending the noise - a camera rebooting for a firmware update at
3am should not leave the room suddenly quiet.

Only a session that has played is reconnected. One that never got that far
failed on something a person has to fix - a wrong password, the Tapo
setting, a camera that is off - and retrying it all night fixes nothing,
and on a Tapo counts towards a login lockout.

Transports call back on their own threads; so does the reconnect timer.
`on_change` is therefore called from any thread, and the Home Assistant
side hands it to the event loop.
"""

from __future__ import annotations

import enum
import logging
import threading
from collections.abc import Callable
from typing import Protocol

from .errors import TalkbackError
from .noise import WHITE, SoothingNoise

_LOGGER = logging.getLogger(__name__)

# Seconds between reconnection attempts, settling at the last.
RECONNECT_DELAYS = (1, 2, 5, 10, 30)


class State(enum.Enum):
    OFF = "off"
    CONNECTING = "connecting"
    PLAYING = "playing"
    RECONNECTING = "reconnecting"


class Transport(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


# Makes a transport for one connection: (source, on_playing, on_stopped).
TransportFactory = Callable[
    [Callable[[int], list[int]], Callable[[], None], Callable[[TalkbackError | None], None]],
    Transport,
]


class NoiseSession:
    def __init__(self, factory: TransportFactory, on_change: Callable[[], None] = lambda: None) -> None:
        self._factory = factory
        self._on_change = on_change
        self._lock = threading.RLock()
        self._volume = 0.5
        self._sound = WHITE
        self._state = State.OFF
        self._last_error: TalkbackError | None = None
        # Every start and stop takes a new number; a callback carrying an old
        # one is about a connection that is no longer this session's.
        self._generation = 0
        self._transport: Transport | None = None
        self._noise: SoothingNoise | None = None
        self._ever_played = False
        self._attempts = 0
        self._timer: threading.Timer | None = None

    @property
    def state(self) -> State:
        return self._state

    @property
    def last_error(self) -> TalkbackError | None:
        """Why the last session ended on its own, if it did."""
        return self._last_error

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        with self._lock:
            self._volume = min(max(value, 0.0), 1.0)
            if self._noise is not None:
                self._noise.volume = self._volume

    @property
    def sound(self) -> str:
        return self._sound

    @sound.setter
    def sound(self, value: str) -> None:
        with self._lock:
            self._sound = value
            if self._noise is not None:
                self._noise.sound = value

    def play(self) -> None:
        with self._lock:
            if self._state is not State.OFF:
                return
            self._generation += 1
            self._ever_played = False
            self._attempts = 0
            self._last_error = None
            self._open(self._generation)
            self._set_state(State.CONNECTING)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._cancel_timer()
            transport, self._transport, self._noise = self._transport, None, None
            if transport is not None:
                transport.stop()
            self._set_state(State.OFF)

    def _open(self, generation: int) -> None:
        noise = SoothingNoise(sound=self._sound, volume=self._volume)
        self._noise = noise
        self._transport = self._factory(
            noise.read,
            lambda: self._playing(generation),
            lambda error: self._stopped(generation, error),
        )
        self._transport.start()

    def _playing(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._ever_played = True
            self._attempts = 0
            self._set_state(State.PLAYING)

    def _stopped(self, generation: int, error: TalkbackError | None) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._transport = None
            self._noise = None
            if self._ever_played and (error is None or error.failure.worth_retrying):
                # A connection that ended without being asked to - with or
                # without an error - is a dropout, not a request for silence.
                delay = RECONNECT_DELAYS[min(self._attempts, len(RECONNECT_DELAYS) - 1)]
                self._attempts += 1
                _LOGGER.warning("Lost the speaker (%s) - reconnecting in %ss", error or "closed", delay)
                self._timer = threading.Timer(delay, self._reconnect, (generation,))
                self._timer.daemon = True
                self._timer.start()
                self._set_state(State.RECONNECTING)
                return
            self._last_error = error
            self._generation += 1
            self._set_state(State.OFF)

    def _reconnect(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._timer = None
            self._open(generation)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _set_state(self, state: State) -> None:
        self._state = state
        self._on_change()
