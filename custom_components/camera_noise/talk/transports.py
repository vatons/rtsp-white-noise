"""Which way to reach a camera's speaker, from what is known about it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .session import TransportFactory
from .tapo import TapoTalkback

KIND_TAPO = "tapo"


def factory_for(camera: Mapping[str, Any]) -> TransportFactory:
    """A transport factory for `camera`: kind, host and its credentials."""
    kind = camera.get("kind", KIND_TAPO)
    if kind == KIND_TAPO:
        host = camera["host"]
        cloud_password = camera["tapo_cloud_password"]
        return lambda source, on_playing, on_stopped: TapoTalkback(
            host, cloud_password, source, on_playing, on_stopped
        )
    raise ValueError(f"Unknown camera kind: {kind}")
