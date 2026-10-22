"""The integration inside Home Assistant: adding a camera, and the entities.

The camera is faked throughout - the checks and the transport - so these
make no sound and need no network.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.camera_noise.const import CONF_KIND, CONF_STREAM_URL, CONF_TAPO_CLOUD_PASSWORD, DOMAIN
from custom_components.camera_noise.diagnostics import async_get_config_entry_diagnostics
from custom_components.camera_noise.talk import onvif_device
from custom_components.camera_noise.talk.errors import Failure, TalkbackError

PLAYER = "media_player.nursery"
SWITCH = "switch.nursery_noise"
TIMER = "number.nursery_sleep_timer"
FLOW = "custom_components.camera_noise.config_flow"
SETUP = "custom_components.camera_noise.async_setup_entry"

MAIN = "rtsp://192.0.2.20:554/Preview_01_main"
SUB = "rtsp://192.0.2.20:554/Preview_01_sub"


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


async def _start(hass: HomeAssistant, address: str, name: str = "Nursery", tapo: bool = False):
    """The first step, with the is-it-a-Tapo question answered."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    with patch(f"{FLOW}.tapo.looks_like_tapo", return_value=tapo):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: name, "address": address}
        )


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


# --- Telling a Tapo from anything else ---------------------------------------

async def test_a_tapo_is_recognised_and_asked_for_its_account_password(hass: HomeAssistant) -> None:
    result = await _start(hass, "192.0.2.10", tapo=True)
    assert result["step_id"] == "tapo"
    with (
        patch(f"{FLOW}.tapo.check_cloud_password", return_value=None) as check,
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TAPO_CLOUD_PASSWORD: "secret"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Nursery"
    assert result["data"] == {CONF_KIND: "tapo", CONF_HOST: "192.0.2.10", CONF_TAPO_CLOUD_PASSWORD: "secret"}
    check.assert_called_once_with("192.0.2.10", "secret")


async def test_a_refused_tapo_password_names_the_tapo_setting(hass: HomeAssistant) -> None:
    result = await _start(hass, "192.0.2.10", tapo=True)
    with patch(
        f"{FLOW}.tapo.check_cloud_password",
        return_value=TalkbackError(Failure.NEEDS_THIRD_PARTY_ACCESS, "HTTP 401"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TAPO_CLOUD_PASSWORD: "wrong"}
        )
    assert result["step_id"] == "tapo"
    assert result["errors"] == {"base": "third_party"}


async def test_anything_else_is_asked_for_its_own_login(hass: HomeAssistant) -> None:
    result = await _start(hass, "192.0.2.20", name="Front door")
    assert result["step_id"] == "login"


# --- Any other camera --------------------------------------------------------

async def test_given_an_ip_it_finds_the_stream_with_a_back_channel(hass: HomeAssistant) -> None:
    result = await _start(hass, "192.0.2.20", name="Front door")
    streams = [onvif_device.Stream(MAIN, 2560, 1920), onvif_device.Stream(SUB, 640, 480)]
    # Some cameras offer the back channel on only one of their streams.
    probes = {MAIN: TalkbackError(Failure.UNSUPPORTED, "none"), SUB: None}
    with (
        patch(f"{FLOW}.onvif_device.find_streams", return_value=streams) as find,
        patch(f"{FLOW}.onvif.probe", side_effect=lambda url, user, password: probes[url]),
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "pw"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_KIND: "onvif", CONF_STREAM_URL: SUB, CONF_USERNAME: "admin", CONF_PASSWORD: "pw"}
    find.assert_called_once_with("192.0.2.20", "admin", "pw")


async def test_a_typed_rtsp_address_is_used_as_given(hass: HomeAssistant) -> None:
    result = await _start(hass, "rtsp://admin:pw@192.0.2.20:554/Preview_01_main", name="Front door")
    assert result["step_id"] == "login"
    with (
        patch(f"{FLOW}.onvif_device.find_streams") as find,
        patch(f"{FLOW}.onvif.probe", return_value=None) as probe,
        patch(SETUP, return_value=True),
    ):
        # The login written into the address stands when the fields are left empty.
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_KIND: "onvif", CONF_STREAM_URL: MAIN, CONF_USERNAME: "admin", CONF_PASSWORD: "pw"}
    probe.assert_called_once_with(MAIN, "admin", "pw")
    find.assert_not_called()


async def test_when_the_camera_cannot_say_it_asks_for_the_address(hass: HomeAssistant) -> None:
    result = await _start(hass, "192.0.2.20", name="Front door")
    with patch(f"{FLOW}.onvif_device.find_streams", side_effect=TalkbackError(Failure.UNREACHABLE, "no ONVIF")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "pw"}
        )
    assert result["step_id"] == "login"
    assert result["errors"] == {"base": "no_stream_address"}
    assert CONF_STREAM_URL in result["data_schema"].schema

    with (
        patch(f"{FLOW}.onvif.probe", return_value=None),
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "pw", CONF_STREAM_URL: MAIN}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_STREAM_URL] == MAIN


