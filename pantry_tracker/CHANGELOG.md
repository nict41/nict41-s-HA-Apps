# Changelog

## 0.2.0

- Scanning a barcode and photographing a use-by date now open a live,
  full-screen camera view in the app itself, instead of handing off to the
  phone's own photo-capture UI:
  - **Scan Barcode** opens a live viewfinder that scans frames automatically
    (via the browser's `BarcodeDetector` API - no library, works out of the
    box on Android Chrome) and jumps straight to the product lookup the
    instant a barcode decodes, no shutter tap needed.
  - **Photo of use-by date** opens the same live view with a **Capture**
    shutter button, since reading a date needs a deliberate "now" moment
    rather than auto-detection.
  - Both fall back to the OS photo-capture file picker on a browser without
    camera support (or if camera permission is denied), so nothing is lost
    on unsupported devices.
- Scanned barcodes are now cached (indefinitely - a product's name/brand
  rarely changes) after their first successful Open Food Facts lookup, so
  re-scanning the same item later (the common case - the same milk, the
  same tins, week after week) is instant and makes no network request at
  all.
- Fixed "Add to pantry" being able to save an item before a still-running
  use-by date photo extraction had finished, silently dropping the date it
  would have found. The button now waits for extraction to finish (showing
  "Reading use-by date…") before submitting.

## 0.1.1

- Fixed the app failing to start on a fresh install
  (`invalid options: expected a URL`). `vision_api_base`'s schema was
  `url?`, but a `?` only makes the *key* optional - the *value* still had
  to pass URL validation, and the default blank value (`""`) failed it.
  The option is now a plain string, validated by the app itself.

## 0.1.0

- Initial release.
- Scan a grocery barcode with your phone's camera (zbar) and look it up
  against Open Food Facts for product name, brand, quantity and category.
- Optionally photograph a use-by/best-before date and have it read by a
  self-hosted vision model (e.g. qwen2.5vl via Open WebUI/Ollama) over an
  OpenAI-compatible endpoint - always editable before saving.
- Mobile-first ingress UI with Scan / Pantry / Settings tabs, native camera
  capture, and no external fonts or CDN requests.
- Background check sends a Home Assistant notification through a
  configurable `notify.*` service when an item becomes due, with a repeat
  reminder until it's dealt with.
- Every active item is exposed as a Home Assistant sensor entity
  (`sensor.pantry_tracker_summary` and one per item), using the add-on's
  own Supervisor-granted Home Assistant API access - no MQTT or extra
  credentials required.
