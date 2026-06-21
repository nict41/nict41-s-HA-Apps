# 3D Print Timelapse

Captures a frame-by-frame timelapse of each 3D print and compiles it into a
GIF, with long-term storage of finished GIFs.

Designed to be driven by a couple of `rest_command`s from your Home
Assistant automations:

1. `POST /start` when a print begins.
2. `POST /frame` once per integer percent of progress, with an `image_url`
   the add-on fetches itself (e.g. a camera snapshot already saved to HA's
   `/local/` folder).
3. `POST /finish` when the print completes — builds the GIF and archives it.

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
for full install instructions, how to confirm the add-on's internal
hostname for `rest_command`, and the `rest_command` examples themselves.

## API

| Method | Path      | Form fields                              |
|--------|-----------|--------------------------------------------|
| POST   | `/start`  | `job_id`                                   |
| POST   | `/frame`  | `job_id`, `percent` (0-100), `image_url`   |
| POST   | `/finish` | `job_id`                                   |
| GET    | `/gifs`   | —                                           |

The ingress panel (this add-on, in your sidebar) shows a gallery of every
archived GIF.

## Options

- **`cleanup_after_finish`** (default `true`) — delete a job's frame images
  once its GIF has been built.
- **`gif_fps`** (default `8`) — frame rate of the assembled GIF.
- **`gif_width`** (default `480`) — GIF width in pixels.
- **`gif_export_path`** (default blank) — also copy every finished GIF to
  `/media/<gif_export_path>`, e.g. a network share mapped under Home
  Assistant's Network Storage as "Media". Blank disables this.

## Storage

- `/data/current/<job_id>/` — frames for an in-progress print.
- `/data/archive/` — finished GIFs, kept permanently.
- `/media/<gif_export_path>/` — optional extra copy of finished GIFs, if
  `gif_export_path` is set.
