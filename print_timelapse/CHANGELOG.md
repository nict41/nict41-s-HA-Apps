# Changelog

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
