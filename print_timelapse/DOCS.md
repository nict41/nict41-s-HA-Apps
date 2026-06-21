# 3D Print Timelapse

![3D Print Timelapse logo](logo.png)

Captures a frame-by-frame timelapse of each 3D print and compiles it into a
GIF, with long-term storage of finished GIFs.

The app itself has no UI for capturing frames — it's a small REST API that
your Home Assistant automations drive: one call to start a job, one call per
progress step to add a frame, and one call to finish the job and build the
GIF. The app's ingress panel (in your sidebar) only shows the gallery of
finished GIFs.

## How it works

1. `POST /start` — call this when a print begins, with a unique `job_id`.
2. `POST /frame` — call this once per integer percent of progress, with the
   same `job_id`, the current `percent`, and an `image_url` pointing at a
   snapshot image. The app fetches the image itself from that URL.
3. `POST /finish` — call this when the print completes. The app assembles
   every frame collected for that `job_id` into a GIF, archives it
   permanently, and (optionally) exports a copy to a network share.

## Setup

### 1. Find the app's internal hostname

Home Assistant gives every app a hostname of the form `{REPO}_{SLUG}` (with
any `_` replaced by `-` to make it a valid DNS name), where `{SLUG}` comes
from the app's `config.yaml` (`print_timelapse` here) and `{REPO}` is a
hash generated from the URL of the repository the app was installed from.

If you installed this app from **this** repository
(`https://github.com/nict41/nict41-s-HA-Apps`), that hash is always
`beb500c8`, so the app's hostname is:

```
beb500c8-print-timelapse
```

This is the same for every user who adds this repository, and the same
hash applies to the other apps in it too (e.g. `beb500c8-glance`). If
you've forked this repository to your own URL, the hash will differ — you
can confirm it for any installed app via the Supervisor API's `/addons`
endpoint, or simply by checking the hostname your own working `rest_command`
ends up using once you test it.

### 2. Make snapshot images reachable by URL

`POST /frame` doesn't accept a file upload — Home Assistant's `rest_command`
can't upload local files, so the app fetches `image_url` itself instead.
That means whatever image you want to use as a frame needs to already be
reachable at a URL the app can fetch over the network.

The simplest way to do this is to save your camera snapshot into Home
Assistant's `www` folder (`/config/www/`), which HA serves unauthenticated
at `/local/`. For example, saving a snapshot to
`/config/www/print_progress.jpg` makes it available at:

```
http://homeassistant:8123/local/print_progress.jpg
```

