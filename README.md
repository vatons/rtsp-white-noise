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
RTSP stream. You give it the RTSP address you would use for the video
(for example `rtsp://192.168.1.20:554/h264Preview_01_main`) and the
camera's login. Setup asks the camera whether it has a back channel
before saving anything.

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
4. **Settings > Devices & services > Add integration > Camera Noise**,
   choose the kind of camera, and fill in its details.

Or copy `custom_components/camera_noise` into your Home Assistant
`config/custom_components` folder and restart.

## What you get

Each camera becomes a device with two entities:

- **A media player** - play, stop, volume, and the sound as its source.
  It shows as idle rather than off while quiet, so the volume can be set
  before the noise starts. Pause stops.
- **A Noise switch** - on and off, for automations and voice.

Volume and sound are remembered across restarts. Whether it was playing is
not: a restart never starts noise in a room on its own.

If the camera drops out while playing - a reboot, a WiFi blip - it keeps
trying to reconnect (after 1, 2, 5, 10, then every 30 seconds) until it is
stopped. A refused password is not retried: Tapo locks out repeated failed
logins.

### A bedtime automation

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
      - action: switch.turn_on
        target: { entity_id: switch.nursery_noise }
      # A sleep timer: off again after an hour.
      - delay: "01:00:00"
      - action: switch.turn_off
        target: { entity_id: switch.nursery_noise }
```

## Limits

- **One sender at a time.** A camera's speaker takes one talk session.
  While another app plays through it, this cannot, and the other way round.
- **Local network only.** Home Assistant has to be able to reach the camera.

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
