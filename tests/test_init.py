"""The integration inside Home Assistant: adding a camera, and the entities.

The camera is faked throughout - the checks and the transport - so these
make no sound and need no network.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.camera_noise.const import CONF_KIND, CONF_STREAM_URL, CONF_TAPO_CLOUD_PASSWORD, DOMAIN
from custom_components.camera_noise.talk.errors import Failure, TalkbackError

PLAYER = "media_player.nursery"
SWITCH = "switch.nursery_noise"

TAPO_INPUT = {CONF_NAME: "Nursery", CONF_HOST: "192.0.2.10", CONF_TAPO_CLOUD_PASSWORD: "secret"}
ONVIF_INPUT = {
    CONF_NAME: "Front door",
    CONF_STREAM_URL: "rtsp://admin:pw@192.0.2.20:554/h264Preview_01_main",
    CONF_USERNAME: "",
}
FLOW = "custom_components.camera_noise.config_flow"


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


async def _form(hass: HomeAssistant, kind: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["onvif", "tapo"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": kind})
    assert result["type"] is FlowResultType.FORM
    return result


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


# --- Adding an ONVIF camera --------------------------------------------------

async def test_adding_any_camera_checks_for_a_back_channel(hass: HomeAssistant) -> None:
    result = await _form(hass, "onvif")
    with (
        patch(f"{FLOW}.onvif.probe", return_value=None) as probe,
        patch("custom_components.camera_noise.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], ONVIF_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Front door"
    # The login written into the address is used, and stored apart from it.
    assert result["data"] == {
        CONF_KIND: "onvif",
        CONF_STREAM_URL: "rtsp://192.0.2.20:554/h264Preview_01_main",
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw",
    }
    probe.assert_called_once_with("rtsp://192.0.2.20:554/h264Preview_01_main", "admin", "pw")


async def test_a_tapo_entered_as_any_camera_is_sent_to_the_tapo_form(hass: HomeAssistant) -> None:
    result = await _form(hass, "onvif")
    with (
        patch(f"{FLOW}.onvif.probe", return_value=TalkbackError(Failure.UNSUPPORTED, "no back channel")),
        patch(f"{FLOW}.tapo.looks_like_tapo", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], ONVIF_INPUT)
    assert result["errors"] == {"base": "is_tapo"}


async def test_a_camera_with_no_back_channel_says_so(hass: HomeAssistant) -> None:
    result = await _form(hass, "onvif")
    with (
        patch(f"{FLOW}.onvif.probe", return_value=TalkbackError(Failure.UNSUPPORTED, "no back channel")),
        patch(f"{FLOW}.tapo.looks_like_tapo", return_value=False),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], ONVIF_INPUT)
    assert result["errors"] == {"base": "no_backchannel"}


async def test_a_refused_camera_login_says_so(hass: HomeAssistant) -> None:
    result = await _form(hass, "onvif")
    with patch(f"{FLOW}.onvif.probe", return_value=TalkbackError(Failure.REJECTED, "401")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], ONVIF_INPUT)
    assert result["errors"] == {"base": "invalid_auth"}


async def test_something_that_is_not_rtsp_is_refused(hass: HomeAssistant) -> None:
    result = await _form(hass, "onvif")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ONVIF_INPUT, CONF_STREAM_URL: "http://192.0.2.20/"}
    )
    assert result["errors"] == {CONF_STREAM_URL: "invalid_url"}


# --- Adding a Tapo -----------------------------------------------------------

async def test_adding_a_tapo_checks_the_password_first(hass: HomeAssistant) -> None:
    result = await _form(hass, "tapo")
    with (
        patch(f"{FLOW}.tapo.looks_like_tapo", return_value=True),
        patch(f"{FLOW}.tapo.check_cloud_password", return_value=None) as check,
        patch("custom_components.camera_noise.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], TAPO_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_KIND: "tapo", CONF_HOST: "192.0.2.10", CONF_TAPO_CLOUD_PASSWORD: "secret"}
    check.assert_called_once_with("192.0.2.10", "secret")


async def test_a_refused_tapo_password_names_the_tapo_setting(hass: HomeAssistant) -> None:
    result = await _form(hass, "tapo")
    with (
        patch(f"{FLOW}.tapo.looks_like_tapo", return_value=True),
        patch(
            f"{FLOW}.tapo.check_cloud_password",
            return_value=TalkbackError(Failure.NEEDS_THIRD_PARTY_ACCESS, "HTTP 401"),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], TAPO_INPUT)
    assert result["errors"] == {"base": "third_party"}


async def test_something_that_is_not_a_tapo_is_turned_away(hass: HomeAssistant) -> None:
    result = await _form(hass, "tapo")
    with patch(f"{FLOW}.tapo.looks_like_tapo", return_value=False):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], TAPO_INPUT)
    assert result["errors"] == {"base": "not_tapo"}


# --- The entities ------------------------------------------------------------

async def test_play_volume_sound_and_stop(hass: HomeAssistant, fake_camera) -> None:
    await _set_up(hass)
    assert hass.states.get(PLAYER).state == "idle"
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
