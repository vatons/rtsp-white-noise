"""The ONVIF back channel: the SDP parser, RTP framing, and a whole session
against a fake camera on localhost - so it runs anywhere and makes no sound.

The SDP is real: Peekaboo's capture from a Reolink doorbell, the camera the
exchange was first worked out against.
"""

from __future__ import annotations

import pathlib
import socket
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "camera_noise"))

from talk.errors import Failure  # noqa: E402
from talk.onvif import OnvifTalkback, probe  # noqa: E402
from talk.rtp import interleaved  # noqa: E402
from talk.rtsp import credentials_in, without_credentials  # noqa: E402
from talk.sdp import parse_backchannel  # noqa: E402

BASE = "rtsp://192.168.1.73/Preview_01_main/"
REQUEST = "rtsp://192.168.1.73:554/Preview_01_main"

REOLINK = """v=0
o=- 1788656428082475 1 IN IP4 192.168.1.73
s=Session streamed by "preview"
t=0 0
a=control:*
m=video 0 RTP/AVP 96
a=rtpmap:96 H264/90000
a=recvonly
a=control:track1
m=audio 0 RTP/AVP 97
a=rtpmap:97 MPEG4-GENERIC/16000
a=recvonly
a=control:track2
m=audio 0 RTP/AVP 0
a=control:track3
a=rtpmap:0 PCMU/8000
a=sendonly
"""

WITHOUT_BACKCHANNEL = REOLINK.split("m=audio 0 RTP/AVP 0")[0]


# --- SDP ---------------------------------------------------------------------

def test_finds_the_send_only_audio_track():
    found = parse_backchannel(REOLINK, BASE, REQUEST)
    assert found.control_url == "rtsp://192.168.1.73/Preview_01_main/track3"
    assert (found.payload_type, found.encoding, found.clock_rate) == (0, "PCMU", 8000)


def test_finds_nothing_without_a_back_channel():
    # The Tapo case: it answers the Require header with no sendonly track.
    assert parse_backchannel(WITHOUT_BACKCHANNEL, BASE, REQUEST) is None


def test_refuses_a_format_it_cannot_produce():
    opus = REOLINK.replace("RTP/AVP 0", "RTP/AVP 111").replace("a=rtpmap:0 PCMU/8000", "a=rtpmap:111 OPUS/48000")
    assert parse_backchannel(opus, BASE, REQUEST) is None


def test_accepts_a_law_and_prefers_mu_law_when_both_are_offered():
    alaw = REOLINK.replace("RTP/AVP 0", "RTP/AVP 8").replace("a=rtpmap:0 PCMU/8000", "a=rtpmap:8 PCMA/8000")
    assert parse_backchannel(alaw, BASE, REQUEST).encoding == "PCMA"
    both = REOLINK.replace("RTP/AVP 0", "RTP/AVP 8 0").replace(
        "a=rtpmap:0 PCMU/8000", "a=rtpmap:8 PCMA/8000\na=rtpmap:0 PCMU/8000"
    )
    assert parse_backchannel(both, BASE, REQUEST).payload_type == 0


def test_accepts_static_payload_types_with_no_rtpmap():
    bare = REOLINK.replace("a=rtpmap:0 PCMU/8000\n", "")
    assert parse_backchannel(bare, BASE, REQUEST).payload_type == 0


def test_resolves_control_urls():
    absolute = REOLINK.replace("a=control:track3", "a=control:rtsp://192.168.1.73/other/track9")
    assert parse_backchannel(absolute, BASE, REQUEST).control_url == "rtsp://192.168.1.73/other/track9"
    assert parse_backchannel(REOLINK, None, REQUEST).control_url == REQUEST + "/track3"


# --- RTP and URLs ------------------------------------------------------------

def test_rtp_frame_layout():
    frame = interleaved(3, 8, 0x1_1234, 0xDEADBEEF, 0x11223344, bytes(160))
    assert frame[:4] == b"$\x03\x00\xac"  # 12 + 160 = 172
    assert frame[4] == 0x80 and frame[5] == 8
    assert frame[6:8] == b"\x12\x34"  # sequence wraps at 16 bits
    assert frame[8:12] == bytes.fromhex("deadbeef")
    assert frame[12:16] == bytes.fromhex("11223344")
    assert len(frame) == 176


def test_credentials_come_out_of_the_url():
    url = "rtsp://admin:p%40ss@192.168.1.20:554/h264Preview_01_main"
    assert without_credentials(url) == "rtsp://192.168.1.20:554/h264Preview_01_main"
    assert credentials_in(url) == ("admin", "p@ss")


