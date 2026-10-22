"""The session: what happens on play, stop, a dropout and a refusal.

A fake transport stands in for the camera, so nothing here makes a sound.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "camera_noise"))

from talk import session as session_module  # noqa: E402
from talk.errors import Failure, TalkbackError  # noqa: E402
from talk.session import NoiseSession, State  # noqa: E402


class FakeTransport:
    def __init__(self, source, on_playing, on_stopped):
        self.source = source
        self.on_playing = on_playing
        self.on_stopped = on_stopped
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def make_session():
    made: list[FakeTransport] = []

    def factory(source, on_playing, on_stopped):
        transport = FakeTransport(source, on_playing, on_stopped)
        made.append(transport)
        return transport

    return NoiseSession(factory), made


def test_play_connects_and_then_plays():
    session, made = make_session()
    session.play()
    assert session.state is State.CONNECTING
    assert made[0].started
    made[0].on_playing()
    assert session.state is State.PLAYING


def test_volume_and_sound_reach_the_noise_while_playing():
    session, made = make_session()
    session.volume = 0.3
    session.play()
    session.volume = 0.8
    session.sound = "ocean"
    noise = made[0].source.__self__
    assert noise.volume == 0.8
    assert noise.sound == "ocean"


def test_stop_ignores_the_old_connection_saying_it_finished():
    session, made = make_session()
    session.play()
    made[0].on_playing()
    session.stop()
    assert made[0].stopped
    assert session.state is State.OFF
    # The transport reports its end after the stop - it must not be read as
    # a dropout and reconnected.
    made[0].on_stopped(None)
    assert session.state is State.OFF
    assert len(made) == 1


def test_a_refusal_before_playing_ends_it_and_says_why():
    session, made = make_session()
    session.play()
    made[0].on_stopped(TalkbackError(Failure.NEEDS_THIRD_PARTY_ACCESS, "HTTP 401"))
    assert session.state is State.OFF
    assert session.last_error.failure is Failure.NEEDS_THIRD_PARTY_ACCESS
    assert len(made) == 1


def test_a_dropout_after_playing_reconnects(monkeypatch):
    monkeypatch.setattr(session_module, "RECONNECT_DELAYS", (0.05,))
    session, made = make_session()
    session.play()
    made[0].on_playing()
    made[0].on_stopped(TalkbackError(Failure.UNREACHABLE, "timed out"))
    assert session.state is State.RECONNECTING
    deadline = time.monotonic() + 2
    while len(made) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(made) == 2 and made[1].started
    made[1].on_playing()
    assert session.state is State.PLAYING


def test_stopping_while_reconnecting_cancels_the_reconnect(monkeypatch):
    monkeypatch.setattr(session_module, "RECONNECT_DELAYS", (0.1,))
    session, made = make_session()
    session.play()
    made[0].on_playing()
    made[0].on_stopped(TalkbackError(Failure.UNREACHABLE, "timed out"))
    session.stop()
    time.sleep(0.3)
    assert session.state is State.OFF
    assert len(made) == 1


def test_a_refused_credential_after_playing_is_not_retried():
    session, made = make_session()
    session.play()
    made[0].on_playing()
    made[0].on_stopped(TalkbackError(Failure.REJECTED, "HTTP 401"))
    assert session.state is State.OFF
    assert len(made) == 1


def test_resuming_after_a_restart_retries_a_camera_not_back_yet(monkeypatch):
    # Home Assistant back from a power cut before the camera is.
    monkeypatch.setattr(session_module, "RECONNECT_DELAYS", (0.05,))
    session, made = make_session()
    session.resume()
    made[0].on_stopped(TalkbackError(Failure.UNREACHABLE, "timed out"))
    assert session.state is State.RECONNECTING
    deadline = time.monotonic() + 2
    while len(made) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(made) == 2 and made[1].started
    made[1].on_playing()
    assert session.state is State.PLAYING


def test_resuming_after_a_restart_does_not_retry_a_refusal():
    session, made = make_session()
    session.resume()
    made[0].on_stopped(TalkbackError(Failure.REJECTED, "HTTP 401"))
    assert session.state is State.OFF
    assert session.last_error.failure is Failure.REJECTED
    assert len(made) == 1
