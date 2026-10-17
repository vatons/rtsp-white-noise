"""Soothing noise, generated as 8 kHz mono 16-bit samples.

A port of Peekaboo's SoothingNoise. Generated rather than played from a
file because the channel to a camera's speaker is G.711 at 8 kHz - a
telephone codec - into a speaker built for speech. Recorded audio would
arrive thin; noise does not care, because noise is what it already is.

Pure Python, no numpy: 8,000 samples a second is nothing, and it keeps the
integration free of dependencies.
"""

from __future__ import annotations

import math
import random

WHITE = "white"
PINK = "pink"
OCEAN = "ocean"
SOUNDS = (WHITE, PINK, OCEAN)

SAMPLE_RATE = 8000

# Full scale is deafening out of a camera. This is the ceiling volume 1.0
# maps to - loud rather than painful.
_MAX_AMPLITUDE = 12000.0
_UNIT = 32767
_MAX_PCM = 32767.0

# A volume change is ramped just enough not to step.
_VOLUME_RAMP_SAMPLES = SAMPLE_RATE * 3 // 10

# Brings Kellet's pink filter back to roughly the range of the white noise
# that went in, so volume means the same whichever sound is playing.
_PINK_GAIN = 3.5

# Ocean: a wave every ten seconds, never dropping below this much of full.
_WAVE_PERIOD_SAMPLES = 80_000
_WAVE_FLOOR = 0.35


class SoothingNoise:
    """White, pink or ocean noise at a volume that can change while playing.

    `sound` and `volume` may be set from any thread; `read` runs on the
    sending thread only. A session starts at its volume; only changes to
    the volume are ramped.
    """

    def __init__(self, sound: str = WHITE, volume: float = 0.5, rng: random.Random | None = None) -> None:
        self.sound = sound
        self.volume = volume
        self._rng = rng or random.Random()
        self._gain = 0.0
        self._ramp_target = 0.0
        self._ramp_step = 0.0
        self._started = False
        self._previous_white = 0
        # Paul Kellet's three-pole approximation to a -3dB/octave slope.
        self._pink0 = 0.0
        self._pink1 = 0.0
        self._pink2 = 0.0
        self._position = 0

    def read(self, count: int) -> list[int]:
        """The next `count` samples."""
        target = min(max(float(self.volume), 0.0), 1.0)
        if not self._started:
            self._gain = target
            self._ramp_target = target
            self._started = True
        elif target != self._ramp_target:
            # A fixed time rather than a fixed rate, so a change takes as
            # long from a quiet setting as from a loud one.
            self._ramp_step = abs(target - self._gain) / _VOLUME_RAMP_SAMPLES
            self._ramp_target = target

        shape = self.sound
        randint = self._rng.randint
        out = [0] * count
        for i in range(count):
            gain = self._gain
            if gain < self._ramp_target:
                gain = min(self._ramp_target, gain + self._ramp_step)
            elif gain > self._ramp_target:
                gain = max(self._ramp_target, gain - self._ramp_step)
            self._gain = gain

            white = randint(-_UNIT, _UNIT)
            if shape == PINK:
                shaped = self._pink(white)
                level = 1.0
            elif shape == OCEAN:
                shaped = self._pink(white)
                level = self._swell()
            else:
                # Averaging with the previous sample rolls the very top off;
                # straight white through this codec is harsh.
                shaped = (white + self._previous_white) / 2.0
                level = 1.0
            self._previous_white = white
            self._position += 1

            value = shaped * level * gain * _MAX_AMPLITUDE / _UNIT
            out[i] = int(max(-_MAX_PCM, min(_MAX_PCM, value)))
        return out

    def _pink(self, white: float) -> float:
        self._pink0 = 0.99765 * self._pink0 + white * 0.0990460
        self._pink1 = 0.96300 * self._pink1 + white * 0.2965164
        self._pink2 = 0.57000 * self._pink2 + white * 1.0526913
        return (self._pink0 + self._pink1 + self._pink2 + white * 0.1848) / _PINK_GAIN

    def _swell(self) -> float:
        """A long, shallow swell that never reaches silence - surf does not."""
        phase = (self._position % _WAVE_PERIOD_SAMPLES) / _WAVE_PERIOD_SAMPLES
        return _WAVE_FLOOR + (1.0 - _WAVE_FLOOR) * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
