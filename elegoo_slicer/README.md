# ElegooSlicer for Home Assistant

![ElegooSlicer logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/elegoo_slicer/logo.png)

[ElegooSlicer](https://github.com/ELEGOO-3D/ElegooSlicer) — Elegoo's fork of
OrcaSlicer — running as a desktop inside a container, reachable from the Home
Assistant sidebar. Slice models and send jobs to your printers without
installing anything on the machine you happen to be sitting at.

Upstream ships Linux as an AppImage only, pinned here at **v1.5.3.5**. Bump
`ELEGOO_VERSION` in `build.yaml` to move to a newer release.

## Access

- **Sidebar (ingress)** — click **ElegooSlicer** in the sidebar. Access is
  authenticated by Home Assistant and nothing is exposed on your network.
- **Direct** — port 3000 is **unmapped by default**. Map it under
  **Configuration → Network** only after setting `web_password`, because the
  desktop gives whoever reaches it a file manager and a terminal-capable
  session on your Home Assistant host.

## Options

| Option | Effect |
|---|---|
| `web_username` | Username for the web interface login (defaults to `elegoo` when a password is set) |
| `web_password` | Password for the web interface. **Empty means no login at all** — the base image treats an unset password as open access |

Both apply to the web interface itself, so they take effect on the sidebar too.
Leave them empty while you are only using the sidebar; Home Assistant is
already doing the authenticating there.

## Printers

Networking is bridged, so the slicer can reach printers on your LAN and send
jobs to them, but the multicast auto-discovery some slicers use to *find*
printers does not cross the bridge. Add each printer by its IP address instead.

If you would rather have discovery, `host_network: true` in `config.yaml`
enables it — at the cost of the container binding host ports directly, and a
wider grant than anything else in this repository.

## Graphics

3D rendering needs a GL stack. The add-on requests `/dev/dri`, so on an Intel
or AMD iGPU it renders on the GPU; where no render node is available it falls
back to software rendering, which works but is noticeably slower to orbit a
large model. The log says which it picked at startup:

```
[elegoo] GPU node /dev/dri/renderD128 present — using hardware rendering
```

## Storage

The desktop's home directory is the add-on's persistent volume, so printer
profiles, filament presets and project files survive restarts and updates.
`share` and `media` are mapped too, which is the easy way to get an STL in and
a G-code file out.

## Architecture

**amd64 only.** Upstream publishes a single x86-64 Linux build and no ARM
binary, so there is nothing to run on a Raspberry Pi or an ARM-based Home
Assistant Green.

## How the dependencies were chosen

Worth recording, because it is the part most likely to break on an upstream
bump. The AppImage bundles ten shared libraries and links against thirty-five
more that it expects from the system. Rather than guess at that list, it was
derived: every `DT_NEEDED` entry across the payload was read, the libraries the
bundle ships were subtracted, and each remaining soname was resolved to its
owning package through the Ubuntu `Contents` index.

Two results from that are worth keeping in mind:

- The binary needs **webkit2gtk-4.1 with libsoup 3**, not the 4.0/libsoup2.4
  generation. They are not interchangeable, and picking the older pair yields
  an image that builds cleanly and then fails to launch.
- Ubuntu 22.04 carries both, which is why this is built on `ubuntujammy`.

The Dockerfile also runs `ldd` over the binary as a build step and fails the
build on any unresolved library, so a future version bump that needs something
new breaks visibly at build time rather than silently at first launch.

### A caveat on upstream's own checks

The AppImage's launcher appears to validate its own runtime — it checks for
host OpenGL and WebKitGTK 4.1 and prints a helpful error naming the packages to
install. That check does not work: `has_host_runtime_library()` declares
`local lib_name` and never assigns its argument, so it searches `ldconfig` for
a bare space, matches any line, and always concludes the library is present.
Running it on a machine with no webkit2gtk-4.1 installed passes without
complaint.

So a missing library will not produce upstream's tidy error message — it will
fail some less obvious way. That is why the build gate exists here rather than
trusting the app to report the problem itself.

## Resource use

The image is large — the desktop base, the GL stack and a 419 MB unpacked
AppImage. The container also holds a running desktop session, so it idles
heavier than a headless add-on. If you only slice occasionally, setting
**Start on boot** to off and starting it when you need it is reasonable.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps) to add
this repository to Home Assistant, then install **ElegooSlicer** from the app
store. The first build downloads and unpacks the AppImage, so give it a few
minutes.
