# Changelog

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
