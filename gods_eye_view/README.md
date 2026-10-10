# God's Eye View for Home Assistant

![God's Eye View logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/gods_eye_view/logo.png)

A real-time intelligence console for planet Earth, running as a Home Assistant
app: a photorealistic 3D globe with live aircraft, ships, satellites,
earthquakes, wildfires, traffic and public cameras, a cockpit view for riding
along with anything you track, and optional voice control.

This packages [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view)
(MIT). All of the app's own behaviour, data sources and limits are upstream's —
this app adds the Home Assistant wiring around it.

## Access

The app is reachable two ways, and both serve the same instance:

- **Sidebar (ingress)** — click **God's Eye View** in the Home Assistant
  sidebar. Nothing is exposed on your network; access is authenticated by
  Home Assistant.
- **Direct** — `http://<your-ha-host>:8037`. Change or remove the port under
  the app's **Configuration → Network** tab.

Direct access has no authentication until you give it some. Set `auth_username`
and `auth_password` and that port gets a login screen; see *Exposing it beyond
your LAN* below. Left unset, anyone who can reach the port can use the app, and
your provider quota with it.

## Exposing it beyond your LAN

The sidebar is authenticated by Home Assistant. The published port is not, so
0.2.0 adds a login for it — set `auth_username` and `auth_password` and port
8037 serves a login screen to anyone without a session.

The two are separate nginx listeners, not one listener with a check. Ingress
gets its own unpublished port, so a tunnelled request cannot reach it by
forging a header, and the sidebar never asks you to sign in twice. Sessions are
signed cookies (`HttpOnly`, `SameSite=Lax`, and `Secure` when the request
arrived over HTTPS), last `auth_session_days` days, and are invalidated wholesale
by changing either credential. Repeated failures from one address back off, from
one minute up to fifteen.

Leaving both options empty keeps the port open, and the log says so at every
start.

### What the login does not protect

Your Google Maps key is **embedded in the page by design** — the geocode call is
made from the browser, so anyone who signs in can read the key out of devtools
and use it elsewhere. The login stops anonymous use of your quota; it does not
protect the key itself. Two things do:

- **Restrict the key at Google** to an HTTP referrer matching the hostname you
  serve from — your tunnel hostname, and your Home Assistant origin if you use
  the sidebar too. A key restricted this way is useless to anyone who lifts it.
- **Cap the quota** per API under *APIs & Services → Quotas* in Google Cloud
  Console. That makes requests fail once you hit the cap rather than bill you.

The same applies to `cesium_ion_token` and `mapillary_client_token`. Every
other key stays server-side.

`google_maps_server_api_key` exists for exactly this problem: put a second,
IP-restricted Google key there and the calls made from inside the container
(place search, the Street View camera fallback) use it, leaving the key in the
page scoped to tiles and geocoding alone.

If you are already fronting this with Cloudflare, putting Cloudflare Access in
front of the tunnel is stronger than this login and costs nothing at your scale
— it authenticates at the edge, so unauthenticated traffic never reaches your
machine at all. This login is there so the port is never open on its own.

## It works with no keys at all

Start the app and you get a working globe: aircraft, satellites, earthquakes,
public cameras, bike-share, terrain, radio. Keys are upgrades, each unlocking
one more layer. Every option below is optional and free-tier unless marked
metered.

## Options

| Option | Unlocks | Where to get it |
|---|---|---|
| `cesium_ion_token` | Google photorealistic 3D tiles and world terrain | [Cesium ion](https://ion.cesium.com/tokens) — free for personal use |
| `google_maps_api_key` | Direct 3D tiles and place search (**metered**) | [Google Maps Platform](https://developers.google.com/maps/documentation/tile/get-api-key) |
| `google_maps_server_api_key` | Optional second Google key for the server-side calls, so the browser-exposed one can stay narrow | same place — see above |
| `mapillary_client_token` | The Street Level layer: street-level photos and 360s on the globe | [Mapillary](https://www.mapillary.com/dashboard/developers) — free |
| `openai_api_key` | Voice control and the AI HUD (**metered**, roughly $0.01–0.05 per minute of speech) | [OpenAI](https://platform.openai.com/api-keys) |
| `aisstream_api_key` | Live ship positions worldwide | [AISStream](https://aisstream.io) — free |
| `firms_map_key` | Live active-fire detections | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/map_key/) — free |
| `tomtom_api_key` | Live traffic flow | [TomTom](https://developer.tomtom.com) — free tier |
| `ll2_api_token` | Higher rate limits on rocket launch data | [Launch Library 2](https://thespacedevs.com) — optional |
| `opensky_client_id` + `opensky_client_secret` | Higher-rate aircraft polling | [OpenSky](https://opensky-network.org) — free |
| `extra_env` | Anything else upstream's `.env.example` documents | see below |
| `client_diagnostics` | Browser-side errors and stalls in the add-on log (on by default) | — |
| `auth_username` + `auth_password` | A login screen on the published port; leave empty to keep it open | — |
| `auth_session_days` | How long a login lasts (default 7) | — |

Setting both OpenSky values also switches the app to OAuth automatically.

### `extra_env`

Upstream supports around thirty further environment variables — voice model and
voice selection, CCTV source lists, per-provider rate limits, the TomTom daily
tile budget, AIS bounding boxes. Any of them can be set here as `NAME=value`
strings:

```yaml
extra_env:
  - OPENAI_REALTIME_VOICE=marin
  - OPENAI_REALTIME_MODEL=gpt-realtime-2
  - TOMTOM_DAILY_TILE_BUDGET=2000
  - CCTV_MAX_SOURCES=400
  - CCTV_VEGVESEN_ENABLED=0
```

Three upstream defaults changed under you in 0.3.0, and all three are
overridable here: the OpenAI routes are now throttled to 30 requests per minute
per client and the Google Places routes to 120, where both were unlimited
(`GEV_RATELIMIT_OPENAI_PER_MIN`, `GEV_RATELIMIT_GOOGLE_PER_MIN`; `0` disables),
and `TOMTOM_DAILY_TILE_BUDGET` fell from 40,000 to 6,000 so a 31-day month
stays inside TomTom's free allowance. The CCTV packs also grew a lot — the
catalog ceiling is now 4,000 cameras across a dozen countries, each pack with
its own `CCTV_*_ENABLED` and `CCTV_*_MAX_SOURCES` switch.

The full list, with explanations, is in
[`.env.example`](https://github.com/bilawalsidhu/gods-eye-view/blob/main/.env.example)
upstream. Restart the app after changing anything.

## The in-app "POWER UP" panel

The app has its own Provider Settings panel for pasting keys. In this app it is
**read-only**: keys you set in the app options show up there as *externally
managed*, which is the same state upstream uses for launcher-managed installs.

That is deliberate: a writable credential form on port 8037 would be writable
by anything that can reach the port. Manage keys in **Configuration → Options**
instead — they are stored by Home Assistant like any other app secret.

Until 0.3.0 this fell out of upstream's own gate, which refuses proxied
requests and anything but a local `Host`. 0.3.0 has to present a local `Host`
to get the metered routes working at all (see *Two ways in, one server*), so
the panel's two endpoints are refused by the internal nginx instead. `/mcp`,
upstream's local agent endpoint, is refused there for the same reason.

Keys the panel did manage to save before (or that you place there yourself) live
in `/data/.env` inside the app and survive restarts and updates. An app option
always wins over a value in that file.

## Two ways in, one server

Home Assistant serves apps from `/api/hassio_ingress/<token>/`, so the app has
to build every URL with that prefix. Vite is started with the live ingress path
as its `--base`, an internal nginx normalises prefixed and unprefixed requests
so both entry points reach the same server, and a small injected script prefixes
the root-absolute URLs the app builds at runtime (`/api/...`, `/models/*.glb`).

There is one more piece, and it is the reason the app would not load at all
before 0.1.2. Home Assistant proxies ingress with aiohttp
`params=request.query`, which parses the query string into a MultiDict and
re-encodes it. Valueless parameters do not survive that round trip:
`?import&url` — how Vite marks a `?url` asset import — arrives as
`?import=&url=`, and Vite then serves the raw file instead of the JS module
the browser is importing. nginx restores those markers before proxying.

And one more, added in 0.3.0. Upstream now checks three things on its
cost-bearing routes — place search, voice, the AI HUD, Street Level — and the
add-on failed all three by passing the request on as the client had made it:

- **Forwarding headers.** `X-Forwarded-For`, `X-Real-IP`, `Via` and
  Cloudflare's `CF-Connecting-IP` among them, all read as proof a request came
  from somewhere else and answered with 403. They are cleared rather than just
  not set, so a tunnel that adds its own does not trip the gate either.
- **`Origin`.** It has to equal the authority the app works out from its own
  scheme and `Host`. The hop from nginx to the app is plain http, so a client
  on https could never match.
- **`Host`.** `HOST=0.0.0.0` used to mean "accept any `Host`". It no longer
  does, and the `.local` suffix is no longer accepted by default either, so a
  forwarded `homeassistant.local` is refused outright.

So nginx now addresses the app as `127.0.0.1:4173`, with a matching `Origin`
and no forwarding headers. Nothing is given up by this: the app listens on
loopback inside the container and nginx is its only caller, so the `Host` it
sees was never a boundary here. The boundary is the two listeners — ingress
authenticated by Home Assistant, the published port with its optional login.

Two consequences worth knowing:

- Direct access on port 8037 serves the page at `/`, but its assets are fetched
  under the ingress-shaped path. That is expected, not a misconfiguration.
- If you reinstall the app, Home Assistant issues a new ingress token. The app
  picks it up on the next start.

## Resource use

This runs upstream's Vite dev server, because every live-data proxy the app
depends on — OpenSky, CelesTrak, CCTV, FIRMS, TomTom, Overpass, GBFS, adsb.lol,
terrain — is registered as dev-server middleware. A production build would drop
them.

Practical effects: the image lands around half a gigabyte, mostly Cesium. The
build installs roughly 250 MB of dependencies — seconds on a fast x86 machine,
a few minutes on a Pi with slow storage — and none of it compiles, so there is
no long native build stage. The server then idles at a few hundred MB of RAM.

The globe itself is rendered by your **browser**, not by Home Assistant, so the
machine you view it on needs the GPU, not the one running Home Assistant.

## When it will not load

The globe runs in your browser, so a stall there used to leave nothing at all
in the add-on log. It now reports what only the page can see. Open the add-on
**Log** tab and look for `[client]` lines:

```
[client] 1.7s  phase: Initializing systems...
[client] 14.5s console.warn: [MapStack] Esri World Imagery unavailable, falling back to OSM
[client] 20.2s stalled: still on "Initializing systems..." after 20.2s; 13 request(s) open: GET https://tile.openstreetmap.org/...
[client] 28.6s ready: globe ready after 28.6s
```

What to read from them:

- **`phase:`** — which step the splash is on. The last one printed is where it
  stopped.
- **`stalled:`** — printed every 20 s while the splash is still up. It lists
  what is still in flight, and says explicitly when **nothing** is in flight,
  which means it is waiting on something that is not the network.
- **`pending:` / `xhr-failed:` / `fetch-failed:`** — individual requests that
  never came back. The host name is usually the whole diagnosis. Repeats from
  one host are capped, so a `suppressed:` line means "more of the same".
- **`ready:`** — how long the globe took.

A normal cold start reaches `ready` in **under 30 seconds**, and it does so
*even when every map provider is unreachable*: the app falls back Esri → OSM →
flat terrain on its own, each after roughly a 12-second timeout. So a splash
that never clears is not explained by blocked map tiles alone.

The log also prints a one-off reachability check at start, which tells you
whether the add-on container itself can get out:

```
Reachability: Esri basemap OK (HTTP 200)
Reachability: CelesTrak FAILED — no response within 8s
```

Set `client_diagnostics: false` to turn the `[client]` reporting off. Path
rewriting is unaffected — only the reporting stops.

## Storage

Everything persistent lives in the app's own `/data`:

| Path | Contents |
|---|---|
| `/data/.env` | keys saved from inside the app |
| `/data/cache` | CelesTrak, FIRMS, TomTom and terrain response caches |
| `/data/logs` | voice and realtime debug logs |

## Troubleshooting

**The sidebar panel is blank or stuck on "Initializing systems…"**
See *When it will not load* above — the `[client]` log lines name what it is
waiting on.

**The log says it could not read the ingress path**
The sidebar panel will not work, and the log says so. Direct access on port 8037
still works. Restart the app; if it persists, reinstall so Supervisor reissues
the ingress token.

**Aircraft stop updating**
Keyless OpenSky polling is rate-limited per source IP. Add
`opensky_client_id` and `opensky_client_secret` for a higher allowance.

**Voice control does nothing**
It needs `openai_api_key`, and the browser needs microphone permission — which
browsers only grant on `https://` or `localhost`. Through the sidebar on an
HTTPS Home Assistant this works; over plain `http://<host>:8037` it will not.

## Upstream's own boundaries

Upstream states the project does not build features for named-person search,
face recognition, or tracking individuals, and that it is a client for
exploration and learning rather than a hardened production service. Both hold
here. All data comes from public feeds.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps) to add
this repository to Home Assistant, then install **God's Eye View** from the app
store.
