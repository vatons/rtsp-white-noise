"""G.711, the 8-bit telephone codec camera speakers take.

ONVIF back channels ask for mu-law (PCMU); Tapo's own channel wants A-law
(PCMA). Both are ITU-T G.711: companded, so quiet sounds keep their detail
and loud ones lose it. A port of Peekaboo's G711.
"""

from __future__ import annotations

_BIAS = 0x84
_ULAW_CLIP = 32635
_ALAW_CLIP = 0x7FFF


def _ulaw_sample(sample: int) -> int:
    sign = 0
    if sample < 0:
        sample = -sample
        sign = 0x80
    if sample > _ULAW_CLIP:
        sample = _ULAW_CLIP
    sample += _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _alaw_sample(sample: int) -> int:
    sign = 0x00 if sample < 0 else 0x80
    if sample < 0:
        sample = -sample
    if sample > _ALAW_CLIP:
        sample = _ALAW_CLIP
    if sample >= 256:
        exponent = 7
        mask = 0x4000
        while exponent > 0 and not (sample & mask):
            exponent -= 1
            mask >>= 1
        value = (exponent << 4) | ((sample >> (exponent + 3)) & 0x0F)
    else:
        value = sample >> 4
    # The 0x55 is the standard even-bit inversion, so a run of silence is
    # not a long run of identical bytes on the wire.
    return (value ^ 0x55) | sign


def encode_ulaw(samples: list[int]) -> bytes:
    return bytes(_ulaw_sample(s) for s in samples)


def encode_alaw(samples: list[int]) -> bytes:
    return bytes(_alaw_sample(s) for s in samples)
