# Changelog

## 0.1.6

- Fix 0.1.5's mobile default, which could never fire on the devices that
  needed it. It applied only when no `resize` setting was stored, but the
  client persists whatever it resolves — `initSetting()` ends with
  `setSetting(name, val)` — so every visit made before 0.1.5 had already
  written `resize: "remote"` into that browser. A phone that had opened the
  panel even once was therefore skipped, and kept shrinking the desktop to
  phone width. The default is now applied once per browser behind its own
  marker key, regardless of what is already stored, and never again, so a
  later explicit choice still survives.
- Reproduced and verified against a real KasmVNC 1.3.3 server and the real
  Kasm web client, driven by a real mobile browser: a browser carrying
  `resize: "remote"` from an earlier session stays on Remote Resizing with the
  old guard and moves to Local Scaling with the new one.

## 0.1.5

- A phone now opens on a usable desktop. The client's default is Remote
  Resizing, which makes the desktop the size of the browser window; on a phone
  that is a few hundred pixels wide, and the slicer's own dialogs are wider
  than that, so their buttons sat off the edge of a screen that cannot scroll —
  a dead end on first launch. Touch devices now start in Local Scaling, which
  fits the whole desktop to the screen. Stored as a default rather than forced
  in the URL, so anything chosen in the client's Settings panel still wins, and
  only written when nothing is stored.
- Correct the mobile documentation. 0.1.4 claimed the viewport change would
  give pinch-to-zoom in the sidebar. It does not: a viewport meta applies only
  to the top-level document, and in the sidebar the page runs inside Home
  Assistant's iframe, so Home Assistant's viewport governs. Browser pinch works
  on the published port only. In the sidebar, magnification is the client's
  "None" resize mode plus its pan button. Documented as such, including the
  distinction from the client's own pinch gesture, which zooms the model.

## 0.1.4

- Network storage now shows up in the slicer's own file dialogs. Every mount
  under `/media` and `/share` is added to the GTK file chooser's shortcut list
  at startup, so a NAS attached in Settings > System > Storage appears by name
  — one click from any open or save dialog, rather than a walk down from the
  filesystem root. `extra_folders` adds anywhere else. Shortcuts are appended
  only, so reordering or deleting one in the file chooser sticks.
- Each device keeps its own sizing. The page hardcoded `resize=remote`, and the
  client reads the URL before its own stored settings, so a desktop and a phone
  could not both be right. It is dropped from the URL — same default, but a
  phone can now pick Scale in the client's Settings panel and have it stick.
- Pinch to zoom works on phones. The page shipped `user-scalable=no`; the
  add-on allows scaling, since pinching is the only way to read a toolbar drawn
  for a larger screen. (Distinct from the client's own pinch gesture, which
  forwards Ctrl+scroll and zooms the model rather than the screen.)
- New `resolution` option for the starting desktop size, default 1440x900,
  replacing the base image's hardcoded 1024x768. The build fails if upstream
  ever moves that line, rather than silently dropping the option.

## 0.1.3

- The sidebar fix in 0.1.2 did not resolve the error for at least one install.
  Rather than guess again, this adds the instrumentation needed to tell the
  remaining explanations apart: whether requests reach the ingress listener at
  all, what Home Assistant actually sends as `X-Ingress-Path`, and whether the
  client ever goes on to request the websocket.
- The page and socket requests are logged to the add-on log. Opening
  `<sidebar-url>/__elegoo` reports what the listener sees, including the
  running version.
- The path injection no longer depends on the exact quoting of the markup it
  is rewriting.

## 0.1.2

- **Fix the sidebar, which never worked.** The Kasm web client builds its
  websocket URL from the origin rather than the page location, so behind
  ingress it asked Home Assistant's own root for `/websockify`, got nothing,
  and died with `Cannot read properties of undefined (reading 'lastActiveAt')`.
  Ingress now has its own listener that injects the correct path per request
  from `X-Ingress-Path` and proxies the socket. Direct access on port 3000 is
  unchanged.
- A password no longer puts a second login in front of the sidebar. Home
  Assistant already authenticates that listener, so `web_password` now guards
  the direct port only, which is what it was for.

## 0.1.1

- Fix the startup script exiting silently when it found no options file. It
  bailed before doing any work, so a password set in the UI was never applied
  and the GPU/software-rendering report never printed — the log said only
  "exited 0", which was true, useless, and indistinguishable from working.
- Graphics detection now runs regardless of the options file, and every path
  through the script logs what it did and why.
- Stop the base image's Docker-in-Docker service from restart-looping. It
  starts dockerd whenever `/dev/cpu_dma_latency` is present — which it is, as a
  side effect of the device access for the iGPU — and dockerd cannot run in an
  unprivileged add-on, so the log filled with `modprobe: not found` and
  `Could not mount /sys/kernel/security` on repeat. Nothing here wants Docker.

## 0.1.0

- Initial release, packaging [ElegooSlicer](https://github.com/ELEGOO-3D/ElegooSlicer)
  v1.5.3.5 as a browser-served desktop on the LinuxServer KasmVNC base
  (Ubuntu 24.04 — the binary requires GLIBC_2.38 and GLIBCXX_3.4.32, which
  22.04 cannot provide)
- Sidebar access through Home Assistant ingress; port 3000 left unmapped by
  default, with `web_username` / `web_password` for when it is exposed
- `/dev/dri` requested for hardware rendering, with an automatic fallback to
  software rendering and a log line saying which is in use
- Printer profiles, presets and projects persist in the add-on's own storage;
  `share` and `media` are mapped for moving models and G-code in and out
- Runtime libraries derived from the AppImage's own `DT_NEEDED` entries rather
  than guessed, with a build-time `ldd` gate that fails the build on any
  unresolved library
- amd64 only, matching the single x86-64 build upstream publishes
