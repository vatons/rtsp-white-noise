"""One RTSP conversation on one socket: requests, digest auth, and a pipe for RTP.

A port of Peekaboo's RtspConnection and RtspAuth. A reader thread owns the
input, because once PLAY has been sent the connection carries two kinds of
traffic: '$'-framed interleaved media, thrown away here - we asked for none,
and cameras send RTCP anyway - and RTSP responses. Draining is not
optional: left unread, the receive buffer fills and the camera stalls.

Responses are matched to requests by CSeq. That lets a keepalive go out
without anyone waiting for its answer - the send loop must not stop for a
round trip every 15 seconds - and its reply is simply dropped.
"""

from __future__ import annotations

import base64
import hashlib
import os
import queue
import re
import socket
import threading
import urllib.parse
from dataclasses import dataclass, field

DEFAULT_PORT = 554
BACKCHANNEL_REQUIRE = "www.onvif.org/ver20/backchannel"

_CONNECT_TIMEOUT_S = 5
_SOCKET_TIMEOUT_S = 5
_RESPONSE_TIMEOUT_S = 8
_MAX_HEAD_BYTES = 8 * 1024
_USER_AGENT = "camera-noise"


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def without_credentials(url: str) -> str:
    """`url` with any embedded user:password removed.

    The request URI is what a digest response is computed over, and the
    form without userinfo is the one every camera agrees on. It also keeps
    the password out of anything that logs a URL.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def credentials_in(url: str) -> tuple[str | None, str | None]:
    parts = urllib.parse.urlsplit(url)
    unquote = urllib.parse.unquote
    return (
        unquote(parts.username) if parts.username is not None else None,
        unquote(parts.password) if parts.password is not None else None,
    )


class _Challenge:
    """A parsed WWW-Authenticate: Digest overwhelmingly, Basic on cheaper hardware."""

    def __init__(self, header: str) -> None:
        self.basic = header.strip().lower().startswith("basic")
        self.realm = _quoted(header, "realm") or ""
        self.nonce = _quoted(header, "nonce") or ""
        qop = _quoted(header, "qop")
        self.qop_auth = qop is not None and "auth" in (q.strip() for q in qop.split(","))
        self.opaque = _quoted(header, "opaque")
        self._nc = 0

    def authorization(self, method: str, uri: str, username: str, password: str) -> str:
        if self.basic:
            return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        ha1 = _md5(f"{username}:{self.realm}:{password}")
        ha2 = _md5(f"{method}:{uri}")
        parts = [f'username="{username}"', f'realm="{self.realm}"', f'nonce="{self.nonce}"', f'uri="{uri}"']
        if self.qop_auth:
            # A fresh count for each request on the same nonce, as RFC 2617
            # asks; a server that sees one repeated may refuse it as a replay.
            self._nc += 1
            nc = f"{self._nc:08x}"
            cnonce = os.urandom(8).hex()
            parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
            response = _md5(f"{ha1}:{self.nonce}:{nc}:{cnonce}:auth:{ha2}")
        else:
            response = _md5(f"{ha1}:{self.nonce}:{ha2}")
        parts.append(f'response="{response}"')
        if self.opaque is not None:
            parts.append(f'opaque="{self.opaque}"')
        return "Digest " + ", ".join(parts)


class RtspConnection:
    def __init__(self, url: str, username: str | None = None, password: str | None = None) -> None:
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            raise ValueError("an RTSP address needs a host")
        self.host = parts.hostname
        self.port = parts.port or DEFAULT_PORT
        embedded_user, embedded_password = credentials_in(url)
        self._username = username or embedded_user
        self._password = password if password is not None else (embedded_password or "")
        self._socket: socket.socket | None = None
        self._write_lock = threading.Lock()
        self._waiting: dict[int, queue.Queue[Response]] = {}
        self._waiting_lock = threading.Lock()
        self._cseq = 0
        self._challenge: _Challenge | None = None
        self._closed = threading.Event()
        self._reader_error: OSError | None = None
        self.session_id: str | None = None

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=_CONNECT_TIMEOUT_S)
        sock.settimeout(_SOCKET_TIMEOUT_S)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._socket = sock
        threading.Thread(target=self._read_loop, name=f"rtsp-{self.host}", daemon=True).start()

    def request(self, method: str, target: str, headers: dict[str, str] | None = None) -> Response:
        """Sends `method` and waits for its response, answering one 401.

        The retry is the normal path: RTSP has no way to offer credentials
        before being asked, so the first request on a connection is refused
        and repeated - on the same socket, because the nonce is per
        connection on at least one camera.
        """
        first = self._exchange(method, target, headers or {})
        if first.status != 401 or self._username is None:
            return first
        header = first.headers.get("www-authenticate")
        if header is None:
            return first
        self._challenge = _Challenge(header)
        return self._exchange(method, target, headers or {})

    def send_without_waiting(self, method: str, target: str) -> None:
        """A request whose answer nobody needs - a keepalive. Its reply is dropped."""
        self._write(self._render(method, target, {}, self._next_cseq()))

    def write(self, data: bytes) -> None:
        if self._reader_error is not None:
            raise self._reader_error
        self._write(data)

    def close(self) -> None:
        self._closed.set()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()

    def _next_cseq(self) -> int:
        with self._waiting_lock:
            self._cseq += 1
            return self._cseq

    def _exchange(self, method: str, target: str, headers: dict[str, str]) -> Response:
        cseq = self._next_cseq()
        reply: queue.Queue[Response] = queue.Queue(maxsize=1)
        with self._waiting_lock:
            self._waiting[cseq] = reply
        try:
            self._write(self._render(method, target, headers, cseq))
            try:
                response = reply.get(timeout=_RESPONSE_TIMEOUT_S)
            except queue.Empty:
                raise self._reader_error or TimeoutError(f"no answer to {method}") from None
            if response.status == 0:
                # The reader's wake-up on its way out, not an answer.
                raise self._reader_error or ConnectionError("camera closed the connection")
            return response
        finally:
            with self._waiting_lock:
                self._waiting.pop(cseq, None)

    def _render(self, method: str, target: str, headers: dict[str, str], cseq: int) -> bytes:
        lines = [f"{method} {target} RTSP/1.0", f"CSeq: {cseq}", f"User-Agent: {_USER_AGENT}"]
        if self._challenge is not None and self._username is not None:
            lines.append(
                "Authorization: " + self._challenge.authorization(method, target, self._username, self._password)
            )
        if self.session_id is not None:
            lines.append(f"Session: {self.session_id}")
        lines += [f"{name}: {value}" for name, value in headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def _write(self, data: bytes) -> None:
        if self._socket is None:
            raise OSError("not connected")
        with self._write_lock:
            self._socket.sendall(data)

    def _read_loop(self) -> None:
        reader = _SocketReader(self._socket, self._closed)
        try:
            while not self._closed.is_set():
                first = reader.read_exact(1)
                if first == b"$":
                    reader.read_exact(1)  # channel
                    length = int.from_bytes(reader.read_exact(2), "big")
                    reader.read_exact(length)
                else:
                    self._deliver(self._read_response(reader, first))
        except OSError as e:
            if not self._closed.is_set():
                self._reader_error = e
        finally:
            # Wakes anyone waiting, so they see the failure now rather than
            # at their timeout.
            with self._waiting_lock:
                for waiter in self._waiting.values():
                    try:
                        waiter.put_nowait(Response(0, {}, ""))
                    except queue.Full:
                        pass

    def _read_response(self, reader: "_SocketReader", first: bytes) -> Response:
        head = first + reader.read_until(b"\r\n\r\n", _MAX_HEAD_BYTES)
        lines = head.decode("latin-1").strip().split("\r\n")
        status_parts = lines[0].split(" ")
        status = int(status_parts[1]) if len(status_parts) > 1 and status_parts[1].isdigit() else 0
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        body = reader.read_exact(length).decode("utf-8", "replace") if length > 0 else ""
        if "session" in headers:
            self.session_id = headers["session"].split(";")[0].strip()
        return Response(status, headers, body)

    def _deliver(self, response: Response) -> None:
        try:
            cseq = int(response.headers.get("cseq", ""))
        except ValueError:
            cseq = None
        with self._waiting_lock:
            if cseq is None and self._waiting:
                # A camera that leaves CSeq out: the oldest request is the
                # one being answered.
                cseq = min(self._waiting)
            waiter = self._waiting.get(cseq)
        if waiter is not None:
            try:
                waiter.put_nowait(response)
            except queue.Full:
                pass


class _SocketReader:
    """Buffered reads that survive socket timeouts.

    Not socket.makefile: after one timeout its file object refuses every
    read that follows, and here a timeout only means the camera had nothing
    to say for a few seconds.
    """

    def __init__(self, sock: socket.socket, closed: threading.Event) -> None:
        self._sock = sock
        self._closed = closed
        self._buffer = bytearray()

    def _fill(self) -> None:
        while True:
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                if self._closed.is_set():
                    raise OSError("closed") from None
                continue
            if not chunk:
                raise ConnectionError("camera closed the connection")
            self._buffer += chunk
            return

    def read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._fill()
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self._buffer:
            if len(self._buffer) > limit:
                raise OSError("RTSP header too long")
            self._fill()
        end = self._buffer.index(marker) + len(marker)
        data = bytes(self._buffer[:end])
        del self._buffer[:end]
        return data


def _quoted(header: str, key: str) -> str | None:
    match = re.search(rf'{key}\s*=\s*"([^"]*)"', header, re.IGNORECASE)
    return match.group(1) if match else None


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()
