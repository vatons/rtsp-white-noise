"""Which way to reach a camera's speaker, from what is known about it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .onvif import OnvifTalkback
from .session import TransportFactory
from .tapo import TapoTalkback

# The ONVIF back channel: the standard, on the camera's RTSP stream.
KIND_ONVIF = "onvif"
# Tapo's own channel on port 8800, for cameras with no back channel.
KIND_TAPO = "tapo"


def factory_for(camera: Mapping[str, Any]) -> TransportFactory:
    """A transport factory for `camera` - its kind, address and credentials."""
    kind = camera.get("kind", KIND_TAPO)
    if kind == KIND_TAPO:
        host = camera["host"]
        cloud_password = camera["tapo_cloud_password"]
        return lambda source, on_playing, on_stopped: TapoTalkback(
            host, cloud_password, source, on_playing, on_stopped
        )
    if kind == KIND_ONVIF:
        url = camera["stream_url"]
        username = camera.get("username") or None
        password = camera.get("password") or None
        return lambda source, on_playing, on_stopped: OnvifTalkback(
            url, username, password, source, on_playing, on_stopped
        )
    raise ValueError(f"Unknown camera kind: {kind}")
