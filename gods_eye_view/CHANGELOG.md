# Changelog

## 0.3.2

- Fix the add-on failing to start at all: `CACError: Unknown option
  '--configFile'`, then `exited with code 1, stopping add-on`. 0.3.1 selected
  its Vite configuration with `--configFile`, which is the name the JS API
  uses for that setting and is not a CLI option; the flag is `-c, --config`.
  Vite rejects an unknown option before the server starts, so the app never
  came up.
- Assert the wiring while building the image, since every way it can break
  leaves the add-on dead on arrival rather than degraded. The build now fails
  unless Vite's CLI still accepts `--config`, the wrapper config and
  upstream's own config both load, and the resolved config really carries the
  replaced plugin — the last checked through a marker on it, so a wrapper that
  quietly stops replacing anything cannot pass.

## 0.3.1

- Fix the app serving nothing but `{"error":"Unknown API route"}` after the
  0.3.0 update. Upstream gained a catch-all so an unmatched API route answers
  JSON instead of falling through to the page:
  `server.middlewares.use('/api', ...)`. Home Assistant serves an add-on from
  `/api/hassio_ingress/<token>/`, and the app runs with that path as Vite's
  `--base`, so every document, module and asset request arrives under `/api/`
  — and middleware a plugin adds in `configureServer` runs *ahead* of Vite's
  own, so the catch-all answered each one before the base prefix had been
  stripped. The collision was total: the splash screen never appeared, because
  the HTML itself was the error.
- The dev server now starts against a wrapper config that replaces that one
  plugin with an equivalent which steps aside for anything addressed under the
  base. Real API calls are untouched — nginx sends those with the base already
  stripped — so an unmatched route still answers upstream's JSON 404. It is a
  wrapper rather than an edit to upstream's file so that bumping `GEV_COMMIT`
  cannot quietly drop it; if upstream renames or removes the plugin, the
  add-on log says so at start.

## 0.3.0

- Move to upstream `591f299` (app version 0.2.1 plus unreleased work), 564
  commits on from the previous pin. New since 0.2.2: a Street Level layer with
  Mapillary photos and 360s, Norwegian road cameras and much larger CCTV packs
  (Caltrans, TfL, Ontario, Fintraffic, DriveBC, TxDOT, NSW, Calgary, Tallinn
  and others), a rewritten voice assistant that can be pointed at a target and
  asked "what's that", portable and resizable CCTV and Street Level panels, and
  traffic roads from OpenFreeMap vector tiles rather than public Overpass
  instances.
- Fix every cost-bearing route answering 403 on this new upstream. Three
  separate gates landed upstream between the two pins, and the add-on tripped
  all three because it proxied the request as the client had made it:
  forwarding headers (`X-Forwarded-For`, `X-Real-IP`, `X-Forwarded-Proto`,
  `Forwarded`, `Via` and Cloudflare's `CF-Connecting-IP`/`CF-Ray`) are now read
  as proof a request came from elsewhere and refused; an `Origin` has
  to match the authority the app computes from its own scheme and `Host`, which
  a client on https never could across a plain-http hop; and `HOST=0.0.0.0` no
  longer means "accept any Host", with the `.local` suffix dropped from the
  defaults as well, so the forwarded Home Assistant hostname was refused
  outright. nginx now addresses the app as `127.0.0.1:4173` with a matching
  `Origin` and clears all nine forwarding headers — clears rather than simply
  not setting them, because nginx passes an unknown request header through
  verbatim, so a Cloudflare tunnel's own headers would trip the gate on the way
  past. Place search, voice, the AI HUD and the whole Street Level layer depend
  on this.
- Keep Provider Settings read-only and `/mcp` unexposed. Both are gated
  upstream on a loopback `Host`, which the change above now presents, so they
  are refused in nginx instead — a credential-writing form and an MCP endpoint
  on the published port are not what that change was for. Keys still belong in
  the add-on options; nothing about this is new behaviour.
- Add `mapillary_client_token` for the new Street Level layer, and
  `google_maps_server_api_key` for the server-side Google calls (place search
  and the Street View camera fallback). The second key never reaches the
  browser, so it can be restricted by IP while the client-exposed key stays
  scoped to tiles and geocoding. Both optional; left empty nothing changes.
- Note two upstream default changes that now apply here: the OpenAI endpoints
  are throttled to 30 requests per minute per client and the Google Places
  endpoints to 120, where both were unlimited, and the TomTom daily tile budget
  drops from 40,000 to 6,000 to keep a month inside the free allowance. All
  three are still overridable through `extra_env`.

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
