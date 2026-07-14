# Changelog

## 1.1.0

- Added a `webui` link so **Settings → Add-ons → Pingvin Share** has a
  working **OPEN WEB UI** button.
- Documented how to add a real Home Assistant sidebar shortcut via the
  built-in `panel_iframe` integration (Ingress isn't used by this app, for
  the same absolute-URL reason as the WordPress app).
- Documented a known upstream bug where custom share links containing an
  underscore silently fail to submit, and the hyphen workaround.

## 1.0.0

- Initial release, based on `smp46/pingvin-share-x:v1.21.0`.
