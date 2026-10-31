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
   quantity, category and photo (shown once found, if Open Food Facts has
   one on file for that barcode - not every product does, especially
   own-brand/discount items; tap **Photo of product** to take your own,
   which is kept for that barcode and reused on every later scan) - a barcode already scanned before is served
   instantly from a local cache instead of hitting Open Food Facts again.
   Crowd-sourced data is sometimes wrong or garbled for a
   particular barcode; correcting it before tapping "Add to pantry" (or
   later, editing the item from the Pantry tab) fixes the cache too, so
   the same barcode serves your correction next time instead of the
   original. On a browser without live camera support, it falls back to
   the phone's own photo-capture UI and decodes the barcode (via zbar) on
   the add-on instead.
2. Optionally open the same live camera view to **photograph the use-by/
   best-before date** - line the date up in the on-screen frame and it
   captures automatically once held steady for a moment (a shutter button
   is also there as a manual override). The photo is cropped to just the
   framed area and sent to a vision-capable model you run yourself (e.g.
   [Open WebUI](https://openwebui.com)/Ollama hosting `glm-ocr`), which
   reads the printed date back for you - the date is read **in the
   background**, so capturing it never stops you moving straight on to
   scan the next item, and the photo itself is kept so you can open it
   later and compare it against the date that was read from it. A second
   reader runs on the phone itself; its text is interpreted by the app and
   compared with the vision model's result. The first use downloads about
   30 MB of OCR files to the browser, then the browser cache keeps them for
   later scans. The use-by photo still goes to your configured vision
   endpoint for its independent reading.
3. Review and **add it to your pantry**. Everything is editable before
   saving, and a barcode that isn't found (or isn't scannable) just falls
   back to manual entry - nothing blocks you from tracking an item.
4. The **Pantry** tab lists everything you're tracking, grouped into
   Expired / Expiring soon / OK / No date set, with quick actions to edit,
   mark consumed, or delete.
5. A background check (configurable interval) re-evaluates every item and
   sends a **Home Assistant notification** through a `notify.*` service of
   your choice when something newly becomes due - with a repeat reminder
   until you deal with it. Tapping the notification opens straight to the
   **Pantry** tab.
6. Every active item is also exposed as a Home Assistant sensor entity (no
   extra setup), so you can build your own automations or dashboards
   around your pantry too - see [Home Assistant entities](#home-assistant-entities).

## Setup

### 1. Configure the app

Set the options below (**Settings → Add-ons → Pantry Tracker →
Configuration**), then start the app. `notify_days_before`,
`notify_repeat_hours` and `check_interval_hours` can be left at their
defaults here and changed later from the app's own **Settings** tab
without a restart - `notify_service` and the vision model connection
live in this Configuration tab only (restart the add-on after changing
either), so a value saved once can never end up silently overriding a
later change made here, across an app update or otherwise.

| Option | Default | Description |
|---|---|---|
| `notify_service` | _(blank)_ | A Home Assistant `notify.*` service, e.g. `notify.mobile_app_your_phone`. Blank disables notifications. Restart the add-on after changing it. |
| `notify_days_before` | `3` | How many days before the use-by date counts as "expiring soon". |
| `notify_repeat_hours` | `24` | How often to resend a reminder for an item still due. |
| `check_interval_hours` | `12` | How often to re-check every item and send any due notifications. |
| `vision_api_base` | _(blank)_ | Base URL of an OpenAI-compatible `/chat/completions` endpoint hosting a vision model (Open WebUI, Ollama, etc.), e.g. `http://192.168.1.50:8080/api`. Blank disables photo date extraction. |
| `vision_api_key` | _(blank)_ | Optional bearer token for the endpoint above. |
| `vision_model` | `glm-ocr:latest` | The vision model name to request. |

Finding the exact `notify_service` name: **Developer Tools → Actions**,
search for "notify", and use the dotted id shown there (e.g.
`notify.mobile_app_your_phone`).

### 2. Open it on your phone

Open the app from the Home Assistant sidebar (or add it to your phone's
home screen for an app-like shortcut) and use the **Scan** tab - it's
built mobile-first, with big touch targets and native camera capture.

### A note on live camera access

The live, auto-scanning camera view needs a **secure context** - your
Home Assistant instance reached over `https://` (Nabu Casa remote access,
or a local reverse proxy/certificate), not plain `http://`. This is a
browser platform rule (`getUserMedia`), not something this app can work
around - over plain HTTP, live scanning is unavailable everywhere, not
just here, and the app detects this and tells you rather than failing
silently.

Taking a photo (for either the barcode or the use-by date) doesn't need
HTTPS and always works - but **the Home Assistant companion app's own
built-in browser has been seen to open a generic photo picker instead of
jumping straight to the camera**, even though a normal mobile browser
handles the same page correctly. If that happens, the Scan tab shows a
link to open the app directly in your phone's browser (its direct port,
same network only) - the exact same app and data, just without that
limitation.

## Using the app

- **Scan** - tap "Scan Barcode" to open your camera, snap the barcode, and
  the product details are looked up automatically. If a barcode can't be
  read or isn't found, you can still fill the form in by hand, or tap
  "Enter manually instead" to skip scanning entirely. Tap "Photo of use-by
  date" to capture the expiry date from a photo of the packaging, then tap
  "Add to pantry" right away - the date is read in the background and
  fills in on its own once it's done (check the Pantry tab), since OCR on
  packaging print is best-effort and isn't worth waiting around for. If
  no year is printed, the app uses the nearest upcoming date (or one up
  to 30 days ago, so forgotten food still shows as expired), and a date
  over 180 days in the past is treated as a misread year. A
  date typed into the field by hand always wins over a background read
  that disagrees with it.
- **Pantry** - everything you're tracking, grouped by urgency, with a
  **Needs a date** banner that jumps to active items without a date. Dates shown
  as e.g. "1 July 2027", with a **Reading use-by date…** spinner shown
  while a background read is still in progress for that item. Each item
  with a stored photo has a **View use-by photo** link, opening it in
  the app so you can compare it against the date that was read from it. Each item has **Edit**
  (correct the name, quantity, category, or notes; pick a new date by
  hand, or tap **Rescan use-by date** to read it from a fresh photo the
  same way as when first adding the item - this also runs in the
  background), **Consumed** (remove it from active tracking), and
  **Delete**.
- **Settings** - the notification knobs above, a **Check now** button to
  run a check immediately instead of waiting for the next scheduled one,
  status lines showing whether the vision model and Home Assistant sync
  are configured, and a data **Export** as JSON.

### Date reading confidence

Each photo read is checked by on-device OCR and the configured vision model. **✓ Both readers agree** means both produced the same date. **Check this date against the photo** means only one reader found a date. **Readings differ** shows both candidate dates; choose the one printed on the package. **Couldn't read the date** means neither reader found a date; use Edit to rescan or enter it by hand. The parser ranks multiple date candidates using labels and packaging context, and compares both readers’ candidate lists. Common OCR character confusions are repaired conservatively. For uncertain or unread readings, tap a digit-bearing OCR line to try it again in lenient mode; confirm the formatted date before it is saved. The parser accepts dates such as `01/03/2027`, `01 03 27`, `010327`, and `28 JAN 27`, while rejecting unrelated batch codes.

## Home Assistant entities

With no extra setup beyond installing the add-on, your pantry is also
exposed as Home Assistant sensor entities, using the add-on's own
Supervisor-granted access to the Home Assistant API - no MQTT setup or
extra credentials needed.

- `sensor.pantry_tracker_summary` - state is the number of actively
  tracked items. Attributes include `expiring_soon`, `expired`, and
  `needs_date` counts,
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

All pantry data is stored locally in `/data/pantry.db` (SQLite), and each
item's use-by date photo (if any) in `/data/photos/` - kept so you can
re-open it later to compare against the date that was read from it, and
removed automatically if the item itself is deleted. Product photos you
take are stored per barcode in `/data/product_photos/` and kept even after
an item is deleted, so the next scan of that product still has one.
The on-device OCR runs in the phone browser. Its recognized text is sent to this app for date interpretation. A barcode digit string goes to Open Food Facts, and the use-by-date photo goes to the vision model endpoint you configure yourself.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps) to
add this repository to Home Assistant, then install **Pantry Tracker** from
the app store.
