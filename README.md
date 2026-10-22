# Camera Noise

A Home Assistant integration that plays white noise, pink noise or ocean
waves through a camera's own speaker - for settling a baby with the
nursery camera you already have, controlled from Home Assistant on your
local network.

The sound is generated, not streamed from a file. These speakers take
G.711 audio at 8 kHz - telephone quality - and noise is the one thing that
loses nothing at that quality.

## Cameras

Nearly any camera with a speaker that can be reached on your network, two
ways:

**Most cameras with two-way audio** - Reolink, Dahua, Amcrest and many
others - through the standard ONVIF *back channel*, on the camera's own
RTSP stream.

**Tapo (TP-Link)** cameras, which have no back channel and use their own
channel on port 8800 instead - the same one go2rtc uses. Tapo needs:

- Your **TP-Link account password**. It is hashed and checked by the
  camera on your network; nothing is sent to TP-Link.
- **Third-Party Compatibility** switched on in the Tapo app, under
  **Me > Tapo Lab**. Without it the camera refuses even the right password.

A Tapo takes about five seconds to accept its login - that is the camera,
not the network - so its noise starts a few seconds after you press play.

What will not work: cameras that only talk to their maker's cloud (Ring,
Nest and the like), and cameras whose speaker is only reachable through a
proprietary API rather than the back channel - some Hikvision models, for
one. Setup says so when a camera has no back channel to offer.

## Install

Through HACS, as a custom repository:

1. HACS > the three-dot menu > **Custom repositories**.
2. Add this repository's URL, type **Integration**.
3. Install **Camera Noise**, then restart Home Assistant.
4. **Settings > Devices & services > Add integration > Camera Noise**.

Everything is set up in the UI; there is no YAML. Setup asks for a name and
the camera's address, then works out what kind of camera it is and asks
only for what that needs:

- **A Tapo** (it answers on port 8800 - asked without a password): your
  TP-Link account password.
- **Anything else**: the camera's own username and password. Given just an
  IP, it asks the camera over ONVIF for its stream addresses and uses the
  first that has a back channel, so you never need the RTSP path. If the
  camera cannot say, setup asks you for it. You can also type an
  `rtsp://` address in the first place.

The camera is asked before anything is saved, and nothing makes a sound
while you set it up. **Reconfigure**, in the camera's menu, changes its
address or login later - leave a password blank to keep the saved one.

Or copy `custom_components/camera_noise` into your Home Assistant
`config/custom_components` folder and restart.

## What you get

Each camera becomes a device with three entities:

- **A media player** - play, stop, volume, and the sound as its source.
  It shows as idle rather than off while quiet, so the volume can be set
  before the noise starts. Pause stops. Its `stop_at` attribute says when
  the sleep timer will stop the noise.
- **A Noise switch** - on and off, for automations and voice.
- **A Sleep timer** - how many minutes the noise plays once it starts,
  however it is started; 0 plays it until it is stopped. Changed while the
  noise plays, it counts from then.

Volume, sound, the sleep timer and whether it was playing are remembered
across restarts: noise that was playing when Home Assistant stopped starts
again when it comes back - until the time its sleep timer had, or not at
all if that passed while Home Assistant was down - and noise that was off
stays off. After a power cut the camera can take longer to come back than
Home Assistant, so a camera that is not answering yet is retried, as below.

If the camera drops out while playing - a reboot, a WiFi blip - it keeps
trying to reconnect (after 1, 2, 5, 10, then every 30 seconds) until it is
stopped. A refused password is not retried: Tapo locks out repeated failed
logins. Home Assistant asks for the login again instead - the camera shows
**Reauthentication required** in Settings > Devices & services.

### A bedtime automation

Built in the automation editor like any other; in YAML, it is:

```yaml
automation:
  - alias: Nursery noise at bedtime
    triggers:
      - trigger: time
        at: "19:00:00"
    actions:
      - action: media_player.select_source
        target: { entity_id: media_player.nursery }
        data: { source: Pink noise }
      - action: media_player.volume_set
        target: { entity_id: media_player.nursery }
        data: { volume_level: 0.4 }
      # Off again after an hour.
      - action: number.set_value
        target: { entity_id: number.nursery_sleep_timer }
        data: { value: 60 }
      - action: switch.turn_on
        target: { entity_id: switch.nursery_noise }
```

Use the sleep timer rather than a `delay` and a turn-off: a restart cancels
an automation part-way through its delay, so the turn-off would never come
and the noise, resumed after the restart, would play on.

## Limits

- **One sender at a time.** A camera's speaker takes one talk session.
  While another app plays through it, this cannot, and the other way round.
- **Local network only.** Home Assistant has to be able to reach the camera.

For a bug report, **Download diagnostics** from the camera's menu in
Settings > Devices & services: its settings, with the passwords removed,
and what its session is doing.

## Development

The protocol code in `custom_components/camera_noise/talk` has no Home
Assistant in it, so it can be tried against a real camera from any
computer:

```
cp camera.local.example.json camera.local.json   # then fill it in
python tools/play.py --check                      # asks the camera, plays nothing
python tools/play.py --sound pink --volume 0.2 --seconds 15
python tools/play.py --config camera.door.local.json --check
```

A Tapo's file has `host` and `tapo_cloud_password`; any other camera's has
`stream_url`, `username` and `password`. Files named `camera*.local.json`
are git-ignored.

Tests: `python -m pytest` runs the protocol and session tests anywhere.
The integration tests need Home Assistant, which does not install on
Windows; in Docker:

```
docker build -t camera-noise-test - <<'EOF'
FROM python:3.13
RUN pip install pytest-homeassistant-custom-component
EOF
docker run --rm -v "$PWD:/app" -w /app camera-noise-test python -m pytest
```

The MPEG-TS tests check against bytes a real Tapo C120 accepted, and the
ONVIF tests run a whole session against a fake camera on localhost.

GitHub runs all of the tests on every push, along with Home Assistant's
`hassfest` and HACS's own checks - see `.github/workflows/validate.yml`.
Weekly too, so a Home Assistant release that breaks something shows up
before anyone updates to it.
