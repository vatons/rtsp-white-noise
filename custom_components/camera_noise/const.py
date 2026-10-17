"""Constants for Camera Noise."""

from __future__ import annotations

from .talk.noise import OCEAN, PINK, WHITE

DOMAIN = "camera_noise"

CONF_KIND = "kind"
CONF_TAPO_CLOUD_PASSWORD = "tapo_cloud_password"

# The sound names as Home Assistant shows them, in the order offered.
SOUND_LABELS = {
    WHITE: "White noise",
    PINK: "Pink noise",
    OCEAN: "Ocean waves",
}
