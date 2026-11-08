# Tube Display — full reference

Renders Home Assistant information into pictures and pushes them to an IPSTube
clock (six 135x240 colour IPS displays in a row, ESP32, custom firmware) that
runs the **panels firmware** — the firmware that accepts pushed pictures on
`POST http://<clock>/panel?tube=N`. The stock time/date/weather firmware does
not accept panels.

## Options

| Option | Default | Meaning |
|---|---|---|
| `clock_host` | `192.168.8.211` | Clock host or IP (no `http://` prefix). |
| `clock_mqtt_id` | `209374ipstube` | The `<id>` in the clock's MQTT topics (`clock/<id>/...`), shown on the clock's web page. |
| `clock_user` / `clock_password` | empty | Optional HTTP digest auth credentials if the clock has a web login. |
| `default_duration` | `15` | Seconds a screen stays in the rotation when it doesn't set its own `duration`. |
| `default_refresh` | `30` | Seconds between re-renders of the shown screen when it doesn't set its own `refresh`. |
| `log_level` | `info` | `debug` \| `info` \| `warning` \| `error`. |

The screens file lives in your Home Assistant config folder at
`tube_display/screens.yaml` (File editor, Samba or VS Code), set by the
`screens_file` option. A commented example is written on first start if the file is
missing, and the file is **re-read automatically when it changes** — no
restart needed. A screen with an error is skipped (the error is logged and
shown in `GET /api/status` under `config_errors`); the other screens keep
working.

## The screens file

```yaml
rotation: [claude, doorbell, clock]   # screen names, in order; "clock" is special
clock: {duration: 60}                 # duration of the "clock" rotation entry

screens:
  claude:
    duration: 20                      # optional, default_duration
    refresh: 30                       # optional, default_refresh
    when: "{{ is_state('binary_sensor.x','on') }}"   # optional; skipped unless "true"
    background: "#000000"
    tubes:                            # exactly 6 widgets, left to right (tube 0..5)
      - {type: image, source: "builtin:claude"}
      - {type: ring, value: "{{ states('sensor.session_usage') }}", max: 100,
         label: "Session", unit: "%", color: "#D97757"}
      - {type: ring, value: "{{ states('sensor.week_usage') }}", max: 100,
         label: "Week", unit: "%", color: "#D97757"}
      - {type: text, text: "Resets\n{{ states('sensor.resets') }}", size: auto,
         color: "#FFFFFF", align: center}
      - {type: big_number, value: "{{ states('sensor.pace') }}", unit: "%", label: "Pace"}
      - {type: icon, icon: "mdi:robot", text: "{{ states('sensor.status') }}", color: "#D97757"}

  doorbell:
    strip: {source: "camera:camera.front_door", fit: cover}  # one 810x240 picture
```

A screen is either `tubes:` (exactly 6 widgets) or `strip:` (one picture
across all six tubes, `fit: cover` crops to fill, `contain` letterboxes).

All string fields may be Home Assistant templates (`{{ ... }}`), rendered
concurrently through the Supervisor's API (`POST /api/template`). Colours
accept `#RRGGBB` (or `#RGB`) or a template returning one. If a template fails,
that widget shows a red `!` and the error is logged once per change, not on
every refresh.

### Widgets

| Widget | Fields |
|---|---|
| `text` | `text` (multi-line with `\n`), `size: auto` fits the tube (or a number), `align: left\|center\|right`, `weight: bold`, `color` |
| `big_number` | `value`, `unit`, `label`, `color`, `unit_newline` — auto-sized to fit; `unit_newline: true` puts the unit on its own line so the value can be bigger |
| `ring` | `value`, `max`, `label`, `unit`, `color` — circular gauge 0..max, value in the middle, label below; `unit_newline: true` puts the unit under the value |
| `bar` | `value`, `max`, `label`, `unit`, `color` — vertical bar gauge |
| `icon` | `icon: mdi:<name>` (Material Design Icons), optional `text` below, `color` |
| `image` | `source`: `builtin:claude` (procedural orange starburst), an `http(s)://` URL, `camera:<entity_id>` (HA camera proxy snapshot), `entity_picture:<entity_id>`; `fit: cover\|contain` |
| `sparkline` | `entity`, `hours` (HA history API), optional `unit`, `color` — line graph with the latest value on top |
| `blank` | empty tube |

