# Tube Display

Renders information from Home Assistant into pictures and pushes them to an
IPSTube clock — six 135x240 colour IPS displays in a row on an ESP32 — so the
clock can show dashboards, gauges, camera snapshots and more, rotating
between "screens" you define in YAML.

The clock must run the **panels firmware** (the firmware that accepts pushed
pictures on `POST /panel`); the stock time/date/weather firmware does not.

## How it works

1. You define screens in `/config/screens.yaml` (a commented example is
   written there on first start). Each screen is either six widgets — one per
   tube, left to right — or one 810x240 "strip" picture across all tubes.
2. The add-on renders the active screen to an 810x240 image, converts it to
   RGB565 and POSTs each 135x240 tube to `http://<clock>/panel?tube=N`.
   Only tubes whose picture changed are re-sent; tubes the clock reports as
   lost (`panels_needed` over MQTT, e.g. after the screen saver) are re-sent
   immediately.
3. A rotation cycles through the screens in `rotation:`, honouring each
   screen's `duration`, `refresh` and optional `when` condition. The special
   rotation entry `clock` hands the tubes back to the clock's own time
   display.
4. Home Assistant automations can interrupt the rotation at any time via the
   HTTP API (`POST /api/show`), and two MQTT entities (a screen select and a
   rotation switch) appear in HA under the device "Tube Display".

See [DOCS.md](DOCS.md) for the full reference: options, the screens.yaml
format with every widget, the HTTP API, the MQTT entities and an example
automation.

## Requirements

- An IPSTube clock on the network running the panels firmware, and its MQTT
  id (shown on the clock's web page).
- The MQTT broker service enabled in Home Assistant (the add-on uses the
  broker the Supervisor injects; no credentials are stored in the add-on).
- `homeassistant_api: true` (already set) so templates, history and camera
  snapshots can be fetched through the Supervisor.