# --- A whole session against a fake camera -----------------------------------

class FakeCamera:
    """Just enough RTSP: a digest 401, the Reolink SDP, SETUP, PLAY, and RTP in."""

    def __init__(self, sdp: str = REOLINK, password: str = "right") -> None:
        self.sdp = sdp
        self.password = password
        self.methods: list[str] = []
        self.rtp: list[bytes] = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.url = f"rtsp://127.0.0.1:{self.port}/Preview_01_main"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        import hashlib

        conn, _ = self.server.accept()
        buffer = b""
        nonce = "abc123"

        def reply(cseq, status, headers=(), body=""):
            lines = [f"RTSP/1.0 {status}", f"CSeq: {cseq}", *headers]
            if body:
                lines.append(f"Content-Length: {len(body)}")
            conn.sendall(("\r\n".join(lines) + "\r\n\r\n" + body).encode())

        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while buffer:
                    if buffer[:1] == b"$":
                        if len(buffer) < 4:
                            break
                        length = int.from_bytes(buffer[2:4], "big")
                        if len(buffer) < 4 + length:
                            break
                        self.rtp.append(buffer[: 4 + length])
                        buffer = buffer[4 + length:]
                        continue
                    if b"\r\n\r\n" not in buffer:
                        break
                    head, buffer = buffer.split(b"\r\n\r\n", 1)
                    lines = head.decode().split("\r\n")
                    method, uri, _ = lines[0].split(" ")
                    headers = dict(line.split(": ", 1) for line in lines[1:])
                    cseq = headers["CSeq"]
                    self.methods.append(method)
                    auth = headers.get("Authorization", "")
                    if method != "TEARDOWN" and "response=" not in auth:
                        reply(cseq, "401 Unauthorized", [f'WWW-Authenticate: Digest realm="cam", nonce="{nonce}"'])
                        continue
                    if "response=" in auth:
                        ha1 = hashlib.md5(f"admin:cam:{self.password}".encode()).hexdigest()
                        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
                        expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
                        if f'response="{expected}"' not in auth:
                            reply(cseq, "401 Unauthorized", [f'WWW-Authenticate: Digest realm="cam", nonce="{nonce}"'])
                            continue
                    if method == "DESCRIBE":
                        reply(cseq, "200 OK", ["Content-Type: application/sdp"], self.sdp)
                    elif method == "SETUP":
                        # Grants a different channel pair from the one asked for.
                        reply(cseq, "200 OK", ["Session: 77;timeout=60", "Transport: RTP/AVP/TCP;interleaved=2-3"])
                    else:
                        reply(cseq, "200 OK")
        except OSError:
            return


def test_probe_finds_the_back_channel_and_plays_nothing(allow_sockets):
    camera = FakeCamera()
    assert probe(camera.url, "admin", "right") is None
    assert camera.methods == ["DESCRIBE", "DESCRIBE"]  # the 401, then the answer


def test_probe_says_when_the_password_is_wrong(allow_sockets):
    camera = FakeCamera()
    assert probe(camera.url, "admin", "wrong").failure is Failure.REJECTED


def test_probe_says_when_there_is_no_back_channel(allow_sockets):
    camera = FakeCamera(sdp=WITHOUT_BACKCHANNEL)
    assert probe(camera.url, "admin", "right").failure is Failure.UNSUPPORTED


def test_a_whole_session(allow_sockets):
    camera = FakeCamera()
    playing = threading.Event()
    stopped: list = []
    talk = OnvifTalkback(
        camera.url, "admin", "right", lambda n: [1000] * n, playing.set, lambda error: stopped.append(error)
    )
    talk.start()
    assert playing.wait(5)
    time.sleep(0.5)
    talk.stop()
    talk.join(5)
    time.sleep(0.2)

    assert stopped == [None]
    # Challenged once, then the login carried on every request after it.
    assert camera.methods == ["DESCRIBE", "DESCRIBE", "SETUP", "PLAY", "TEARDOWN"]
    # About 25 packets in half a second; each on the channel the camera
    # granted, PCMU, 160 samples, and in sequence.
    assert 15 <= len(camera.rtp) <= 40
    assert all(frame[1] == 2 and frame[5] == 0 and len(frame) == 4 + 12 + 160 for frame in camera.rtp)
    sequences = [int.from_bytes(frame[6:8], "big") for frame in camera.rtp]
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(sequences, sequences[1:]))
