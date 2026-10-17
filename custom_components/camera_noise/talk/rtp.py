"""An RTP packet in RTSP's interleaved framing - a port of Peekaboo's RtpFrames.

Interleaved rather than UDP: the RTSP connection is already open and
already through the router, so there is no second port and no NAT to
negotiate. Four bytes of framing - '$', the channel, a 16-bit length -
then a 12-byte RTP header. The marker bit stays clear: it marks the start
of a talk spurt, and continuous noise has none.
"""

from __future__ import annotations

import struct


def interleaved(channel: int, payload_type: int, sequence: int, timestamp: int, ssrc: int, payload: bytes) -> bytes:
    packet_length = 12 + len(payload)
    return (
        struct.pack(
            ">cBHBBHII",
            b"$",
            channel & 0xFF,
            packet_length,
            0x80,  # RTP version 2, no padding, no extension, no CSRCs
            payload_type & 0x7F,
            sequence & 0xFFFF,
            timestamp & 0xFFFFFFFF,
            ssrc & 0xFFFFFFFF,
        )
        + payload
    )
