# Changelog

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
