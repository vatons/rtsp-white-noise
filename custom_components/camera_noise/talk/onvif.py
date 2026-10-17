"""Plays audio out of a camera's speaker over the ONVIF back channel.

The standard way: most cameras with two-way audio - Reolink, Dahua,
Amcrest and plenty of generic ones - support it on their ordinary RTSP
stream. A port of Peekaboo's Talkback, first walked through by hand
against a Reolink doorbell:

 - DESCRIBE with `Require: www.onvif.org/ver20/backchannel`. A camera with
   a speaker answers with an extra media section: audio, a=sendonly, G.711.
 - SETUP that track as RTP/AVP/TCP;interleaved=0-1, so the audio shares the
   connection already open.
 - PLAY, then RTP packets of 160 samples, one every 20 ms.

Packets leave at the rate they represent - too fast overruns the camera's
buffer, too slow and the speaker stutters - so the loop paces against a
fixed schedule from the start rather than sleeping 20 ms each time.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable

from . import g711
from .errors import Failure, TalkbackError
from .rtp import interleaved
from .rtsp import BACKCHANNEL_REQUIRE, RtspConnection, without_credentials
from .sdp import Backchannel, parse_backchannel

_LOGGER = logging.getLogger(__name__)

_PACKET_MS = 20
# Comfortably inside the shortest session timeout seen (30 s).
_KEEPALIVE_S = 15
_AUDIO_CHANNEL = 0


class OnvifTalkback:
    """One back-channel session; the same shape as TapoTalkback."""

    def __init__(
        self,
        stream_url: str,
        username: str | None,
        password: str | None,
        source: Callable[[int], list[int]],
        on_playing: Callable[[], None] | None = None,
        on_stopped: Callable[[TalkbackError | None], None] | None = None,
    ) -> None:
        self._url = stream_url
        self._target = without_credentials(stream_url)
        self._username = username or None
        self._password = password
        self._source = source
        self._on_playing = on_playing or (lambda: None)
        self._on_stopped = on_stopped or (lambda error: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection: RtspConnection | None = None
        self._playing = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="onvif-talkback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Still setting up: cut it short rather than wait out a slow camera's
        # reply. Once playing, the send loop sees the stop within a frame and
        # tears the session down properly.
        if not self._playing and self._connection is not None:
            self._connection.close()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        error: TalkbackError | None = None
        connection = RtspConnection(self._url, self._username, self._password)
        self._connection = connection
        try:
            connection.connect()
            channel, backchannel = _open(connection, self._target)
            self._playing = True
            self._on_playing()
            self._send(connection, channel, backchannel)
        except TalkbackError as e:
            error = None if self._stop.is_set() else e
        except OSError as e:
            error = None if self._stop.is_set() else TalkbackError(Failure.of(e), str(e) or type(e).__name__)
        except Exception as e:  # noqa: BLE001 - reported, not swallowed
            _LOGGER.exception("ONVIF talkback failed")
            error = TalkbackError(Failure.UNKNOWN, str(e) or type(e).__name__)
        finally:
            if connection.session_id is not None:
                try:
                    # Without waiting: the camera ending the session is its
                    # business, and stopping should not hang on its reply.
                    connection.send_without_waiting("TEARDOWN", self._target)
                except OSError:
                    pass
            connection.close()
            if error is not None:
                _LOGGER.warning("ONVIF talkback on %s stopped: %s", self._target, error)
            self._on_stopped(error)

    def _send(self, connection: RtspConnection, channel: int, backchannel: Backchannel) -> None:
        samples = backchannel.clock_rate * _PACKET_MS // 1000
        encode = g711.encode_alaw if backchannel.encoding == "PCMA" else g711.encode_ulaw
        sequence = int.from_bytes(os.urandom(2), "big")
        timestamp = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        ssrc = int.from_bytes(os.urandom(4), "big")

        started = time.monotonic()
        packets = 0
        keepalive_due = started + _KEEPALIVE_S
        while not self._stop.is_set():
            payload = encode(self._source(samples))
            connection.write(interleaved(channel, backchannel.payload_type, sequence, timestamp, ssrc, payload))
            sequence = (sequence + 1) & 0xFFFF
            timestamp = (timestamp + samples) & 0xFFFFFFFF
            packets += 1

            now = time.monotonic()
            if now >= keepalive_due:
                # A camera is not obliged to count our RTP as activity, so
                # the control channel says something of its own - without
                # waiting for the answer, which would hold up the audio.
                connection.send_without_waiting("OPTIONS", self._target)
                keepalive_due = now + _KEEPALIVE_S

            delay = started + packets * _PACKET_MS / 1000 - time.monotonic()
            if delay > 0:
                time.sleep(delay)


def _open(connection: RtspConnection, target: str) -> tuple[int, Backchannel]:
    """DESCRIBE, SETUP and PLAY; the interleaved channel and the track."""
    describe = connection.request(
        "DESCRIBE", target, {"Accept": "application/sdp", "Require": BACKCHANNEL_REQUIRE}
    )
    _raise_for(describe.status, "DESCRIBE")
    backchannel = parse_backchannel(describe.body, describe.headers.get("content-base"), target)
    if backchannel is None:
        raise TalkbackError(Failure.UNSUPPORTED, "the camera offers no back channel this can use")

    setup = connection.request(
        "SETUP",
        backchannel.control_url,
        {
            "Transport": f"RTP/AVP/TCP;unicast;interleaved={_AUDIO_CHANNEL}-{_AUDIO_CHANNEL + 1}",
            "Require": BACKCHANNEL_REQUIRE,
        },
    )
    _raise_for(setup.status, "SETUP")
    # The camera picks the channel, and is entitled to pick another pair.
    granted = re.search(r"interleaved=(\d+)", setup.headers.get("transport", ""))
    channel = int(granted.group(1)) if granted else _AUDIO_CHANNEL

    play = connection.request("PLAY", target, {"Range": "npt=0.000-", "Require": BACKCHANNEL_REQUIRE})
    _raise_for(play.status, "PLAY")
    return channel, backchannel


def _raise_for(status: int, method: str) -> None:
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise TalkbackError(Failure.REJECTED, f"the camera refused the username or password ({method} {status})")
    if status in (551, 454, 461):
        raise TalkbackError(Failure.UNSUPPORTED, f"the camera will not open a back channel ({method} {status})")
    raise TalkbackError(Failure.UNKNOWN, f"the camera refused {method} ({status})")


def probe(stream_url: str, username: str | None, password: str | None) -> TalkbackError | None:
    """Whether the camera has a back channel this can use: one DESCRIBE, no sound.

    None when it does; otherwise why not - unreachable, a refused login, or
    no back channel - which is what decides what a person is told.
    """
    target = without_credentials(stream_url)
    connection = RtspConnection(stream_url, username or None, password)
    try:
        connection.connect()
        describe = connection.request(
            "DESCRIBE", target, {"Accept": "application/sdp", "Require": BACKCHANNEL_REQUIRE}
        )
        _raise_for(describe.status, "DESCRIBE")
        if parse_backchannel(describe.body, describe.headers.get("content-base"), target) is None:
            return TalkbackError(Failure.UNSUPPORTED, "the camera offers no back channel this can use")
        return None
    except TalkbackError as e:
        return e
    except (OSError, ValueError) as e:
        failure = Failure.of(e) if isinstance(e, OSError) else Failure.UNKNOWN
        return TalkbackError(failure, str(e) or type(e).__name__)
    finally:
        connection.close()
