"""What to attach to a bug report: the camera's settings with its passwords
taken out, and what its session is doing.

Downloaded from the camera's page in Settings > Devices & services.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import CameraNoiseConfigEntry
from .const import CONF_TAPO_CLOUD_PASSWORD

# Stream addresses are saved without their login, so the passwords are all.
TO_REDACT = {CONF_PASSWORD, CONF_TAPO_CLOUD_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CameraNoiseConfigEntry
) -> dict[str, Any]:
    camera = entry.runtime_data
    session = camera.session
    error = session.last_error
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "session": {
            "state": session.state.value,
            "sound": session.sound,
            "volume": session.volume,
            "last_error": error.failure.value if error else None,
            "last_error_detail": str(error) if error else None,
        },
        "sleep_timer": {
            "minutes": camera.sleep_minutes,
            "stop_at": camera.stop_at.isoformat() if camera.stop_at else None,
        },
    }
