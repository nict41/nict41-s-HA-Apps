# Changelog

## 0.5.1

- Fixed the gallery (and Settings/Help) loading completely blank when opened
  through a remote-access route with limited upload bandwidth, such as a
  Cloudflare Tunnel — it always loaded fine on the LAN. The page carried a
  leftover render-blocking tag from an earlier cross-document
  view-transition design (superseded back in 0.4.4 by the in-app navigation
  it uses today) that held the very first paint until the *entire* page had
  downloaded. Since archived GIFs are kept permanently, that page only grows
  over time, and over a slow link the wait could be long enough to look like
  the add-on wasn't working at all. The tag was already dead weight for
  in-app navigation, so removing it has no other effect.
- Archived GIFs in the gallery now get a small static thumbnail image
  generated alongside the GIF itself, so loading the gallery no longer
  requires downloading every full animated GIF just to draw its first frame
  as a preview. GIFs archived before this update don't have one yet and
  keep using the old (slower) full-GIF preview until they're re-generated.

## 0.5.0

- Added a **Help** page (the **?** icon in the gallery header) that walks
  through setup interactively: the `rest_command:` YAML to paste (still a
  one-time manual step — Home Assistant has no API for creating those),
  a form for your printer's entity IDs that live-updates the YAML previews
  as you type, and a button that creates the 3 required automations
  directly in your Home Assistant via its own Supervisor-granted API
  access. Re-running it after changing an entity ID updates the existing
  automations in place rather than duplicating them. A status panel shows
  whether `/start`, `/frame`, and `/finish` have actually been called since
  the add-on started, to help confirm the wiring is working end to end.
- This needs two new permissions (`homeassistant_api`, `hassio_api` for
  optional hostname self-detection) — restart the add-on once after
  upgrading for them to take effect.
- The automations generated (in-app or by hand, see DOCS.md) no longer need
  an `input_text` helper to pass `job_id` between them; they now derive it
  from the print-status sensor's own `last_changed` timestamp instead.

## 0.4.6

- Fixed the merged stats bar from 0.4.5 dropping back to two stacked rows on
  narrow screens (≤480px), which defeated the point of merging them. The two
  tiles now stay side by side at every width.

## 0.4.5

- Merged the **Timelapses** and **Most recent capture** tiles into a single
  bordered bar with an internal divider, instead of two separate cards, to
  cut the visual weight of the summary row in half.
- The **Archive** section is now collapsible (chevron in its header), like
  the **Capturing now** kebab patterns elsewhere in the gallery. Collapsed
  state persists per-browser across reloads.

## 0.4.4

- The gallery ↔ settings flash from 0.4.3 could still appear intermittently,
  because Home Assistant always loads this add-on inside an iframe, and
  Chromium's smooth-transition handling doesn't fully cover navigations
  inside an iframe. Navigation now swaps the page content in place (instead
  of loading a new document), which removes the flash entirely since the
  page never reloads.

## 0.4.3

- Removed the brief blank flash that appeared just before the gallery ↔
  settings page transition, by holding the first paint until the page is
  laid out and theming the page background from the start.

## 0.4.2

- **Capturing now** cards now show a live thumbnail of the print's most
  recent captured frame instead of a generic icon.
- The **Most recent capture** tile now reads **Capturing now** while a print
  is being captured, and refreshes itself automatically when the capture
  finishes.
- Merged the **Timelapses** and **Storage used** tiles into one to save
  space.
- Smoother crossfade when moving between the gallery and the settings page
  (on browsers that support view transitions; no change elsewhere).

## 0.4.1

- Fixed `401: Unauthorized` when clicking a GIF in the gallery. The redesign
  in 0.4.0 reintroduced the old bug where the thumbnail opened the
  ingress-proxied GIF URL in a new tab, which breaks out of the
  ingress-scoped session. GIFs now play in place instead of opening a tab.
- GIFs no longer autoplay: each archived GIF shows a still first-frame
  preview and only animates when you press play (press again to pause).
- Added a **⋮** menu on each **Capturing now** row to delete a stuck or
  abandoned capture job (clears that job's frames only; archived GIFs are
  never affected).

## 0.4.0

- Redesigned the gallery onto a proper design system that follows your
  device's light/dark setting: a header bar, summary tiles (timelapse count,
  storage used, most recent capture), polished cards, and humanized
  timestamps and file sizes instead of raw ISO strings and KB counts.
- Added a live **Capturing now** section that shows any print currently
  being captured (frame count + progress), updating on its own while the
  page is open.
- Added an in-app **Settings** page (gear icon) for `gif_fps`, `gif_width`,
  `cleanup_after_finish`, and `gif_export_path`. Changes are saved to the
  add-on's data and take effect on the next finished timelapse with no
  restart, overriding the matching add-on configuration options while set.

## 0.3.1

- Fixed `401: Unauthorized` when clicking a GIF thumbnail in the gallery
  panel. The thumbnail link opened the ingress-proxied GIF URL in a new tab
  (`target="_blank"`), which breaks out of the iframe-scoped ingress session
  and gets rejected. The link now navigates in place, like the working
  `download` link already did.

## 0.3.0

- New `gif_export_path` option: also copies every finished GIF to
  `/media/<gif_export_path>` (e.g. a mapped network share), in addition to
  the permanent local copy in `/data/archive`. Requires the app's new
  `media:rw` map permission. Leave blank to disable (default).
- `/finish`'s response now includes `exported_to` (the export destination
  path, or `null` if export is disabled or failed).

## 0.2.0

- `POST /frame` now takes `image_url` instead of an uploaded file: the
  app fetches the image itself. Home Assistant's `rest_command` can't
  upload local files, so this lets `/frame` be driven by a plain
  `rest_command` pointed at a snapshot already saved under HA's
  unauthenticated `/local/` folder.

## 0.1.0

- Initial release.
- `POST /start`, `POST /frame`, `POST /finish`, `GET /gifs` REST API.
- Ingress gallery page listing archived GIFs.