async def test_a_refused_login_says_so(hass: HomeAssistant) -> None:
    result = await _start(hass, "192.0.2.20")
    with patch(f"{FLOW}.onvif_device.find_streams", side_effect=TalkbackError(Failure.REJECTED, "401")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"}
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_a_camera_with_no_back_channel_says_so(hass: HomeAssistant) -> None:
    result = await _start(hass, "192.0.2.20")
    with (
        patch(f"{FLOW}.onvif_device.find_streams", return_value=[onvif_device.Stream(MAIN)]),
        patch(f"{FLOW}.onvif.probe", return_value=TalkbackError(Failure.UNSUPPORTED, "none")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "pw"}
        )
    assert result["errors"] == {"base": "no_backchannel"}


async def test_an_address_that_is_neither_is_refused(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "x", "address": "http://192.0.2.20/"}
    )
    assert result["errors"] == {"address": "invalid_address"}


async def test_a_camera_already_added_is_not_added_twice(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="192.0.2.10", data={}).add_to_hass(hass)
    result = await _start(hass, "192.0.2.10", tapo=True)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- Reconfigure -------------------------------------------------------------

async def test_reconfiguring_a_tapo_keeps_a_blank_password(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nursery",
        unique_id="192.0.2.10",
        data={CONF_KIND: "tapo", CONF_HOST: "192.0.2.10", CONF_TAPO_CLOUD_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with (
        patch(f"{FLOW}.tapo.looks_like_tapo", return_value=True),
        patch(f"{FLOW}.tapo.check_cloud_password", return_value=None) as check,
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "192.0.2.11"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    check.assert_called_once_with("192.0.2.11", "secret")
    assert entry.data[CONF_HOST] == "192.0.2.11"
    assert entry.unique_id == "192.0.2.11"


async def test_reconfiguring_any_camera_checks_the_new_details(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Front door",
        unique_id="192.0.2.20",
        data={CONF_KIND: "onvif", CONF_STREAM_URL: MAIN, CONF_USERNAME: "admin", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with patch(f"{FLOW}.onvif.probe", return_value=TalkbackError(Failure.REJECTED, "401")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_STREAM_URL: MAIN, CONF_USERNAME: "admin", CONF_PASSWORD: "typo"}
        )
    assert result["errors"] == {"base": "invalid_auth"}
    with (
        patch(f"{FLOW}.onvif.probe", return_value=None),
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_STREAM_URL: MAIN, CONF_USERNAME: "admin", CONF_PASSWORD: "new"}
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new"


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


# --- The sleep timer ---------------------------------------------------------

def _stop_at(hass: HomeAssistant):
    value = hass.states.get(PLAYER).attributes["stop_at"]
    return dt_util.parse_datetime(value) if value else None


async def test_the_sleep_timer_stops_the_noise(hass: HomeAssistant, fake_camera) -> None:
    await _set_up(hass)
    await hass.services.async_call("number", "set_value", {"entity_id": TIMER, "value": 60}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    fake_camera[0].on_playing()
    await hass.async_block_till_done()
    stop_at = _stop_at(hass)
    assert timedelta(minutes=59) < stop_at - dt_util.utcnow() <= timedelta(minutes=60)

    async_fire_time_changed(hass, stop_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert fake_camera[0].stopped
    assert hass.states.get(PLAYER).state == "idle"
    assert _stop_at(hass) is None


async def test_the_sleep_timer_changed_while_playing_counts_from_then(hass: HomeAssistant, fake_camera) -> None:
    await _set_up(hass)
    await hass.services.async_call("media_player", "media_play", {"entity_id": PLAYER}, blocking=True)
    await hass.async_block_till_done()
    assert _stop_at(hass) is None

    await hass.services.async_call("number", "set_value", {"entity_id": TIMER, "value": 30}, blocking=True)
    await hass.async_block_till_done()
    assert timedelta(minutes=29) < _stop_at(hass) - dt_util.utcnow() <= timedelta(minutes=30)

    await hass.services.async_call("number", "set_value", {"entity_id": TIMER, "value": 0}, blocking=True)
    await hass.async_block_till_done()
    assert _stop_at(hass) is None
    assert hass.states.get(PLAYER).state == "buffering"


async def test_stopping_by_hand_cancels_the_sleep_timer(hass: HomeAssistant, fake_camera) -> None:
    await _set_up(hass)
    await hass.services.async_call("number", "set_value", {"entity_id": TIMER, "value": 60}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    first = _stop_at(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    # Started again with no timer: the first one must not stop it.
    await hass.services.async_call("number", "set_value", {"entity_id": TIMER, "value": 0}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, first + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "on"


# --- Restarts ----------------------------------------------------------------

async def test_noise_playing_before_a_restart_plays_again(hass: HomeAssistant, fake_camera) -> None:
    mock_restore_cache(hass, [State(PLAYER, "playing", {"volume_level": 0.25, "source": "Ocean waves"})])
    await _set_up(hass)
    assert hass.states.get(PLAYER).state == "buffering"
    assert hass.states.get(SWITCH).state == "on"
    noise = fake_camera[0].source.__self__
    assert (noise.volume, noise.sound) == (0.25, "ocean")


async def test_noise_off_before_a_restart_stays_off(hass: HomeAssistant, fake_camera) -> None:
    mock_restore_cache(hass, [State(PLAYER, "idle", {"volume_level": 0.25, "source": "Ocean waves"})])
    await _set_up(hass)
    state = hass.states.get(PLAYER)
    assert state.state == "idle"
    assert (state.attributes["volume_level"], state.attributes["source"]) == (0.25, "Ocean waves")
    assert fake_camera == []


async def test_a_restart_resumes_until_the_sleep_timer_it_had(hass: HomeAssistant, fake_camera) -> None:
    stop_at = dt_util.utcnow() + timedelta(minutes=20)
    mock_restore_cache(hass, [State(PLAYER, "playing", {"stop_at": stop_at.isoformat()})])
    await _set_up(hass)
    assert hass.states.get(PLAYER).state == "buffering"
    assert _stop_at(hass) == stop_at

    async_fire_time_changed(hass, stop_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert fake_camera[0].stopped
    assert hass.states.get(PLAYER).state == "idle"


async def test_a_sleep_timer_that_ran_out_during_the_restart_leaves_it_off(hass: HomeAssistant, fake_camera) -> None:
    stop_at = dt_util.utcnow() - timedelta(minutes=5)
    mock_restore_cache(hass, [State(PLAYER, "playing", {"stop_at": stop_at.isoformat()})])
    await _set_up(hass)
    assert hass.states.get(PLAYER).state == "idle"
    assert fake_camera == []


async def test_the_sleep_timer_setting_is_remembered(hass: HomeAssistant, fake_camera) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(TIMER, "45"),
                {
                    "native_value": 45,
                    "native_unit_of_measurement": "min",
                    "native_min_value": 0,
                    "native_max_value": 720,
                    "native_step": 1,
                },
            )
        ],
    )
    await _set_up(hass)
    assert float(hass.states.get(TIMER).state) == 45


async def test_home_assistant_stopping_ends_the_session_but_keeps_it_for_the_restart(
    hass: HomeAssistant, fake_camera
) -> None:
    await _set_up(hass)
    await hass.services.async_call("media_player", "media_play", {"entity_id": PLAYER}, blocking=True)
    fake_camera[0].on_playing()
    await hass.async_block_till_done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert fake_camera[0].stopped
    # What is saved for after the restart - so the noise comes back.
    assert hass.states.get(PLAYER).state == "playing"
    assert hass.states.get(SWITCH).state == "on"


# --- A refused login ---------------------------------------------------------

async def test_a_refused_tapo_login_asks_for_it_again(hass: HomeAssistant, fake_camera) -> None:
    entry = await _set_up(hass)
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    fake_camera[0].on_stopped(TalkbackError(Failure.NEEDS_THIRD_PARTY_ACCESS, "HTTP 401"))
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [config_entries.SOURCE_REAUTH]

    flow_id = flows[0]["flow_id"]
    with patch(
        f"{FLOW}.tapo.check_cloud_password",
        return_value=TalkbackError(Failure.NEEDS_THIRD_PARTY_ACCESS, "HTTP 401"),
    ) as check:
        # Blank tries the saved password - for when it was the Tapo setting.
        result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["errors"] == {"base": "third_party"}
    check.assert_called_once_with("192.0.2.10", "secret")

    with patch(f"{FLOW}.tapo.check_cloud_password", return_value=None) as check:
        result = await hass.config_entries.flow.async_configure(flow_id, {CONF_TAPO_CLOUD_PASSWORD: "new"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    check.assert_called_once_with("192.0.2.10", "new")
    assert entry.data[CONF_TAPO_CLOUD_PASSWORD] == "new"


async def test_a_dropout_does_not_ask_for_the_login(hass: HomeAssistant, fake_camera) -> None:
    await _set_up(hass)
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    fake_camera[0].on_stopped(TalkbackError(Failure.UNREACHABLE, "timed out"))
    await hass.async_block_till_done()
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_reauthenticating_any_camera_checks_the_new_login(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Front door",
        unique_id="192.0.2.20",
        data={CONF_KIND: "onvif", CONF_STREAM_URL: MAIN, CONF_USERNAME: "admin", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with (
        patch(f"{FLOW}.onvif.probe", return_value=None) as probe,
        patch(SETUP, return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "new"}
        )
    assert result["reason"] == "reauth_successful"
    probe.assert_called_once_with(MAIN, "admin", "new")
    assert entry.data[CONF_PASSWORD] == "new"


# --- Diagnostics -------------------------------------------------------------

async def test_diagnostics_leave_out_the_password(hass: HomeAssistant, fake_camera) -> None:
    entry = await _set_up(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"][CONF_TAPO_CLOUD_PASSWORD] == "**REDACTED**"
    assert diagnostics["entry"][CONF_HOST] == "192.0.2.10"
    assert diagnostics["session"]["state"] == "off"
