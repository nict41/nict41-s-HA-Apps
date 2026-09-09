# Changelog

## 0.1.0

- Initial release, packaging [God's Eye View](https://github.com/bilawalsidhu/gods-eye-view)
  at upstream commit `7596522`
- Sidebar access through Home Assistant ingress and direct access on port 8037,
  both served by one instance
- Provider keys (Cesium ion, Google Maps, OpenAI, AISStream, NASA FIRMS,
  TomTom, Launch Library 2, OpenSky) configurable as app options, plus
  `extra_env` for everything else upstream's `.env.example` documents
- Keys saved from inside the app, response caches and debug logs persist in
  `/data` across restarts and updates
- Multi-arch: amd64, aarch64