## Pushing to the clock

- `POST http://<clock>/panel?tube=N` with raw RGB565 big-endian body
  (w*h*2 bytes, default full tube 135x240). Tubes 0..5 left to right.
- The add-on keeps the last bytes sent per tube and only POSTs tubes whose
  bytes changed (hash), plus any tube in the clock's `panels_needed` bitmask
  (from `clock/<id>/volatile/state` over MQTT) — those are re-sent
  immediately, even if unchanged (e.g. after the screen saver).
- When a screen becomes active the add-on publishes `display=4` to
  `clock/<id>/set/display` (unless the clock's state already says `display: 4`),
  then sends all six tubes. When the rotation reaches `clock`, it publishes
  `display=0` and stops sending.
- HTTP `409` (not a panel tube) → re-publish `display=4` once and retry.
  `503` / connection errors → retry with backoff capped at ~10 s. Tubes are
  sent one at a time; the loop never crashes on clock errors.
- `GET http://<clock>/panel` returns `{"tubes": [...], "needed": [...],
  "width": 135, "height": 240}` and is used for status.

## HTTP API (port 8098)

The add-on listens on `0.0.0.0:8098` inside the container, published to the
host on port 8098.

| Endpoint | Description |
|---|---|
| `GET /api/status` | JSON: current screen, rotation list, rotation enabled, pinned screen, screen names, config errors, last error, last clock state seen over MQTT. |
| `POST /api/show` | Body `{"screen": "doorbell", "seconds": 30}` — interrupt the rotation with that screen for N seconds, then resume. `{"screen": "clock", ...}` hands the tubes to the clock. Unknown screen → 404. |
| `POST /api/rotation` | Body `{"enabled": false}` — pause the rotation (stays on the current screen); `true` resumes. |
| `GET /preview/<screen>.png` | The rendered 810x240 PNG of a screen, for checking layouts without a clock. `?scale=2` zooms (1..8). |

### Example automation: show the doorbell camera when it rings

Add a `rest_command` once (Home Assistant has no API for creating these):

```yaml
rest_command:
  tube_display_show:
    url: "http://<addon-hostname>:8098/api/show"
    method: POST
    content_type: "application/json"
    payload: '{"screen": "doorbell", "seconds": 30}'
```

`<addon-hostname>` is the add-on's internal hostname, `{REPO_HASH}-tube-display`
(for this repository the hash is `beb500c8`, so `beb500c8-tube-display`; from
the LAN you can also use the host's IP with the published port 8098).

Then the automation:

```yaml
automation:
  - alias: "Doorbell rings -> show front door on the clock"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell
        to: "on"
    action:
      - service: rest_command.tube_display_show
```

## MQTT entities (device "Tube Display")

Published via MQTT discovery, using the broker credentials the Supervisor
injects (no passwords in the add-on):

- **select "Tube Display Screen"** — options: `Rotate`, `Clock`, plus every
  screen name. Selecting a screen pins it until `Rotate` is chosen again;
  `Clock` hands the tubes to the clock's time display.
- **switch "Tube Display Rotation"** — on/off; off pauses the rotation on the
  current screen.

Discovery is republished when `screens.yaml` changes. The add-on also
subscribes to `clock/<id>/volatile/state` (for `display`, `panel_tubes`,
`panels_needed`) and publishes display-mode commands to
`clock/<id>/set/display`.

## Development

Tests (no network, no real clock; Pillow-dependent tests skip automatically
when Pillow isn't importable, icon tests skip when the MDI font is absent):

```
cd tube_display/app
pip install --break-system-packages -r requirements-dev.txt   # if needed
python3 -m pytest -q
```
