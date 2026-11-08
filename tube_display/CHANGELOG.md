# Changelog

## 0.4.4

- Icon tubes without text show the icon centred (and a little bigger) instead of high up.

## 0.4.3

- Editor: entity search results were squashed to slivers (list rows shrank to fit); they keep their height, wrap long names and the list scrolls. Inputs no longer push cards wider than a phone screen.
- `big_number` with only a value (no label/unit) fills the tube, e.g. one digit of a time per tube.
- Text is centred by its visible ink, so large text is no longer pushed down and clipped.

## 0.4.2

- Editor: screens taken out of the rotation stay in the Rotation list (unticked) so they can be ticked back in.

## 0.4.1

- Editor: a tube with a mistake (e.g. an icon without `mdi:`) no longer blanks the preview; it shows a red "!" and the error, and all tubes stay clickable so it can be fixed.
- Editor: icon names autocomplete from the Material Design Icons set as you type; `train` becomes `mdi:train`.

## 0.4.0

- Button-only mode: turn off **Cycle automatically** (editor top bar, or the Tube Display Rotation switch in Home Assistant) and screens only change when you press the clock's button; the clock entry is skipped then. The setting is remembered across restarts.

## 0.3.1

- Editor: tube previews really fit the tube now (a `background` shorthand reset the picture's scaling).

## 0.3.0

- Picture library: upload pictures in the editor (image tube → + Upload), pick them from thumbnails, delete them. Stored in `tube_display/images/` next to screens.yaml and used as `source: file:<name>`. Transparent parts show the screen background.

- Editor: phones kept an old cached stylesheet with the new page, so previews were stretched and cut off; the page now always loads matching files. Smooth preview scaling.

## 0.2.2

- Editor: tube previews were cut off (border drawn over the picture; not scaled on phones).

## 0.2.1

- Editor: the preview and clickable tubes didn't appear (the page read the preview image as JSON, using up its body).

## 0.2.0

- Visual screen editor in Home Assistant (ingress panel "Tube Display", served on a second port 8099 that only accepts the ingress proxy): edit rotation, screens and all six tubes with a live rendered preview, entity picker, colour thresholds, YAML tab with atomic comment-preserving saves (ruamel round-trip), conflict detection when the file changed on disk, and live template rendering. The 8098 API is unchanged.

## 0.1.2

- The clock's button moves on to the next screen while panels show (needs clock firmware 2.2.0-nt3 or later, which publishes `clock/<id>/button` = `next`).

## 0.1.1

- screens.yaml now lives in the Home Assistant config folder (`/homeassistant/tube_display/screens.yaml`, File editor: `tube_display/screens.yaml`), set by the new `screens_file` option. An existing one is copied there on first start.
- Fix: template values showed as `b'…'`, rings had no colour and `\n` didn't break lines (HA's text reply was not decoded).
- `unit_newline: true` on `big_number` and `ring` puts the unit on its own line.

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
