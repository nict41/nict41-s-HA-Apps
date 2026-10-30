# Changelog

## 0.9.0

- Use-by photos and product photos now open in a viewer inside the app.
  The old link opened a new browser tab, which isn't part of the Home
  Assistant session and failed with `401: Unauthorized`.
- Fixed wrong use-by dates such as `2023-03-14` for a pack printed `03 NOV`:
  - The camera now asks for up to 4K for date and product photos instead of
    the browser's low default, which left a cropped date about 250 pixels
    wide, and uses continuous focus where the phone supports it. The size
    limit on photos sent to the vision model went from 1024 to 2048 pixels.
  - The model is now asked to copy the date as printed rather than reformat
    it, because it was inventing a year when none was printed. The app works
    out the year itself.
  - A time after a date is no longer read as a year (`02 Nov 11:26` became
    2011), and dates run together with other print are now understood.
  - A date more than 180 days in the past is now treated as a misread year
    and moved to its next occurrence.
- A date with no year that passed within the last 30 days now stays in the
  past, so forgotten food shows as expired instead of gaining a year.

## 0.8.0

- Fixed tapping a notification not opening the Pantry tab. The link added
  in 0.7.0 never worked: it used the container hostname (which has hyphens)
  as the add-on's slug, and relied on a `#pantry` hash that Home Assistant
  doesn't pass to an add-on's iframe. It now points at the panel's real
  path (`/<slug>/pantry`) and reads the route Home Assistant sends the page.
  Also sets the iOS `url` key alongside Android's `clickAction`.
- Added **Photo of product**, in the review form and the Pantry tab's Edit,
  for products Open Food Facts has no photo of. The photo is stored against
  the barcode and reused by every later scan. Items without a barcode can't
  use this, since there's nothing to key it to.
- Items added before 0.6.0 now get their product photos: the add form didn't
  submit an image back then, so none was saved even when one was available.
  A one-off background job fills these in on startup, without touching
  corrected names or brands.

## 0.7.0

- Tapping a due-item notification now opens straight to the Pantry tab
  instead of wherever Home Assistant last was - the notification's
  `clickAction` now points at this add-on's own ingress panel.
- No code change for product photos (confirmed working end-to-end against
  Open Food Facts, including a real-world example) - if a particular
  barcode still doesn't show one, Open Food Facts itself doesn't have a
  front-of-pack photo for that product (common for own-brand/discount
  items; well-known branded products are far more likely to have one).

## 0.6.0

- Fixed the Home Assistant notify service appearing to get lost after
  every app update. It wasn't actually about updates: `notify_service`
  could be set two ways (the add-on's own Configuration tab, or the
  app's in-app Settings page), and once anything was ever saved via the
  in-app page - even accidentally blank, e.g. from visiting Settings
  before Configuration had been set up yet - that saved value silently
  shadowed the Configuration tab forever after, no matter what was
  subsequently set there. `notify_service` is now add-on-configuration-
  only (restart the add-on after changing it), the same as the vision
  model connection already was, removing the possibility of this
  altogether. A stale saved override from before this fix is cleared
  out automatically on upgrade.
- Product photos from Open Food Facts are now shown - in the review form
  right after a barcode's looked up (handy for spotting a barcode that
  matched the wrong product), and on each Pantry list row.
- Correcting a barcode's product details (handy when Open Food Facts'
  crowd-sourced data is wrong or garbled for a particular barcode) now
  also updates the local cache for that barcode, whether the correction
  is made before tapping "Add to pantry" or later from the Pantry tab's
  Edit - so the same barcode serves the correction next time instead of
  the original bad data.

## 0.5.0

- Use-by date photos are now kept (`/data/photos/`, removed automatically
  if the item is deleted) and can be opened from the Pantry list (**View
  use-by photo**) or the edit panel's thumbnail, to compare against the
  date that was actually read from it.
- Reading a use-by date now always happens **in the background**, on the
  add-on itself, instead of blocking the app's UI while it waits on the
  vision model:
  - **Add to pantry** no longer waits on a photo's extraction - capture
    the photo, tap Add, and go straight on to scanning the next item. The
    date fills in on its own (check the Pantry tab) once it's ready.
  - **Rescan use-by date** (on an existing item) works the same way -
    capture the new photo and the edit panel can be closed immediately,
    there's nothing left to wait for.
  - A date typed into the field by hand before tapping "Add to pantry"
    always wins over a background read that disagrees with it. Rescanning
    an existing item still always replaces whatever date was there, since
    that's the whole point of a rescan.
  - The Pantry list shows a **Reading use-by date…** spinner for an item
    while this is in progress, and refreshes itself automatically once
    it's done.
- Removed the now-unused `/api/extract-date` endpoint - date extraction is
  always tied to an item (so there's somewhere to save the photo and a
  result to write to) rather than a one-off, disconnected request.

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
