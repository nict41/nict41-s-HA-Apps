# Home Assistant App: Immich Machine Learning (OpenVINO)

![Immich Machine Learning logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/immich_ml/logo.png)

A standalone [Immich](https://immich.app/) Machine Learning sidecar, built
on the official
[`ghcr.io/immich-app/immich-machine-learning`](https://github.com/immich-app/immich/pkgs/container/immich-machine-learning)
image with the `-openvino` tag, so a separate Immich server app can offload
face detection and smart search to an Intel iGPU instead of maxing out the
CPU. This app runs the upstream image completely unmodified - the only
thing this repo adds is a thin startup script that turns its Home
Assistant options into the image's own environment variables.

This app is **not** an Immich server on its own - it's a sidecar that an
existing Immich server (e.g. the "Immich (all-in-one)" app) points its
Machine Learning setting at.

## Requirements

- **amd64 only.** OpenVINO acceleration is Intel-specific; this app won't
  install on other architectures.
- An Intel iGPU exposed at `/dev/dri` on the Home Assistant host (true for
  any machine with a supported Intel CPU/iGPU, e.g. the N100).

## Quickstart

1. Install and start this app. On first start it downloads the ML models
   it needs into its own persistent storage - this can take a few minutes
   and a few GB of network traffic; watch the log.
2. In your Immich server's admin settings, go to **Administration →
   Settings → Machine Learning Settings** and add this app's URL,
   `http://<home-assistant-host-ip>:3003`, as the **first** URL in the
   list, keeping the server's own built-in URL after it as a fallback.
   Immich tries each URL in order and falls back automatically if one is
   unreachable.
3. Trigger a job that uses ML (e.g. re-run "Face Detection" or "Smart
   Search" on a photo, or wait for a new upload) and check this app's log
   - see [Verifying OpenVINO is active](#verifying-openvino-is-active)
   below.

## Configuration

| Option | Description |
|---|---|
| `workers` | Number of ML worker processes. Default `1`. Each worker loads its own copy of models into memory, so raising this trades RAM for throughput under concurrent requests. |
| `model_ttl` | Seconds of inactivity before an idle model is unloaded from memory, to free RAM between jobs. Default `300`. `0` disables unloading. |
| `worker_timeout` | Seconds before a stuck/slow inference request kills and restarts its worker. Default `300`. Raise it if large jobs (big batches, first-time model downloads) get killed before finishing. |

## Verifying OpenVINO is active

Trigger an ML job (face detection or smart search) from Immich, then check
this app's log. You should see a line listing the available ONNX Runtime
execution providers, including OpenVINO, for example:

```
Available ORT providers: ['OpenVINOExecutionProvider', 'CPUExecutionProvider']
```

If `OpenVINOExecutionProvider` is missing from that list, OpenVINO isn't
being used and jobs are silently falling back to CPU - double check
`/dev/dri` is present on the host (`ls /dev/dri` over SSH) and that the
app's log doesn't show a GPU/driver initialization error higher up.

You can also confirm GPU utilization directly on the host with
`intel_gpu_top` (if available) while a job is running.

## Networking

This app exposes its REST API directly on port `3003` (configurable from
the app's **Network** tab) for the Immich server to reach. There's no web
UI and no Ingress - this is a backend service, not something you browse to
directly.

## GPU access

The app sets `video: true` (HAOS's hardware category for mapping in
available video/render devices) alongside explicit `/dev/dri` and
`/dev/dri/renderD128` entries in `devices`. A bare `devices: [/dev/dri]`
without `video: true` was tried first and looked correct - ONNX Runtime
listed `OpenVINOExecutionProvider` first with no fallback warning - but
the iGPU never actually clocked up during inference
(`/sys/class/drm/card0/gt_act_freq_mhz` stayed at `0`), so something about
plain device mapping alone wasn't sufficient on HAOS. The app runs as
root inside its container either way (no `USER` directive anywhere in
the image), so it isn't a matter of the container's own user needing
`render`/`video` group membership on the host side.

## Persistent storage

Downloaded ML models (multi-GB) are cached in the app's own persistent
`/data` directory (`MACHINE_LEARNING_CACHE_FOLDER`), so they survive app
restarts and updates and aren't re-downloaded every time the app starts -
unlike the upstream image's own default cache location, which isn't
persisted at all outside this app's storage.

## Keeping the version in step with your Immich server

This app's version **must be kept in step with your Immich server's
version** - a Machine Learning sidecar on a different version than the
Immich server can cause instability or outright failures. Your Immich
server's version is shown in its own admin UI footer
(**Administration → Server Stats**, or the page footer). To update this
app to match:

1. Edit the `FROM` line's implicit version - this app's own **version**
   field in `config.yaml` is used directly as the upstream image tag
   (e.g. `v3.0.2-openvino`). Bump it to match your Immich server's
   version with `-openvino` appended.
2. Update the app (Supervisor rebuilds from the new tag) and restart.

This app currently tracks `v3.0.2-openvino`. Check the [Immich releases
page](https://github.com/immich-app/immich/releases) for the latest
version, and the [immich-machine-learning package
page](https://github.com/immich-app/immich/pkgs/container/immich-machine-learning)
to confirm an `-openvino` build exists for it before bumping.

## RAM usage

OpenVINO acceleration uses noticeably more RAM than plain CPU inference
(the Intel GPU compute runtime and OpenVINO's own model representations
add overhead on top of the models themselves). Make sure the host has
headroom beyond what CPU-only inference would need, especially if you
raise `workers` above `1`.

## Troubleshooting

- **Models keep re-downloading on every restart**: confirm the app's
  persistent storage isn't being reset - this shouldn't happen under
  normal use, since models live under `/data`, which Home Assistant
  preserves across app restarts and updates.
- **`OpenVINOExecutionProvider` missing from the log** (see
  [above](#verifying-openvino-is-active)): usually means `/dev/dri` isn't
  reaching the container. Confirm it exists on the host and that the
  app's **Hardware** section (or `devices` in its configuration) lists
  it.
- **Immich server can't reach this app**: confirm the URL entered in
  Immich's Machine Learning Settings uses the Home Assistant host's IP
  (not `localhost`, which would refer to the Immich server app's own
  container) and port `3003`, and that this app is actually running.
- **Instability after an Immich server update**: check whether this app's
  version has drifted out of step with the server - see
  [above](#keeping-the-version-in-step-with-your-immich-server).

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **Immich Machine
Learning (OpenVINO)** from the app store. You'll also need an existing
Immich server app to point at it.

### Applying repo changes that don't bump the version

This app's `version` field always equals the exact upstream image tag it
builds from - it can't be bumped without also changing which image gets
pulled. That means a repo change that doesn't touch which upstream tag is
used (a `config.yaml` hardware-access fix, for example) won't make
Supervisor show an "Update available" banner, since it only compares
versions. To pick up that kind of change: **uninstall the app, then
reinstall it** (not just "Rebuild") - this guarantees Supervisor re-reads
the current `config.yaml` in full, including things like `devices`, that
a plain rebuild isn't guaranteed to reconcile for an already-installed
app at an unchanged version. Do **not** select "delete data" when
uninstalling, so the downloaded models under `/data` survive and don't
need re-downloading.
