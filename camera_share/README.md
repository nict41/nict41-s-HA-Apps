# Camera Share

![Camera Share logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/camera_share/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Shares **one** Home Assistant camera as a read-only web page, so you can publish it to friends (for example through a Cloudflare tunnel) without giving anyone a Home Assistant login.

## Features

- One connection to Home Assistant however many people are watching: frames are fanned out from memory
- Per-viewer frame-rate cap, and the camera is only subscribed to while someone is watching
- Built for cameras that tolerate only one connection, such as the Elegoo Centauri Carbon 2 chamber camera
- No Home Assistant credentials ever reach the viewer

> [!NOTE]
> Needs the [Elegoo integration fork](https://github.com/nict41/HACS-elegoo-integration) 2.12.3.0 or newer when used with an Elegoo printer camera.

## Documentation

Setup, options and troubleshooting are in the app's **Documentation** tab in Home Assistant, or in [DOCS.md](DOCS.md).

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
