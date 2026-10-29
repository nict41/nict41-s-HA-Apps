# Pantry Tracker

![Pantry Tracker logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/pantry_tracker/logo.png)

Scan a grocery barcode with your phone, look up the product automatically,
snap a photo of its use-by date, and get notified before it goes off - all
from a phone-first ingress panel.

## How it works

1. **Scan** a barcode with a live, full-screen camera view that scans
   automatically as you point it at the barcode (via the browser's
   `BarcodeDetector` API - works out of the box on Android Chrome, no app
   permissions dance). The decoded barcode is looked up against
   [Open Food Facts](https://world.openfoodfacts.org) - a free, keyless,
   crowd-sourced product database - to pre-fill the product's name, brand,
   quantity and category - a barcode already scanned before is served
   instantly from a local cache instead of hitting Open Food Facts again.
   On a browser without live camera support, it falls back to the phone's
   own photo-capture UI and decodes the barcode (via zbar) on the add-on
   instead.
2. Optionally open the same live camera view to **photograph the use-by/
   best-before date** - this one has a shutter button, since reading a date
   needs a deliberate "now" moment rather than auto-detection. The photo is
   sent to a vision-capable model you run yourself (e.g.
   [Open WebUI](https://openwebui.com)/Ollama hosting `qwen2.5vl`), which
   reads the printed date back for you to confirm or correct - no photo
   ever leaves your own network.
3. Review and **add it to your pantry**. Everything is editable before
   saving, and a barcode that isn't found (or isn't scannable) just falls
   back to manual entry - nothing blocks you from tracking an item.
4. The **Pantry** tab lists everything you're tracking, grouped into
   Expired / Expiring soon / OK / No date set, with quick actions to edit,
   mark consumed, or delete.
5. A background check (configurable interval) re-evaluates every item and
   sends a **Home Assistant notification** through a `notify.*` service of
   your choice when something newly becomes due - with a repeat reminder
   until you deal with it.
6. Every active item is also exposed as a Home Assistant sensor entity (no
   extra setup), so you can build your own automations or dashboards
   around your pantry too - see [Home Assistant entities](#home-assistant-entities).

## Setup

### 1. Configure the app

Set the options below (**Settings → Add-ons → Pantry Tracker →
Configuration**), then start the app. All of them can be left at their
defaults and changed later from the app's own **Settings** tab, except the
vision model connection, which lives in this configuration only.

| Option | Default | Description |
|---|---|---|
| `notify_service` | _(blank)_ | A Home Assistant `notify.*` service, e.g. `notify.mobile_app_your_phone`. Blank disables notifications. |
| `notify_days_before` | `3` | How many days before the use-by date counts as "expiring soon". |
| `notify_repeat_hours` | `24` | How often to resend a reminder for an item still due. |
| `check_interval_hours` | `12` | How often to re-check every item and send any due notifications. |
| `vision_api_base` | _(blank)_ | Base URL of an OpenAI-compatible `/chat/completions` endpoint hosting a vision model (Open WebUI, Ollama, etc.), e.g. `http://192.168.1.50:8080/api`. Blank disables photo date extraction. |
| `vision_api_key` | _(blank)_ | Optional bearer token for the endpoint above. |
| `vision_model` | `qwen2.5vl:3b` | The vision model name to request. |

Finding the exact `notify_service` name: **Developer Tools → Actions**,
search for "notify", and use the dotted id shown there (e.g.
`notify.mobile_app_your_phone`).

### 2. Open it on your phone

Open the app from the Home Assistant sidebar (or add it to your phone's
home screen for an app-like shortcut) and use the **Scan** tab - it's
built mobile-first, with big touch targets and native camera capture.

## Using the app

- **Scan** - tap "Scan Barcode" to open your camera, snap the barcode, and
  the product details are looked up automatically. If a barcode can't be
  read or isn't found, you can still fill the form in by hand, or tap
  "Enter manually instead" to skip scanning entirely. Tap "Photo of use-by
  date" to read the expiry date from a photo of the packaging - the result
  is pre-filled into the date field but always stays editable, since OCR
  on packaging print is best-effort. Review everything and tap "Add to
  pantry".
- **Pantry** - everything you're tracking, grouped by urgency. Each item
  has **Edit** (correct the date, quantity, etc.), **Consumed** (remove it
  from active tracking), and **Delete**.
- **Settings** - the notification knobs above, a **Check now** button to
  run a check immediately instead of waiting for the next scheduled one,
  status lines showing whether the vision model and Home Assistant sync
  are configured, and a data **Export** as JSON.

## Home Assistant entities

With no extra setup beyond installing the add-on, your pantry is also
exposed as Home Assistant sensor entities, using the add-on's own
Supervisor-granted access to the Home Assistant API - no MQTT setup or
extra credentials needed.

- `sensor.pantry_tracker_summary` - state is the number of actively
  tracked items. Attributes include `expiring_soon` and `expired` counts,
  plus an `items` list with every active item's id, name, expiry date,
  days left, and status.
- `sensor.pantry_tracker_<id>_<name>` - one entity per active item, state
  is its urgency bucket (`ok`, `expiring_soon`, `expired`, or `unknown`) -
  useful as an automation trigger for a specific item. The entity
  disappears once that item is consumed or deleted.

Example automation, in case you'd rather route alerts yourself instead of
(or alongside) the built-in `notify_service` option:

```yaml
automation:
  - alias: Flash a light when something in the pantry expires
    trigger:
      - platform: state
        entity_id: sensor.pantry_tracker_summary
        attribute: expired
    condition:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.expired > 0 }}"
    action:
      - service: light.turn_on
        target:
          entity_id: light.kitchen
        data:
          flash: short
```

States set this way aren't tied to a registered integration, so they
briefly disappear across a Home Assistant restart until the next scheduled
check (governed by `check_interval_hours`) re-creates them.

## Storage

All pantry data is stored locally in `/data/pantry.db` (SQLite). Nothing
is sent anywhere except: a barcode digit string to Open Food Facts (to look
up the product), and a use-by-date photo to the vision model endpoint you
configure yourself.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps) to
add this repository to Home Assistant, then install **Pantry Tracker** from
the app store.
