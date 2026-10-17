"""Finds a camera's talk-back track in its SDP, if it has one.

A port of Peekaboo's BackchannelSdp. The only honest test for talk-back:
a camera is supposed to answer 551 to a Require header it does not
implement, and the Tapo C120 answers 200 regardless - so the description
has to be read. What is wanted is an audio section marked a=sendonly (the
camera receives; we send) in a format this can produce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# What this can encode, by SDP encoding name, preferred first.
PCMU = 0
PCMA = 8
_ENCODINGS = {"PCMU": PCMU, "PCMA": PCMA}
# RFC 3551 assigns these statically, so a bare "RTP/AVP 0" needs no rtpmap.
_STATIC = {PCMU: "PCMU", PCMA: "PCMA"}


@dataclass(frozen=True)
class Backchannel:
    control_url: str
    payload_type: int
    encoding: str  # "PCMU" or "PCMA"
    clock_rate: int = 8000


def parse_backchannel(sdp: str, content_base: str | None, request_url: str) -> Backchannel | None:
    sections = re.split(r"(?m)^m=", sdp)[1:]
    for section in sections:
        lines = [line.strip() for line in ("m=" + section).splitlines()]
        if not lines[0].startswith("m=audio"):
            continue
        if "a=sendonly" not in lines:
            continue
        chosen = _payload(lines)
        if chosen is None:
            continue
        control = next((line[len("a=control:"):].strip() for line in lines if line.startswith("a=control:")), None)
        if control is None:
            continue
        payload_type, encoding, clock_rate = chosen
        return Backchannel(_resolve(control, content_base, request_url), payload_type, encoding, clock_rate)
    return None


def _payload(lines: list[str]) -> tuple[int, str, int] | None:
    offered = [int(p) for p in lines[0].split()[3:] if p.isdigit()]
    rtpmaps: dict[int, tuple[str, int]] = {}
    for line in lines:
        # a=rtpmap:0 PCMU/8000
        match = re.match(r"a=rtpmap:(\d+)\s+([^/\s]+)/(\d+)", line)
        if match:
            rtpmaps[int(match.group(1))] = (match.group(2).upper(), int(match.group(3)))
    candidates = []
    for payload_type in offered:
        encoding, clock_rate = rtpmaps.get(payload_type, (_STATIC.get(payload_type), 8000))
        if encoding in _ENCODINGS and clock_rate == 8000:
            candidates.append((payload_type, encoding, clock_rate))
    # mu-law where there is a choice - it is what every back channel seen so
    # far offers, and so the best-trodden path through a camera's firmware.
    candidates.sort(key=lambda c: list(_ENCODINGS).index(c[1]))
    return candidates[0] if candidates else None


def _resolve(control: str, content_base: str | None, request_url: str) -> str:
    """Content-Base wins where given; the request URL is RFC 2326's fallback."""
    if control.lower().startswith("rtsp://"):
        return control
    base = (content_base or request_url).rstrip("/")
    if control == "*":
        return base
    return f"{base}/{control}"
