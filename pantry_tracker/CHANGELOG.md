# Changelog

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
