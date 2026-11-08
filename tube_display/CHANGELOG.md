# Changelog

## 0.1.0

- Initial release. Renders Home Assistant information into pictures and pushes
  them to an IPSTube clock's six 135x240 panel displays, rotating between
  screens defined in `/config/screens.yaml` (re-read live when the file
  changes). Widgets: text, big_number, ring, bar, icon (Material Design
  Icons), image (builtin:claude starburst, http(s) URL, `camera:` proxy,
  `entity_picture:`), sparkline (HA history API) and blank; a screen can also
  be a single 810x240 `strip` picture. All string fields accept HA templates,
  rendered concurrently via the Supervisor API. Pushes only tubes whose bytes
  changed, plus tubes the clock reports as needed; handles 409 (re-publishes
  display=4) and 503/connection errors with backoff. HTTP API on port 8098
  (`/api/status`, `/api/show`, `/api/rotation`, `/preview/<screen>.png`) and
  MQTT discovery entities "Tube Display Screen" (select) and "Tube Display
  Rotation" (switch). Writes a commented example screens.yaml on first start.
