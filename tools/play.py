"""Plays noise through a camera's speaker from the command line.

The proof of concept, before any of this is wrapped for Home Assistant:

    python tools/play.py --check
        Logs in and stops there. No sound. Use this first.

    python tools/play.py --sound pink --volume 0.2 --seconds 15
        Plays for 15 seconds, then stops.

Camera details come from camera.local.json beside this project's README -
see camera.local.example.json - so no password goes on the command line or
into shell history. That file is git-ignored.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The talk package, without the Home Assistant integration around it.
sys.path.insert(0, str(ROOT / "custom_components" / "camera_noise"))

from talk import tapo  # noqa: E402
from talk.noise import SOUNDS, SoothingNoise  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(ROOT / "camera.local.json"))
    parser.add_argument("--check", action="store_true", help="log in only; play nothing")
    parser.add_argument("--sound", choices=SOUNDS, default="white")
    parser.add_argument("--volume", type=float, default=0.2, help="0.0 to 1.0 (default 0.2)")
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    camera = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
    host = camera["host"]
    cloud_password = camera.get("tapo_cloud_password")
    if not cloud_password:
        print("Only Tapo cameras for now: set tapo_cloud_password in", args.config)
        return 2

    print(f"{host}: Tapo stream server {'found' if tapo.looks_like_tapo(host) else 'NOT found'} on port {tapo.PORT}")

    if args.check:
        error = tapo.check_cloud_password(host, cloud_password)
        print("Login accepted." if error is None else f"Login failed - {error}")
        return 0 if error is None else 1

    noise = SoothingNoise(sound=args.sound, volume=args.volume)
    done = threading.Event()
    outcome: dict = {}

    def on_playing() -> None:
        outcome["playing_at"] = time.monotonic()
        print(f"Playing {args.sound} at volume {args.volume} for {args.seconds:g}s...")

    def on_stopped(error) -> None:
        outcome["error"] = error
        done.set()

    talk = tapo.TapoTalkback(host, cloud_password, noise.read, on_playing, on_stopped)
    talk.start()
    try:
        # Up to ten seconds to connect, then the requested time counted from
        # when sound actually started.
        connect_by = time.monotonic() + 10
        while not done.is_set():
            started = outcome.get("playing_at")
            if started is None and time.monotonic() > connect_by:
                print("Gave up waiting for the camera.")
                break
            if started is not None and time.monotonic() - started >= args.seconds:
                break
            done.wait(0.2)
    except KeyboardInterrupt:
        print("Interrupted.")
    talk.stop()
    talk.join(5)

    error = outcome.get("error")
    if error is not None:
        print(f"Stopped with an error - {error}")
        return 1
    print("Stopped." if "playing_at" in outcome else "Never started playing.")
    return 0 if "playing_at" in outcome else 1


if __name__ == "__main__":
    sys.exit(main())
