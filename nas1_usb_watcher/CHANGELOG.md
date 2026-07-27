# Changelog

## 1.0.0

- Initial release. Replaces the hand-created `nas1-usb-watcher` container
  with a proper, versioned, `boot: auto` add-on.
- Polls `/sys/bus/usb/devices/<usb_port>` (default `2-1`) every
  `poll_interval` seconds (default `5`) and publishes an MQTT-discovery
  `binary_sensor` (`device_class: problem`) that reads "problem" while the
  drive's USB data link is dropped.
- Uses the exact same MQTT topics and `unique_id` (`nas1_usb_link`) as the
  original hand-rolled watcher, so it drives the **same** entity
  (`binary_sensor.nas1_usb_watcher_nas1_usb_link`) the existing "NAS1 USB
  Link Change" automation already references - not a duplicate.
- MQTT broker credentials are injected by Supervisor via
  `services: [mqtt:want]` (read with `bashio::services mqtt ...`), so no
  password is hardcoded.
- Reads sysfs only, so no privileged/host-pid/sidecar escalation is needed
  (unlike `cpu_governor`, which had to *write* to `/sys`); `full_access` is
  set only to guarantee the host USB subsystem is visible under `/sys`.
- Refuses to publish if the USB subsystem isn't visible at all
  (`/sys/bus/usb/devices` missing or empty), to avoid reporting a false
  permanent "disconnected".
