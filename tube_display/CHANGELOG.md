# Changelog

## 1.0.2

- **Faster wipe:** the tubes now wipe down together, each starting a moment after the one to its left, with shorter pauses: about 0.9 s instead of about 4 s.
- On HA Tubes 2.5 the app sends several pictures in one request (the clock's new batch upload), which makes small updates such as a wipe's bands much quicker. Older firmware works as before.

## 1.0.1

- Transitions now play when the clock's button changes the screen in button-only mode (*Cycle automatically* off); the button press used to make the new screen skip its transition.
- Dependency updates: aiohttp 3.14.5, ruamel.yaml 0.19.1.

## 1.0.0

- **New widgets:** `graph` (history as a line, area or bars), `progress` (a gauge with an ETA, for prints and washing machines), `analog_clock` (with a ticking seconds hand), `weather` (condition icon, temperature and today's high/low), `ticker` (scrolling text) and `gif` (an animated GIF played by the clock itself).
- **Animation:** screens can appear with a transition (cascade, from the right, from the centre, wipe); tickers scroll and analogue clocks tick between refreshes by updating only the part of the tube that moves.
- **Fonts:** ten bundled fonts (Bebas Neue, Orbitron, Share Tech Mono, Oswald, JetBrains Mono, Nunito, Playfair Display, Press Start 2P, Roboto Condensed, Audiowide) and your own `.ttf`/`.otf`, per widget or as the default.
- **Free design across all six tubes** (`canvas`), with the drag-and-drop designer, tube boundaries and snap to tube centres.
- **Editor:** a gallery of ready-made screens, import and export of screens, a setup wizard, a condition builder for `when`, *Show on clock* while editing, and warnings with did-you-mean suggestions for mistyped fields.
- **HA Tubes 2.4 support:** compressed uploads (QOI, and JPEG for photos), about three times faster; GIFs played by the clock; the `ipstube_ha` firmware variant.
- New icon and logo.
- The editor no longer reports an error when no clock is set up yet (first run).
- Browser tests of the editor run in CI.

## 0.6.2

- Entity names no longer repeat the device name: *Tube Display Pause* instead of *Tube Display Tube Display Pause* (Home Assistant already puts the device name in front). Entity ids that already exist are kept; new installs get `select.tube_display_screen`, `switch.tube_display_pause` and so on.

## 0.6.1

- Firmware updates: the link offered to the clock used port 8098 even when the app's port is published on another host port (Settings > Apps > Tube Display > Network, e.g. 8093 when 8098 is taken), so the clock would have downloaded from the wrong place. The app now asks the Supervisor which host port it's on.

## 0.6.0

- **Live updates:** templates are subscribed over Home Assistant's websocket, so a screen changes the moment its entities do (at most once a second) instead of every `refresh` seconds. Falls back to the REST API if the connection drops.
- **Actions:** a bundled `tube_display` integration adds `tube_display.show_screen`, `flash_message`, `pause`, `resume`, `next_screen` and `set_rotation`. The app installs it and Home Assistant finds it after one restart (Settings > Devices & services).
- **Flash messages:** `POST /api/flash`, the new *Message* text entity or `tube_display.flash_message` show a message across the six tubes (optional icon and colour), then go back to what was showing.
- **Pause:** hand the tubes back to the clock's own time display until resumed or for a set time (*Pause* switch, `POST /api/pause`, `tube_display.pause`).
- **Night mode:** a `night:` block in the screens file sets the clock's brightness from the sun's elevation, dims the screens at night and can keep quiet hours (the clock's own time, or black tubes). Without the block the brightness is left alone.
- **Clock firmware updates:** the app offers new HA Tubes releases to the clocks (Home Assistant shows them on each clock's *Firmware* update entity) and serves the file to the clock itself. While a clock updates, the app sends nothing, so there's no longer any need to stop the app first.
- **Clock offline:** when a clock can't be reached the app stops retrying every tube and checks it every 5 to 60 seconds instead, then sends everything again when it's back. New *Clock Online* binary sensor.
- **Several clocks:** the new `clocks` option adds more clocks, each with its own entities and optionally its own rotation (`clocks: {<name>: {rotation: [...]}}` in the screens file). Every action and API call can name a `clock`.
- **More entities:** *Current Screen* and *Last Error* sensors, *Pause* switch, *Message* text, *Next Screen* button. All of them go unavailable when the app stops.
- **Editor:** undo and redo, a history of earlier versions of the screens file (a copy is kept every time you save) with restore, a clock picker for per-clock rotations, a *Night mode* card and a status line showing what each clock shows.
- New options: `clocks`, `firmware_updates`, `firmware_variant`, `app_url`. `clock_host` and `clock_mqtt_id` can be left empty when all clocks are under `clocks`.

## 0.5.1

- Designer: **snap to grid** (2 to 20 px, measured from the centre of the tube) with the grid drawn over the tube. Remembered in the browser.
- Entity names autocomplete: type a domain and a dot (`sensor.`) in any template or text box, or a few letters in an entity field, and pick from the matches (Up/Down, Enter or Tab; Esc closes).

## 0.5.0

- Pictures of any size or shape: under the picture library the picture is shown on the tube, and you drag it into place, zoom it (pinch, scroll or slider), rotate it and choose the colour behind it. New fields `zoom`, `offset_x`, `offset_y`, `rotate` and `background` on `image` tubes and on `strip` screens. Sideways phone photos are turned upright.
- Editor: a screen can be switched between six tubes and one picture across all six (**layout**), and that picture is placed the same way.
- New `custom` tube: any number of text, ring, bar, icon, picture and box items, each placed by `x`/`y`. The editor has a designer for it: drag items on the tube, add, duplicate, reorder and delete them. Changing a tube's type to `custom` keeps what it showed as separate items.
- Templates work in every field of a widget, including ones that only use `{% ... %}`; the editor shows a template's live result under labels, units and sizes too.
- Several lines in labels, units, values and icon text (Enter in the editor, or `\n`). A ring whose label has several lines moves up to make room.
- Text sizes and colours per part: `value_size`, `unit_size`, `label_size`, `icon_size`, `text_size`, `value_color`, `unit_color`, `label_color`, `text_color`; ring `radius` and `thickness`; text `valign`. Text size in the editor is any number, not a fixed list.
- Rings have smooth edges.
- `fit: contain` fills the leftover space with the background colour instead of black.

## 0.4.7

- Security: Pillow 13.0.0 (was 11.2.1) and aiohttp 3.14.4, which fix known vulnerabilities.

## 0.4.6

- Editor: opening a tube with colour thresholds marked the screen changed and rewrote its colours in lower case; it now only rewrites them when you edit the thresholds.
- Editor: long templates wrap inside the card on phones; the Claude logo has a thumbnail in the picture library.
- Docs: screenshots of the editor.
- Docs: how to use the screen editor, the screens_file option, file: pictures, full-tube numbers, centred icons, the editor's icon/picture/rotation API, the clock's button with HA Tubes 2.3.0, and stopping the app during clock firmware updates. README describes the editor, button and HA Tubes requirement.

## 0.4.5

- Docs: the clock firmware is now called HA Tubes (github.com/nict41/HA-Tubes), with a web installer.

## 0.4.4

- Editor: the colour picker started at white when no colour was set, so picking white for an icon (which defaults to orange) saved nothing. It now starts at the tube's real default colour.
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
