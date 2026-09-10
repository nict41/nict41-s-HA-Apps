# Changelog

## 0.2.2

- Fix "Missing option 'auth_session_days' in root" when saving the
  configuration. `auth_session_days` and `client_diagnostics` were declared as
  required, and updating an add-on does not backfill new defaults into an
  existing install's saved options — so an install predating those options had
  no value for them and every save was refused. Both are optional now.
- A missing `client_diagnostics` means on, as its default always intended.
  It was previously read in a way that treated absent as off.

## 0.2.1

- Fix the login redirect being unreachable through a tunnel. nginx expands a
  relative redirect using its own scheme, host and port, so a client arriving
  at `https://your-host/` was sent to `http://your-host:8037/__gev_ha/login` —
  a port the tunnel does not publish and a scheme it does not serve. Redirects
  are now relative, and the browser resolves them against wherever it actually
  is. Ingress benefits from the same fix.

## 0.2.0

- Add a login screen for the published port, for exposing the app through a
  tunnel: set `auth_username` and `auth_password`, and port 8037 serves a
  styled login to anyone without a session. Sessions are signed cookies
  (HttpOnly, SameSite=Lax, Secure over HTTPS), last `auth_session_days` days,
  and are invalidated by changing either credential. Failed attempts back off
  per address, one minute up to fifteen.
- Ingress moves to its own unpublished listener (port 8099 inside the
  container). The sidebar therefore never sees the login, and the login cannot
  be bypassed by forging a header, because the separation is a socket rather
  than a check. No user-visible change to the sidebar.
- Left unconfigured, the published port stays open exactly as before, and the
  log now says so at every start.
- Note: a login does not protect the Google Maps key, which is embedded in the
  page by design. Restrict it by HTTP referrer and cap its quota at Google —
  see the docs.

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
