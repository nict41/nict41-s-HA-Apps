# NAS1 USB Link Watcher

![NAS1 USB Link Watcher logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/nas1_usb_watcher/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Watches a USB-attached drive's port in sysfs and publishes an MQTT `binary_sensor` that shows **Problem** whenever the drive's USB data link drops, so drops and recoveries are visible and can trigger notifications.

## Features

- A read-only sysfs check: no Docker API, helper container or host PID access
- MQTT discovery, with a stable entity ID and a `last_change` attribute
- Uses the Mosquitto broker credentials from the Supervisor, so no password is stored
- Refuses to start rather than report a false "disconnected" if it can't see the USB bus

> [!NOTE]
> Built for one specific drive ("NAS1"); set the `usb_port` option to point it at yours.

## Documentation

Setup, options and troubleshooting are in the app's **Documentation** tab in Home Assistant, or in [DOCS.md](DOCS.md).

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
