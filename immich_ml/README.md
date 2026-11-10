# Immich Machine Learning (OpenVINO)

![Immich Machine Learning (OpenVINO) logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/immich_ml/logo.png)

![Supports amd64 Architecture][amd64-shield]

A standalone [Immich](https://immich.app/) machine-learning sidecar using Intel OpenVINO, so a separate Immich server can offload face detection and smart search to an Intel iGPU instead of the CPU.

## Features

- Runs the official `immich-machine-learning` `-openvino` image unmodified
- Add it as the first machine-learning URL in Immich and keep the built-in one as a fallback
- Models are cached in the app's persistent storage

> [!NOTE]
> amd64 only, and needs an Intel iGPU at `/dev/dri`. This is not an Immich server on its own.

## Documentation

Setup, options and troubleshooting are in the app's **Documentation** tab in Home Assistant, or in [DOCS.md](DOCS.md).

[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
