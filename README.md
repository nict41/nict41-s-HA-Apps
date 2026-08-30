# nict41's HA Apps

This repository can be used as an app repository for Home Assistant.

[![Open your Home Assistant instance and show the app store with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fnict41%2Fnict41-s-HA-Apps)

## Apps

This repository contains the following apps

### [3D Print Timelapse](./print_timelapse)

![3D Print Timelapse logo](print_timelapse/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Captures a frame-by-frame timelapse of each 3D print and compiles it into a GIF, with long-term storage of finished GIFs.

### [CPU Performance Governor](./cpu_governor)

![CPU Performance Governor logo](cpu_governor/logo.png)

![Supports amd64 Architecture][amd64-shield]
![Supports aarch64 Architecture][aarch64-shield]

Sets the host CPU cpufreq scaling governor (default performance) so CPU-bound jobs get full clock speed instead of the powersave default, and re-applies it on every host boot.

### [Glance Dashboard](./glance)

![Glance logo](glance/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]
![Supports armv7 Architecture][armv7-shield]

Self-hosted dashboard for RSS, weather, bookmarks, calendars, stocks, and more.

### [Immich Machine Learning (OpenVINO)](./immich_ml)

![Immich Machine Learning logo](immich_ml/logo.png)

![Supports amd64 Architecture][amd64-shield]

Standalone Immich Machine Learning sidecar, hardware-accelerated with Intel OpenVINO, for offloading face detection and smart search from a separate Immich server app.

### [Kiwix](./kiwix)

![Kiwix logo](kiwix/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Offline Wikipedia and other ZIM archives: browse the Kiwix catalog, download archives onto a NAS share, and read them in the sidebar through kiwix-serve.

### [NAS1 USB Link Watcher](./nas1_usb_watcher)

![NAS1 USB Link Watcher logo](nas1_usb_watcher/logo.png)

![Supports amd64 Architecture][amd64-shield]
![Supports aarch64 Architecture][aarch64-shield]

Watches the NAS1 external drive's USB port in sysfs and publishes an MQTT binary_sensor that flags when the drive's flaky USB data link drops, so drops and recoveries are visible and notifiable.

### [Parcel Tracker](./parcel_tracker)

![Parcel Tracker logo](parcel_tracker/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Automatically detects tracking numbers from your shipping emails (AliExpress, eBay, Amazon, and more) and tracks delivery status.

### [Pingvin Share](./pingvin_share)

![Pingvin Share logo](pingvin_share/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Self-hosted file sharing platform (a WeTransfer alternative), built on the actively-maintained Pingvin Share X fork.

### [WordPress](./wordpress)

![WordPress logo](wordpress/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]
![Supports armhf Architecture][armhf-shield]
![Supports armv7 Architecture][armv7-shield]
![Supports i386 Architecture][i386-shield]

Self-hosted WordPress, built on the official docker-library/wordpress image.

See each app's own README/DOCS for configuration options and setup steps.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
[armhf-shield]: https://img.shields.io/badge/armhf-yes-green.svg
[armv7-shield]: https://img.shields.io/badge/armv7-yes-green.svg
[i386-shield]: https://img.shields.io/badge/i386-yes-green.svg
