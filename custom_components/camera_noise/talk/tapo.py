"""Plays audio out of a Tapo camera's speaker, over TP-Link's own channel.

Tapo cameras have no ONVIF back channel. What they have is a proprietary
stream server on port 8800, reverse-engineered by go2rtc's pkg/tapo and
ported here from Peekaboo's TapoTalkback, which plays through a C120:

 - `POST /stream`, multipart/mixed in both directions.
 - HTTP digest with a derived credential: the username is always `admin`
   and the password is the uppercase hex digest of the TP-Link *cloud
   account* password - SHA-256 when the challenge says encrypt_type="3",
   MD5 otherwise. The digest exchange itself is MD5 either way.
 - A JSON part opens a talk session and returns a session id.
 - Audio then goes as MPEG-TS parts carrying G.711 A-law under Tapo's
   private stream type 0x90, one 20 ms frame at a time.

Nothing here contacts TP-Link: the cloud password is hashed locally and
presented to the camera on the LAN.

Two traps, both found the hard way in Peekaboo:

 - The digest nonce is per connection. Answering a challenge on a fresh
   socket gets a fresh challenge and a second 401, which looks exactly like
   a wrong password.
 - The Content-Length of the JSON part covers the body only; the CRLF after
   it is written separately. Counting it makes the camera wait for ever,
   silently.

It also needs Third-Party Compatibility switched on in the Tapo app (Me ->
Tapo Lab). Without it the camera refuses correct credentials.

Blocking sockets on a thread of its own, as in Peekaboo: a packet has to
leave every 20 ms and a camera buffers almost nothing, so the pacing should
not share a thread with anything else.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable

from . import g711
from .errors import Failure, TalkbackError
from .mpegts import CLOCK_RATE, STREAM_TYPE_PCMA_TAPO, MpegTs
from .noise import SAMPLE_RATE

_LOGGER = logging.getLogger(__name__)

PORT = 8800
_PATH = "/stream"
_USERNAME = "admin"
_USER_AGENT = "camera-noise"
_NC = "00000001"
_ENCRYPT_TYPE_SHA256 = "3"
_CLIENT_BOUNDARY_VALUE = "--client-stream-boundary--"
_CLIENT_BOUNDARY = "----client-stream-boundary--"
_TALK_REQUEST = b'{"params":{"talk":{"mode":"aec"},"method":"get"},"seq":3,"type":"request"}'

_PACKET_MS = 20
_SAMPLES = SAMPLE_RATE * _PACKET_MS // 1000
# A packet's length in the 90 kHz clock its timestamps count in. Multiplied
# out before dividing: 90 kHz is 11.25 ticks a sample, and rounded to 11 the
# timestamps fell 22 ms a second behind the audio they carried.
_TICKS_PER_PACKET = CLOCK_RATE * _SAMPLES // SAMPLE_RATE
_CONNECT_TIMEOUT_S = 5
_READ_TIMEOUT_S = 15
_MAX_HEAD_BYTES = 8 * 1024
_MAX_REPLY_BYTES = 64 * 1024

# The realm every Tapo camera's stream server announces.
TAPO_REALM = "TP-Link IP-Camera"


class TapoTalkback:
    """One talk session: connect, authenticate, open, then send until stopped.

    `source(count)` returns the next `count` 16-bit samples. `on_playing()`
    is called once audio is about to flow; `on_stopped(error)` once it has
    ended, with None when it was asked to stop. Both run on the sending
    thread.
    """

    def __init__(
        self,
        host: str,
        cloud_password: str,
        source: Callable[[int], list[int]],
        on_playing: Callable[[], None] | None = None,
        on_stopped: Callable[[TalkbackError | None], None] | None = None,
    ) -> None:
        self._host = host
        self._cloud_password = cloud_password
        self._source = source
        self._on_playing = on_playing or (lambda: None)
        self._on_stopped = on_stopped or (lambda error: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"tapo-talkback-{self._host}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        if sock is not None:
            # Wakes a thread blocked on the camera rather than waiting out
            # the read timeout.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        error: TalkbackError | None = None
        try:
            sock, reader = self._connect()
            session = self._open_talk_session(sock, reader)
            self._on_playing()
            self._send(sock, session)
        except TalkbackError as e:
            error = None if self._stop.is_set() else e
        except OSError as e:
            error = None if self._stop.is_set() else TalkbackError(Failure.of(e), str(e) or type(e).__name__)
        except Exception as e:  # noqa: BLE001 - reported, not swallowed
            _LOGGER.exception("Tapo talkback failed")
            error = TalkbackError(Failure.UNKNOWN, str(e) or type(e).__name__)
        finally:
            if self._socket is not None:
                try:
                    self._socket.close()
                except OSError:
                    pass
            if error is not None:
                _LOGGER.warning("Tapo talkback on %s stopped: %s", self._host, error)
            self._on_stopped(error)

    def _connect(self):
        """Authenticates, leaving the multipart stream open."""
        sock = socket.create_connection((self._host, PORT), timeout=_CONNECT_TIMEOUT_S)
        self._socket = sock
        sock.settimeout(_READ_TIMEOUT_S)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = sock.makefile("rb")

        sock.sendall(self._request(None))
        challenge_head = _read_head(reader)
        if _status(challenge_head) != 401:
            raise TalkbackError(Failure.UNKNOWN, f"camera did not ask for a login ({_status(challenge_head)})")
        challenge = _header(challenge_head, "WWW-Authenticate")
        if challenge is None:
            raise TalkbackError(Failure.UNKNOWN, "camera sent no authentication challenge")

        realm = _quoted(challenge, "realm") or ""
        nonce = _quoted(challenge, "nonce") or ""
        qop = _quoted(challenge, "qop") or "auth"
        opaque = _quoted(challenge, "opaque")
        sha256 = _quoted(challenge, "encrypt_type") == _ENCRYPT_TYPE_SHA256

        # The cloud password is never sent; its digest stands in for the
        # camera's local password.
        hash_name = "sha256" if sha256 else "md5"
        derived = hashlib.new(hash_name, self._cloud_password.encode()).hexdigest().upper()
        cnonce = os.urandom(16).hex()
        ha1 = _md5(f"{_USERNAME}:{realm}:{derived}")
        ha2 = _md5(f"POST:{_PATH}")
        response = _md5(f"{ha1}:{nonce}:{_NC}:{cnonce}:{qop}:{ha2}")
        authorization = (
            f'Digest username="{_USERNAME}", realm="{realm}", nonce="{nonce}", '
            f'uri="{_PATH}", qop={qop}, nc={_NC}, cnonce="{cnonce}", response="{response}"'
        )
        if opaque is not None:
            authorization += f', opaque="{opaque}", algorithm=MD5'

        # Same socket: the nonce is per connection.
        sock.sendall(self._request(authorization))
        head = _read_head(reader)
        status = _status(head)
        if status != 200:
            # A camera without third-party access refuses correct
            # credentials, so that is the likelier cause of a 401 than a typo.
            failure = Failure.NEEDS_THIRD_PARTY_ACCESS if status in (401, 403) else Failure.UNKNOWN
            raise TalkbackError(
                failure,
                f"the camera refused the cloud password (HTTP {status}, digest {'SHA-256' if sha256 else 'MD5'})",
            )
        return sock, reader

    def _open_talk_session(self, sock: socket.socket, reader) -> str:
        _write_part(sock, [("Content-Type", "application/json")], _TALK_REQUEST, trailing_crlf=True)
        reply = _read_until(reader, b"session_id")
        match = re.search(rb'"session_id"\s*:\s*"([^"]*)"', reply)
        if match is None:
            raise TalkbackError(Failure.UNSUPPORTED, "camera opened no talk session")
        return match.group(1).decode()

    def _send(self, sock: socket.socket, session: str) -> None:
        muxer = MpegTs(STREAM_TYPE_PCMA_TAPO)
        headers = [
            ("Content-Type", "audio/mp2t"),
            ("X-If-Encrypt", "0"),
            ("X-Session-Id", session),
        ]
        _write_part(sock, headers, muxer.header(), trailing_crlf=False)

        pts = 0
        packets = 0
        started = time.monotonic()
        while not self._stop.is_set():
            payload = g711.encode_alaw(self._source(_SAMPLES))
            _write_part(sock, headers, muxer.packets(payload, pts), trailing_crlf=False)
            pts += _TICKS_PER_PACKET
            packets += 1
            # Paced against the start, not the last packet, so the small
            # delays of each send do not add up into a slow drift.
            delay = started + packets * _PACKET_MS / 1000 - time.monotonic()
            if delay > 0:
                # time.sleep, not the stop event's wait: on Windows the
                # latter only times to ~15 ms, too coarse for 20 ms frames.
                # A stop is still seen within a frame.
                time.sleep(delay)

    def _request(self, authorization: str | None) -> bytes:
        lines = [
            f"POST {_PATH} HTTP/1.1",
            f"Host: {self._host}:{PORT}",
            f"User-Agent: {_USER_AGENT}",
            # A POST with no body still declares one. Firmware closes the
            # connection on "Content-Length: -1" or "Connection: keep-alive",
            # so this stays minimal on purpose.
            "Content-Length: 0",
            f"Content-Type: multipart/mixed; boundary={_CLIENT_BOUNDARY_VALUE}",
        ]
        if authorization is not None:
            lines.append(f"Authorization: {authorization}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()


def looks_like_tapo(host: str, timeout: float = _CONNECT_TIMEOUT_S) -> bool:
    """Whether `host` answers like a Tapo stream server. Sends no credentials."""
    try:
        with socket.create_connection((host, PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(
                f"POST {_PATH} HTTP/1.1\r\nHost: {host}:{PORT}\r\nContent-Length: 0\r\n"
                f"Content-Type: multipart/mixed; boundary={_CLIENT_BOUNDARY_VALUE}\r\n\r\n".encode()
            )
            return TAPO_REALM in _read_head(sock.makefile("rb"))
    except (OSError, TalkbackError):
        return False


def check_cloud_password(host: str, cloud_password: str) -> TalkbackError | None:
    """Runs the real login and stops there: no talk session, no sound.

    Unlike looks_like_tapo this does send a credential, so it counts towards
    the lockout these cameras apply after repeated failures.
    """
    probe = TapoTalkback(host, cloud_password, source=lambda count: [0] * count)
    try:
        probe._connect()
        return None
    except TalkbackError as e:
        return e
    except OSError as e:
        return TalkbackError(Failure.of(e), str(e) or type(e).__name__)
    finally:
        if probe._socket is not None:
            probe._socket.close()


def _write_part(sock: socket.socket, headers: list[tuple[str, str]], body: bytes, trailing_crlf: bool) -> None:
    head = _CLIENT_BOUNDARY + "\r\n"
    head += "".join(f"{name}: {value}\r\n" for name, value in headers)
    head += f"Content-Length: {len(body)}\r\n\r\n"
    sock.sendall(head.encode() + body + (b"\r\n" if trailing_crlf else b""))


def _read_head(reader) -> str:
    head = bytearray()
    while not head.endswith(b"\r\n\r\n"):
        byte = reader.read(1)
        if not byte:
            raise TalkbackError(Failure.UNREACHABLE, "camera closed the connection")
        head += byte
        if len(head) > _MAX_HEAD_BYTES:
            raise TalkbackError(Failure.UNKNOWN, "camera sent an oversized header")
    return head.decode("latin-1")


def _read_until(reader, marker: bytes) -> bytes:
    buffer = bytearray()
    while marker not in buffer:
        chunk = reader.read1(4096)
        if not chunk:
            raise TalkbackError(Failure.UNREACHABLE, "camera closed the connection")
        buffer += chunk
        if len(buffer) > _MAX_REPLY_BYTES:
            raise TalkbackError(Failure.UNSUPPORTED, "camera sent no talk session")
    return bytes(buffer)


def _status(head: str) -> int:
    """The response code off the status line - never by searching the block,
    where a nonce containing "200" would read a refusal as a success."""
    match = re.match(r"HTTP/\d\.\d (\d{3})", head.lstrip())
    return int(match.group(1)) if match else -1


def _header(head: str, name: str) -> str | None:
    for line in head.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return None


def _quoted(header: str, key: str) -> str | None:
    match = re.search(rf'{key}\s*=\s*"([^"]*)"', header, re.IGNORECASE)
    return match.group(1) if match else None


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()
