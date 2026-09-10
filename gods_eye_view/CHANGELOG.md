# Changelog

## 0.1.2

- Fix the app never getting past its loading screen under ingress. Home
  Assistant proxies ingress with aiohttp `params=request.query`, which parses
  the query into a MultiDict and re-encodes it — so Vite's valueless module
  markers arrive rewritten, `?import&url` becoming `?import=&url=`. Vite then
  no longer recognises a `?url` asset import and serves the raw file where the
  browser expects a JS module, killing the module graph on a syntax error
  before `main.js` ever runs. nginx now restores the markers before proxying.
- This was the original hang: two `.geojsonl` layers are imported that way, so
  the failure was total, and the splash sat on its initial text forever with
  nothing in the log to explain it.

## 0.1.1

- Report browser-side diagnostics into the add-on log as `[client]` lines: boot
  phase, uncaught errors, console warnings, and requests that never finish.
  The globe runs in the browser, so a stall there previously left no trace in
  Home Assistant at all and could not be diagnosed from the Log tab.
- Print a one-off outbound reachability check at start, so a DNS sink or a
  firewall that drops rather than rejects is distinguishable from a bug here.
- Add `client_diagnostics` to turn the `[client]` reporting off.

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
