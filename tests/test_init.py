"""The integration inside Home Assistant: adding a camera, and the entities.

The camera is faked throughout - the login checks and the transport - so
these make no sound and need no network.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.camera_noise.const import CONF_KIND, CONF_TAPO_CLOUD_PASSWORD, DOMAIN
from custom_components.camera_noise.talk.errors import Failure, TalkbackError

PLAYER = "media_player.nursery"
SWITCH = "switch.nursery_noise"

USER_INPUT = {CONF_NAME: "Nursery", CONF_HOST: "192.0.2.10", CONF_TAPO_CLOUD_PASSWORD: "secret"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Home Assistant only loads custom_components in tests when asked."""
    yield


class FakeTransport:
    made: list["FakeTransport"] = []

    def __init__(self, source, on_playing, on_stopped):
        self.source = source
        self.on_playing = on_playing
        self.on_stopped = on_stopped
        self.stopped = False
        FakeTransport.made.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_camera():
    FakeTransport.made = []
    with patch(
        "custom_components.camera_noise.factory_for",
        return_value=lambda source, on_playing, on_stopped: FakeTransport(source, on_playing, on_stopped),
    ):
        yield FakeTransport.made


async def _set_up(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nursery",
        unique_id="192.0.2.10",
        data={CONF_KIND: "tapo", CONF_HOST: "192.0.2.10", CONF_TAPO_CLOUD_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_adding_a_camera_checks_the_password_first(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    with (
        patch("custom_components.camera_noise.config_flow.tapo.looks_like_tapo", return_value=True),
        patch("custom_components.camera_noise.config_flow.tapo.check_cloud_password", return_value=None) as check,
        patch("custom_components.camera_noise.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nursery"
    assert result["data"] == {CONF_KIND: "tapo", CONF_HOST: "192.0.2.10", CONF_TAPO_CLOUD_PASSWORD: "secret"}
    check.assert_called_once_with("192.0.2.10", "secret")


async def test_a_refused_password_names_the_tapo_setting(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with (
        patch("custom_components.camera_noise.config_flow.tapo.looks_like_tapo", return_value=True),
        patch(
            "custom_components.camera_noise.config_flow.tapo.check_cloud_password",
            return_value=TalkbackError(Failure.NEEDS_THIRD_PARTY_ACCESS, "HTTP 401"),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "third_party"}


async def test_something_that_is_not_a_tapo_is_turned_away(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.camera_noise.config_flow.tapo.looks_like_tapo", return_value=False):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "not_tapo"}


async def test_play_volume_sound_and_stop(hass: HomeAssistant, fake_camera) -> None:
    await _set_up(hass)
    state = hass.states.get(PLAYER)
    assert state.state == "idle"
    assert hass.states.get(SWITCH).state == "off"

    await hass.services.async_call(
        "media_player", "volume_set", {"entity_id": PLAYER, "volume_level": 0.25}, blocking=True
    )
    await hass.services.async_call(
        "media_player", "select_source", {"entity_id": PLAYER, "source": "Ocean waves"}, blocking=True
    )
    await hass.services.async_call("media_player", "media_play", {"entity_id": PLAYER}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(PLAYER).state == "buffering"
    assert hass.states.get(SWITCH).state == "on"

    transport = fake_camera[0]
    noise = transport.source.__self__
    assert (noise.volume, noise.sound) == (0.25, "ocean")

    transport.on_playing()
    await hass.async_block_till_done()
    state = hass.states.get(PLAYER)
    assert state.state == "playing"
    assert state.attributes["volume_level"] == 0.25
    assert state.attributes["source"] == "Ocean waves"

    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    assert transport.stopped
    assert hass.states.get(PLAYER).state == "idle"
    assert hass.states.get(SWITCH).state == "off"


async def test_a_refusal_shows_on_the_player(hass: HomeAssistant, fake_camera) -> None:
    await _set_up(hass)
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    fake_camera[0].on_stopped(TalkbackError(Failure.NEEDS_THIRD_PARTY_ACCESS, "HTTP 401"))
    await hass.async_block_till_done()
    state = hass.states.get(PLAYER)
    assert state.state == "idle"
    assert state.attributes["last_error"] == "needs_third_party_access"


async def test_unloading_stops_the_noise(hass: HomeAssistant, fake_camera) -> None:
    entry = await _set_up(hass)
    await hass.services.async_call("media_player", "media_play", {"entity_id": PLAYER}, blocking=True)
    fake_camera[0].on_playing()
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_camera[0].stopped
