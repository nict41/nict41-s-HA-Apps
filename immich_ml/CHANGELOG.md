# Changelog

## v3.0.2-openvino

- Initial release. Runs `ghcr.io/immich-app/immich-machine-learning:v3.0.2-openvino`
  unmodified, with a thin startup script translating the `workers`,
  `model_ttl` and `worker_timeout` options into the image's own env vars,
  `/dev/dri` passed through for Intel iGPU acceleration, and the model
  cache persisted under this app's own `/data` storage.
