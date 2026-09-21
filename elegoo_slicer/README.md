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
| `resolution` | Starting desktop size, `WIDTHxHEIGHT`. Default `1440x900`. Mainly affects phones and tablets; a desktop browser resizes on connect |
| `extra_folders` | Extra paths to offer in the slicer's file dialogs. `/share` and `/media` mounts are added automatically |

These apply to the **direct port only**. The sidebar is served by a separate
listener that Home Assistant has already authenticated, so setting a password
does not put a second login in front of the panel. Leave both empty unless you
are mapping port 3000.

## How the sidebar works

Worth knowing, because it is unusual. The Kasm web client builds its websocket
URL from the page's **origin**, not from the page's location:

```js
url += '://' + host;
if (port) { url += ':' + port; }
url += '/' + path;          // `path` defaults to "websockify"
```

Behind ingress the panel lives at `/api/hassio_ingress/<token>/`, so that
resolves to `ws://your-home-assistant/websockify` — Home Assistant's own root,
which has no such endpoint. The socket never opens, the client discards its
connection object, and its keep-alive timer then fails with
`Cannot read properties of undefined (reading 'lastActiveAt')`. That error is
the symptom; the unreachable socket is the cause.

The client does accept an explicit path via its query string. That is how the
base image supports reverse-proxy subfolders, but it expects a value fixed at
build time and an ingress token is neither fixed nor knowable in advance. So
the add-on runs its own listener on 8099 for ingress, which injects the right
path per request from the `X-Ingress-Path` header and proxies the socket
through. The base image's own listener on 3000 is untouched and still serves
direct access.

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

## Files and network storage

`/share` and `/media` are both mapped in, so anything attached to Home
Assistant under **Settings → System → Storage** — a NAS, a USB disk — is
visible to the slicer.

You should not have to go looking for it. The slicer is a wxGTK application, so
its open and save dialogs are GTK file choosers, and every mount found under
`/media` and `/share` at startup is added to the shortcut list down the left of
those dialogs, under its own name. A share called NAS1 appears as **NAS1**, one
click from any open dialog.

The add-on log says what it found:

```
[elegoo]   file dialogs: added /media/NAS1
```

If nothing is listed, the mount is not reaching the add-on — check it is
attached in Settings → System → Storage, then restart. `extra_folders` adds
anywhere else.

Shortcuts are only ever appended, so if you reorder or delete one from inside
the file chooser it stays that way.

The desktop's home directory is the add-on's persistent volume, so printer
profiles, filament presets and project files survive restarts and updates.

## Desktop and mobile

On a desktop browser the remote desktop resizes to match the window, so it is
always the right size and pixel-sharp. Nothing to configure.

A phone needs more thought, and the reason is worth stating plainly because it
is the opposite of what you might expect.

**Sizing is the lever, not zooming.** The client's default is *Remote
Resizing*: the desktop becomes the size of the browser window. On a phone that
produces a desktop a few hundred pixels wide, and the slicer's own dialogs have
minimum widths larger than that — so their buttons sit off the edge of a screen
that cannot be scrolled. The add-on therefore sets **Local Scaling** as the
starting mode on touch devices, which fits the whole desktop to the screen
instead.

You can change it any time from the control bar (the tab on the left edge) under
**Settings → Resize**:

| Mode | On a phone |
|---|---|
| **Local Scaling** | whole desktop fitted to the screen — everything visible, small. The default here |
| **None** | desktop at full size, with a **pan button** in the control bar to drag around it. Best for reading |
| **Remote Resizing** | desktop matches the window. Right on a desktop browser, a trap on a phone |

That choice is stored per browser, so a phone and a laptop can differ. The
add-on sets the touch default only when nothing is stored, so an explicit
choice is never overridden.

**Pinch-to-zoom.** There are three different things called pinch here, and it
is worth keeping them apart.

1. **The client's own pinch** forwards `Ctrl`+scroll to the slicer, so it zooms
   the *model* in the 3D view. Always available.
2. **Page zoom** would magnify the screen. The add-on allows it, but a
   `viewport` meta applies only to the top-level document — and in the sidebar
   this page runs inside Home Assistant's iframe, so Home Assistant's viewport
   governs and the add-on's is ignored. This works on the **published port**,
   where the add-on *is* the top-level document.
3. **WebView zoom**, which sits above the page and so is not subject to any of
   that. In the Home Assistant **Android** app, turn on
   **Settings → Companion App → Pinch-to-Zoom**. It zooms the whole app,
   including this panel, and is the answer for the sidebar on Android.

On a mobile browser rather than the app: Safari on iOS ignores `user-scalable=no`
and pinches anyway; Chrome on Android honours it, but
**Settings → Accessibility → Force enable zoom** overrides it.

Failing all of that, *None* resize mode plus the pan button magnifies without
any pinching at all.

Set `resolution` to control how big the desktop is to begin with. The default,
1440x900, is wide enough for the slicer's dialogs while still fitting a phone
screen once scaled; a desktop browser overrides it on connect anyway.

Honest limitation: this is a desktop CAD-adjacent application streamed to a
browser. On a phone it is genuinely usable for checking a slice or kicking off
a print, and genuinely awkward for laying out a plate.

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
- It also requires **GLIBC_2.38 and GLIBCXX_3.4.32**. Ubuntu 22.04 ships glibc
  2.35 and libstdc++ 12 (GLIBCXX_3.4.30), so it cannot run this binary at all
  — and that is not something a package install can fix, since glibc is the
  floor the whole image stands on. Ubuntu 24.04 ships glibc 2.39 and
  libstdc++ 14, which is why this is built on `ubuntunoble` and why several
  package names below carry the `t64` suffix from that release's time_t
  transition (`libgtk-3-0t64`, `libglib2.0-0t64`, `libatk1.0-0t64`).

Both of those were found by the build gate rather than by reading, which is
the argument for having it.

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