— which is exactly the kind of URL you point `image_url` at. Use the
[`camera.snapshot`](https://www.home-assistant.io/integrations/camera/#action-camerasnapshot)
action (or your printer integration's equivalent) to write that file just
before calling `/frame`. Re-using the same filename for every frame is
fine — `/frame` fetches and stores its own copy as soon as it's called, so
the file just needs to hold the *current* frame's image at call time.

### 3. Set up the `rest_command`s

Add the following to your `configuration.yaml` (replace the hostname if
yours differs from step 1, and `print_progress.jpg` with whatever filename
you save snapshots to):

```yaml
rest_command:
  timelapse_start:
    url: "http://beb500c8-print-timelapse:8099/start"
    method: POST
    payload: "job_id={{ job_id }}"
    content_type: "application/x-www-form-urlencoded"
  timelapse_frame:
    url: "http://beb500c8-print-timelapse:8099/frame"
    method: POST
    payload: "job_id={{ job_id }}&percent={{ percent }}&image_url=http://homeassistant:8123/local/print_progress.jpg"
    content_type: "application/x-www-form-urlencoded"
  timelapse_finish:
    url: "http://beb500c8-print-timelapse:8099/finish"
    method: POST
    payload: "job_id={{ job_id }}"
    content_type: "application/x-www-form-urlencoded"
```

Restart Home Assistant (or reload the YAML configuration) after adding
this, then call each action from an automation with `job_id` (and `percent`
for `timelapse_frame`) supplied as action data, e.g.
`rest_command.timelapse_start` with `data: {job_id: "{{ job_id }}"}`.

### 4. Wire up automations

The exact triggers depend on your printer integration (OctoPrint, Moonraker,
Bambu Lab, etc.), but the shape is always the same: start a job when
printing begins, snapshot + capture a frame on every progress change, and
finish when the print completes.

`job_id` needs to be generated once per print and reused across all three
calls, so a small `input_text` helper (**Settings → Devices & services →
Helpers → Create helper → Text**) works well to hold it for the
automations below. Adjust the trigger entity IDs to match your printer:

```yaml
automation:
  - alias: "Timelapse: start"
    trigger:
      - trigger: state
        entity_id: sensor.printer_print_status
        to: "printing"
    action:
      - action: input_text.set_value
        target:
          entity_id: input_text.current_timelapse_job_id
        data:
          value: "{{ now().strftime('%Y%m%d_%H%M%S') }}"
      - action: rest_command.timelapse_start
        data:
          job_id: "{{ states('input_text.current_timelapse_job_id') }}"

  - alias: "Timelapse: capture frame"
    trigger:
      - trigger: state
        entity_id: sensor.printer_print_progress
    condition:
      - condition: template
        value_template: >-
          {{ trigger.to_state.state | int(0) != trigger.from_state.state | int(0) }}
    action:
      - action: camera.snapshot
        target:
          entity_id: camera.printer
        data:
          filename: "/config/www/print_progress.jpg"
      - action: rest_command.timelapse_frame
        data:
          job_id: "{{ states('input_text.current_timelapse_job_id') }}"
          percent: "{{ trigger.to_state.state | int }}"

  - alias: "Timelapse: finish"
    trigger:
      - trigger: state
        entity_id: sensor.printer_print_status
        to: "completed"
    action:
      - action: rest_command.timelapse_finish
        data:
          job_id: "{{ states('input_text.current_timelapse_job_id') }}"
```

## API

| Method | Path      | Form fields                              |
|--------|-----------|--------------------------------------------|
| POST   | `/start`  | `job_id`                                   |
| POST   | `/frame`  | `job_id`, `percent` (0-100), `image_url`   |
| POST   | `/finish` | `job_id`                                   |
| GET    | `/gifs`   | —                                           |

The ingress panel (this app, in your sidebar) shows a gallery of every
archived GIF.

## Options

| Option | Default | Description |
|---|---|---|
| `cleanup_after_finish` | `true` | Delete a job's frame images once its GIF has been built. |
| `gif_fps` | `8` | Frame rate of the assembled GIF. |
| `gif_width` | `480` | GIF width in pixels (height scales to match). |
| `gif_export_path` | _(blank)_ | Also copy every finished GIF to `/media/<gif_export_path>`. See [Configuring GIF export](#configuring-gif-export). |

## Configuring GIF export

By default, finished GIFs are only kept in the app's own permanent storage
(`/data/archive/`). Setting `gif_export_path` additionally copies every
finished GIF out to `/media/<gif_export_path>` — handy for sending them
straight to a NAS share instead of having to dig them out of the app's
storage manually.

To use it:

1. Map a network share under Home Assistant's
   **Settings → System → Storage → Add network storage**, with usage set
   to *Media*. This makes it appear under `/media/<share name>` to apps
   that request media access (this app already does, no extra setup
   needed there).
2. Set `gif_export_path` to the path under `/media` you want GIFs copied
   to, e.g. `NAS1/Photos and Videos/3D Print Timelapses`.
3. Leave it blank (the default) to disable export — GIFs then only live in
   the app's own permanent storage.

`POST /finish`'s response includes `exported_to`: the absolute export path
on success, or `null` if export is disabled or the copy failed (check the
app's logs for the reason).

## Storage

- `/data/current/<job_id>/` — frames for an in-progress print.
- `/data/archive/` — finished GIFs, kept permanently.
- `/media/<gif_export_path>/` — optional extra copy of finished GIFs, if
  `gif_export_path` is set.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **3D Print
Timelapse** from the app store.
