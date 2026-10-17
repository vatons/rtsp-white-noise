"""Just enough MPEG-TS to carry one audio track - for Tapo's talk channel.

Tapo does not take raw audio: it takes a transport stream wrapped in HTTP
multipart parts. So the audio goes as a program with a single elementary
stream, 20 ms at a time. A port of Peekaboo's MpegTs, itself written
against go2rtc's pkg/mpegts.
"""

from __future__ import annotations

# Tapo's private stream type for G.711 A-law.
STREAM_TYPE_PCMA_TAPO = 0x90

# 90 kHz, the clock every MPEG timestamp is measured in.
CLOCK_RATE = 90_000

_PACKET_SIZE = 188
_PAYLOAD_BYTES = _PACKET_SIZE - 4
_SYNC_BYTE = 0x47
_STUFFING = 0xFF

_PAT_PID = 0x0000
_PMT_PID = 0x1000
_PES_PID = 0x0100
_NO_PCR_PID = 0x1FFF

_PAYLOAD_START = 0x4000
_PAYLOAD_ONLY = 0x10
_ADAPTATION_AND_PAYLOAD = 0x30

_AUDIO_STREAM_ID = 0xC0
_PES_HEADER_BYTES = 14
_PTS_BYTES = 5

_TABLE_ID_PAT = 0x00
_TABLE_ID_PMT = 0x02
_TABLE_ID_EXTENSION = 1
_PROGRAM_NUMBER = 1
_SECTION_SYNTAX = 0xB000
_SECTION_VERSION_CURRENT = 0xC1
_PROGRAM_INFO_NONE = 0xF000
_RESERVED_13BIT = 0xE000
_CRC_BYTES = 4


def crc32_mpeg2(data: bytes) -> int:
    """CRC-32/MPEG-2: poly 0x04C11DB7, all ones in, no reflection, no final xor.

    Not zlib.crc32, which reflects and inverts and would be silently rejected.
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


def _short(value: int) -> bytes:
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


class MpegTs:
    """A muxer for one session: the continuity counter runs across calls."""

    def __init__(self, stream_type: int) -> None:
        self._stream_type = stream_type
        self._continuity = 0

    def header(self) -> bytes:
        """The PAT and PMT a decoder needs before any audio makes sense."""
        return self._program_association() + self._program_map()

    def packets(self, payload: bytes, pts: int) -> bytes:
        """`payload` as transport packets, timestamped `pts` in 90 kHz ticks."""
        pes = self._pes(payload, pts)
        out = bytearray()
        offset = 0
        first = True
        while offset < len(pes):
            remaining = len(pes) - offset
            pid = _PES_PID | (_PAYLOAD_START if first else 0)
            packet = bytearray((_SYNC_BYTE, (pid >> 8) & 0xFF, pid & 0xFF))
            if remaining < _PAYLOAD_BYTES:
                # A short tail is padded with an adaptation field: a
                # transport stream has no short packets.
                padding = _PAYLOAD_BYTES - 1 - remaining
                packet.append(_ADAPTATION_AND_PAYLOAD | (self._continuity & 0x0F))
                packet.append(padding)
                packet.extend(b"\x00" * padding)
                packet.extend(pes[offset:])
                offset = len(pes)
            else:
                packet.append(_PAYLOAD_ONLY | (self._continuity & 0x0F))
                packet.extend(pes[offset:offset + _PAYLOAD_BYTES])
                offset += _PAYLOAD_BYTES
            self._continuity += 1
            first = False
            out.extend(packet)
        return bytes(out)

    def _pes(self, payload: bytes, pts: int) -> bytes:
        length = 3 + _PTS_BYTES + len(payload)
        head = bytearray((0, 0, 1, _AUDIO_STREAM_ID, (length >> 8) & 0xFF, length & 0xFF))
        head.append(0x80)  # marker bits
        head.append(0x80)  # a PTS follows, no DTS
        head.append(_PTS_BYTES)
        head.extend(self._timestamp(pts))
        return bytes(head) + payload

    @staticmethod
    def _timestamp(pts: int) -> bytes:
        """A 33-bit timestamp over five bytes, a marker bit low in each."""
        return bytes((
            (0x20 | ((pts >> 29) & 0x0E) | 1) & 0xFF,
            (pts >> 22) & 0xFF,
            (((pts >> 14) & 0xFF) | 1) & 0xFF,
            (pts >> 7) & 0xFF,
            (((pts << 1) & 0xFF) | 1) & 0xFF,
        ))

    def _program_association(self) -> bytes:
        body = _short(_PROGRAM_NUMBER) + _short(_RESERVED_13BIT | _PMT_PID)
        return self._section_packet(_PAT_PID, _TABLE_ID_PAT, body)

    def _program_map(self) -> bytes:
        body = (
            _short(_RESERVED_13BIT | _NO_PCR_PID)
            + _short(_PROGRAM_INFO_NONE)
            + bytes((self._stream_type,))
            + _short(_RESERVED_13BIT | _PES_PID)
            + _short(_PROGRAM_INFO_NONE)
        )
        return self._section_packet(_PMT_PID, _TABLE_ID_PMT, body)

    @staticmethod
    def _section_packet(pid: int, table_id: int, body: bytes) -> bytes:
        content = _short(_TABLE_ID_EXTENSION) + bytes((_SECTION_VERSION_CURRENT, 0, 0)) + body
        section = bytes((table_id,)) + _short(_SECTION_SYNTAX | (len(content) + _CRC_BYTES)) + content
        section += crc32_mpeg2(section).to_bytes(4, "big")
        packet = bytearray([_STUFFING] * _PACKET_SIZE)
        packet[0] = _SYNC_BYTE
        packet[1] = ((_PAYLOAD_START >> 8) | (pid >> 8)) & 0xFF
        packet[2] = pid & 0xFF
        packet[3] = _PAYLOAD_ONLY
        packet[4] = 0  # pointer field: the section starts immediately
        packet[5:5 + len(section)] = section
        return bytes(packet)
