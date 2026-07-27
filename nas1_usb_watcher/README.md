# Home Assistant App: NAS1 USB Link Watcher

![NAS1 USB Link Watcher logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/nas1_usb_watcher/logo.png)

Watches the **NAS1** external drive's USB port in sysfs and publishes a Home
Assistant `binary_sensor` over MQTT that flips to **problem** whenever the
drive's flaky USB *data* link drops - so drops and recoveries are visible in
HA and can be notified on, instead of silently cascading into downstream
failures.

## Why

NAS1 is a WD Red 2 TB drive in a USB3 enclosure (a "LITEON ULTRA 1" /
JMicron bridge). It intermittently drops its USB **data** connection while
staying powered. SMART rules the drive itself out:

- `Reallocated_Sector_Ct = 0`, `Current_Pending_Sector = 0` - no bad sectors.
- `Power_Cycle_Count` barely moves despite frequent `dmesg`
  "USB disconnect" events - so the drive isn't losing power, isn't sleeping,
  and it isn't autosuspend.

That points at a flaky physical data-line connection (most likely the
connector itself). This one drive backs Samba shares, an Immich library, and
Supervisor's NAS backup mount, so each silent drop had been cascading into
downstream breakage (Immich reimport loops, failed backups) that had to be
cleaned up after the fact. Recovery today is manual - nudge the connector,
restart Samba - but there was **no visibility** into when a drop happened or
how long it lasted. This add-on provides exactly that visibility.

## What it produces

- **Entity:** `binary_sensor.nas1_usb_watcher_nas1_usb_link`
  (friendly name **NAS1 USB Link**, `device_class: problem`, so it shows
  **red / "Problem"** when the link is down and clear when it's up).
- **Device:** grouped under an MQTT device named **NAS1**
  (identifier `nas1_usb_watcher`).
- **Attribute:** `last_change` - the UTC timestamp of the most recent
  transition.

### MQTT topics (kept stable on purpose)

| Purpose | Topic | Payload |
|---|---|---|
| Discovery | `homeassistant/binary_sensor/nas1_usb_link/config` | the discovery JSON (retained) |
| State | `watcher/nas1_usb/state` | `connected` / `disconnected` (retained) |
| Attributes | `watcher/nas1_usb/attributes` | `{"last_change":"<UTC ISO8601>"}` (retained) |

These topics and the `unique_id` (`nas1_usb_link`) are **identical to the
original hand-created watcher's**, so this add-on takes over the *same*
entity rather than creating a duplicate.

### The automation it feeds

An existing automation in `automations.yaml` (id `1785000000001`, alias
**"NAS1 USB Link Change"**) triggers on this entity's state changes and
notifies `notify.mobile_app_oneplus_10_pro` with the drop / recovery time.
Because this add-on preserves the entity_id, that automation keeps working
unchanged - no edit needed.

## How it works (and why it's *not* privileged like cpu_governor)

The watcher just checks whether the directory
`/sys/bus/usb/devices/<usb_port>` exists (default `2-1`). Present = the USB
device is enumerated (**connected**); absent = it dropped off the bus
(**disconnected**).

This is a **read-only** sysfs check, which is a fundamentally lighter ask
than [`cpu_governor`](../cpu_governor), which needed to *write* to `/sys` -
something Supervisor add-ons can't do even with `full_access` +
`privileged:` caps, forcing that add-on into a real `--privileged --pid=host`
Docker sidecar. Reading whether a sysfs directory exists needs none of that:
no sidecar, no `--pid=host`, no `docker_api`. This add-on sets only
`full_access: true`, which is enough to make the host USB subsystem visible
under `/sys` inside the container. (It deliberately does **not** set
`host_pid`, which would break the s6-overlay base image's PID-1 init - see
`cpu_governor`'s notes.)

If the add-on genuinely can't see the USB subsystem at all
(`/sys/bus/usb/devices` missing or empty), it refuses to start rather than
publish a permanent false "disconnected" - and the log tells you the plain
`full_access` approach wasn't sufficient on your host, at which point the
heavier `cpu_governor`-style privileged approach would be the fallback. On
this install the plain approach is expected to be enough.

## Configuration

| Option | Description |
|---|---|
| `usb_port` | sysfs USB device name of NAS1's port, checked under `/sys/bus/usb/devices/`. Default `2-1`. Change only if the drive moves to a different physical port (`ls /sys/bus/usb/devices/` on the host to find the new value). |
| `poll_interval` | Seconds between presence checks. Default `5`. It's a cheap directory-existence check, so short intervals are fine. |

## MQTT credentials

The broker host, port, username and password are injected by Supervisor via
`services: [mqtt:want]` and read with `bashio::services mqtt ...` - **no
password is hardcoded** (unlike the original hand-rolled container). You need
the **Mosquitto broker** add-on installed and the MQTT integration set up;
if no broker is available when this add-on starts it waits briefly, then
exits with a clear message so Supervisor surfaces the problem.

## Verifying end-to-end

1. Start the add-on and watch its log. You should see:
   ```
   Watching NAS1 USB port 2-1 (/sys/bus/usb/devices/2-1) every 5s.
   Published MQTT discovery config to homeassistant/binary_sensor/nas1_usb_link/config.
   NAS1 USB 2-1 is connected (as of ...).
   ```
2. In HA, confirm `binary_sensor.nas1_usb_watcher_nas1_usb_link` exists and
   reads **OK / clear** (off = connected).
3. **Physically wiggle / unplug the connector** to force a drop. Within
   `poll_interval` seconds the entity should go to **Problem**, the log
   should show a `disconnected` line, and the **"NAS1 USB Link Change"**
   automation should fire the mobile notification. Reconnect and confirm it
   clears.

### The real test: a full host reboot

The point of `boot: auto` is that this survives a host reboot, not just an
add-on restart. After it's working, **reboot the HA host**
(**Settings → System → Hardware → power menu → Reboot Host**, or
`ha host reboot`), wait for it to come back, and confirm:

- the entity still exists and still updates, and
- wiggling the connector still flips it and still fires the notification.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps) to
add this repository to Home Assistant, then install **NAS1 USB Link
Watcher** from the app store and leave `boot: auto` on. You'll also need the
Mosquitto broker add-on for MQTT.

## Notes & caveats

- **`usb_port` is host-specific.** `2-1` is where NAS1 currently enumerates;
  if you replug it into a different port it may change. The value maps
  directly to a directory under `/sys/bus/usb/devices/`.
- **"disconnected" means "not enumerated on the USB bus"**, which is exactly
  the failure mode here (data link drops while power stays on). It is not a
  SMART/health signal about the drive's media.
- This is a monitor, not a fix - it makes the flakiness *visible*. Recovery
  is still a connector nudge + Samba restart until the enclosure/connector
  is replaced.
