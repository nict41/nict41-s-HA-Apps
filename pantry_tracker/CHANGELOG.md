# Changelog

## 0.4.1

- Fixed a use-by date with no year printed near it (e.g. packaging that
  just says "31 OCT") sometimes coming back with a hallucinated, wrong
  year that's already in the past (reported: a "31 Oct" photo came back
  as 2023-10-31 instead of the intended 2026). The vision model is now
  told to reply with a 0000 placeholder year instead of guessing one when
  it can't see a year, and the app resolves that itself to the nearest
  occurrence of that day/month that isn't already in the past - so an
  item just bought no longer extracts as already expired. A date that
  does have a real printed year is unaffected either way.

## 0.4.0

- The use-by date camera now **auto-captures**: once the on-screen frame is
  held steady over something for a moment (and isn't too dark, too bright,
  or blank), it snaps automatically - no shutter tap needed, though the
  shutter button is still there to capture on demand instead. This is a
  steadiness heuristic, not real date detection, so it occasionally
  triggers on anything held still - a cheap false positive, since a bad
  capture just falls back to "couldn't read a date, enter manually" like
  it always has.
- Every use-by date photo (auto- or manually-captured) is now **cropped to
  the on-screen guide frame** before being sent for extraction, instead of
  the much wider full camera frame around it - giving the vision model a
  tighter, more legible image to read.

## 0.3.0

- Live camera scanning now correctly detects when it can't actually work:
  `getUserMedia` (needed for the live barcode viewfinder) requires a
  "secure context" (HTTPS, or localhost) - on a Home Assistant instance
  reached over plain `http://`, browsers refuse it outright, in every
  browser and app, not a bug this app could route around. The Scan tab now
  checks this up front instead of attempting it and failing, and shows a
  short note explaining what's going on - plus, since the Home Assistant
  app's own in-app browser has also been seen not to honour a file input's
  "open camera directly" hint (falling back to a generic photo picker
  instead), a link to open the app directly in your phone's own browser,
  where photo capture opens the camera properly even without HTTPS.
- Fixed the live camera overlay not being able to open at all from the
  Pantry tab (e.g. for the new "Rescan use-by date" below) - it was nested
  inside the Scan tab's own markup, which is hidden while another tab is
  active, so a camera opened from elsewhere rendered nothing.
- Dates are now shown as "1 July 2027" instead of the raw "2027-01-01"
  everywhere the app displays one as text (the pantry list, and the
  use-by date extraction result) - storage and the date-picker field
  itself are unaffected.
- Editing a pantry item can now **rescan its use-by date** - a new button
  on the edit form opens the same live camera/photo capture used when
  first adding an item, instead of only allowing the date to be typed or
  picked by hand.

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
