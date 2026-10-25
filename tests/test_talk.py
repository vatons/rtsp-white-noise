"""The protocol pieces, checked against bytes a real camera accepted.

The MPEG-TS vectors are from Peekaboo's MpegTsTest, which captured them
from the Python that first made a Tapo C120 play audible noise. A transport
stream a camera dislikes produces silence, not an error, so these are the
only warning there would be.

Runs with pytest, or on its own: python tests/test_talk.py
"""

from __future__ import annotations

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "camera_noise"))

from talk import g711, tapo  # noqa: E402
from talk.mpegts import CLOCK_RATE, STREAM_TYPE_PCMA_TAPO, MpegTs  # noqa: E402
from talk.noise import OCEAN, PINK, SOUNDS, WHITE, SoothingNoise  # noqa: E402

EXPECTED_HEADER = (
    "474000100000b00d0001c100000001f0002ab104b2" + "ff" * 167
    + "475000100002b0120001c10000fffff00090e100f0005631e8cd" + "ff" * 162
)

EXPECTED_PACKET = (
    "4741003009000000000000000000000001c000a88080052100010e11"
    "00070e151c232a31383f464d545b626970777e858c939aa1a8afb6bdc4cbd2d9e0e7ee"
    "f5fc030a11181f262d343b424950575e656c737a81888f969da4abb2b9c0c7ced5dce3"
    "eaf1f8ff060d141b222930373e454c535a61686f767d848b9299a0a7aeb5bcc3cad1d8"
    "dfe6edf4fb020910171e252c333a41484f565d646b727980878e959ca3aab1b8bfc6cd"
    "d4dbe2e9f0f7fe050c131a21282f363d444b5259"
)


def _payload() -> bytes:
    return bytes((i * 7) & 0xFF for i in range(160))


def test_header_matches_what_a_camera_accepted():
    header = MpegTs(STREAM_TYPE_PCMA_TAPO).header()
    assert len(header) == 376
    assert header.hex() == EXPECTED_HEADER


def test_audio_packet_matches_what_a_camera_accepted():
    packet = MpegTs(STREAM_TYPE_PCMA_TAPO).packets(_payload(), pts=1800)
    assert len(packet) == 188
    assert packet.hex() == EXPECTED_PACKET


def test_every_packet_is_188_bytes():
    muxer = MpegTs(STREAM_TYPE_PCMA_TAPO)
    for size in (1, 100, 160, 174, 175, 400, 1000):
        out = muxer.packets(bytes(size), pts=0)
        assert len(out) % 188 == 0, size
        assert all(out[i] == 0x47 for i in range(0, len(out), 188)), size


def test_a_second_of_tapo_packets_is_a_second_of_timestamps():
    # Slower timestamps than audio and a camera's buffer fills until it
    # drops sound to catch up - every 20-odd seconds, at 2% slow.
    assert tapo._TICKS_PER_PACKET * (1000 // tapo._PACKET_MS) == CLOCK_RATE


def test_continuity_runs_across_calls():
    muxer = MpegTs(STREAM_TYPE_PCMA_TAPO)
    assert muxer.packets(_payload(), 0)[3] & 0x0F == 0
    assert muxer.packets(_payload(), 1800)[3] & 0x0F == 1


def test_g711_silence_and_full_scale():
    assert g711.encode_ulaw([0, -1]) == bytes((0xFF, 0x7F))
    assert g711.encode_ulaw([32767, -32768]) == bytes((0x80, 0x00))
    assert g711.encode_alaw([0]) == bytes((0xD5,))
    assert g711.encode_alaw([40000]) == g711.encode_alaw([32767])
    assert g711.encode_alaw([-40000]) == g711.encode_alaw([-32768])


def test_every_sound_stays_in_range_and_follows_volume():
    for sound in SOUNDS:
        quiet = SoothingNoise(sound, 0.2, random.Random(1)).read(24_000)
        loud = SoothingNoise(sound, 1.0, random.Random(1)).read(24_000)
        assert max(abs(s) for s in loud) <= 32767, sound
        assert max(abs(s) for s in loud) > 2 * max(abs(s) for s in quiet), sound
        assert all(s == 0 for s in SoothingNoise(sound, 0.0).read(8_000)), sound


def test_pink_is_duller_than_white():
    def roughness(pcm):
        return sum(abs(pcm[i] - pcm[i - 1]) for i in range(1, len(pcm))) / (len(pcm) - 1)

    white = roughness(SoothingNoise(WHITE, 1.0, random.Random(2)).read(24_000))
    pink = roughness(SoothingNoise(PINK, 1.0, random.Random(2)).read(24_000))
    assert pink < white


def test_waves_swell_without_going_silent():
    pcm = SoothingNoise(OCEAN, 1.0, random.Random(3)).read(80_000)
    levels = [max(abs(s) for s in pcm[i:i + 4000]) for i in range(0, 80_000, 4000)]
    assert max(levels) > min(levels) * 3 / 2
    assert min(levels) > 200


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"{len(tests)} passed")
