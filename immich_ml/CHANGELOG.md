# Changelog

## v3.3.1-openvino

- Move to `ghcr.io/immich-app/immich-machine-learning:v3.3.1-openvino`,
  three minor releases on from v3.0.2.
- **Update your Immich server to 3.3.1 as well.** Immich expects every
  component to run the same version, and this add-on is a sidecar for a
  server it does not control. If the server stays on 3.0.2, update it
  first or hold this update — a mismatch is the one way this bump bites.
- Nothing in this add-on changed. Everything the startup script relies on
  still holds at 3.3.1: the `MACHINE_LEARNING_` env prefix with the
  `workers`, `model_ttl`, `worker_timeout` and `cache_folder` settings;
  `python -m immich_ml` as the command under the image's own tini
  entrypoint; port 3003 and the `/ping` endpoint the watchdog polls. The
  image is still Debian-based, so bashio still installs the same way.
- The model cache under `/data/model-cache` is kept. 3.3 can fetch
  different model revisions, so expect a one-off re-download on first run
  and a slow first request while it happens.

## v3.0.2-openvino

- Initial release. Runs `ghcr.io/immich-app/immich-machine-learning:v3.0.2-openvino`
  unmodified, with a thin startup script translating the `workers`,
  `model_ttl` and `worker_timeout` options into the image's own env vars,
  `/dev/dri` passed through for Intel iGPU acceleration, and the model
  cache persisted under this app's own `/data` storage.
- Fixed the iGPU never actually being used despite ONNX Runtime listing
  `OpenVINOExecutionProvider` first with no fallback warning
  (`/sys/class/drm/card0/gt_act_freq_mhz` stayed at `0` during inference).
  Added `video: true` and an explicit `/dev/dri/renderD128` device entry
  alongside the existing `/dev/dri` mapping - see [GPU
  access](DOCS.md#gpu-access). This is a config-only change (no new
  upstream tag), so it needs a reinstall rather than an update - see the
  README's install/update notes.
- The log now reports iGPU activity directly (`iGPU active: NNN MHz`
  whenever `/sys/class/drm/card0/gt_act_freq_mhz` is nonzero during a
  job), so you no longer need SSH or `docker exec` to check whether
  OpenVINO is genuinely using the GPU - see [Verifying OpenVINO is
  active](DOCS.md#verifying-openvino-is-active). This is a `run.sh`
  change, so a plain **Rebuild** picks it up (no reinstall needed).
