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

Direct access has **no authentication of its own**. Anyone who can reach that
port can use the app, and therefore your provider keys and their quota. If your
Home Assistant is reachable from the internet, leave the port unmapped and use
the sidebar only.

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
| `openai_api_key` | Voice control and the AI HUD (**metered**, roughly $0.01–0.05 per minute of speech) | [OpenAI](https://platform.openai.com/api-keys) |
| `aisstream_api_key` | Live ship positions worldwide | [AISStream](https://aisstream.io) — free |
| `firms_map_key` | Live active-fire detections | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/map_key/) — free |
| `tomtom_api_key` | Live traffic flow | [TomTom](https://developer.tomtom.com) — free tier |
| `ll2_api_token` | Higher rate limits on rocket launch data | [Launch Library 2](https://thespacedevs.com) — optional |
| `opensky_client_id` + `opensky_client_secret` | Higher-rate aircraft polling | [OpenSky](https://opensky-network.org) — free |
| `extra_env` | Anything else upstream's `.env.example` documents | see below |

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
  - CCTV_MAX_SOURCES=40
```

The full list, with explanations, is in
[`.env.example`](https://github.com/bilawalsidhu/gods-eye-view/blob/main/.env.example)
upstream. Restart the app after changing anything.

## The in-app "POWER UP" panel

The app has its own Provider Settings panel for pasting keys. In this app it is
**read-only**: keys you set in the app options show up there as *externally
managed*, which is the same state upstream uses for launcher-managed installs.

That is deliberate on two counts. Upstream's panel only answers unproxied
requests from the local machine, and everything here arrives through the app's
internal proxy; and a writable credential form on port 8037 would be writable by
anything on your network. Manage keys in **Configuration → Options** instead —
they are stored by Home Assistant like any other app secret.

Keys the panel did manage to save before (or that you place there yourself) live
in `/data/.env` inside the app and survive restarts and updates. An app option
always wins over a value in that file.

## Two ways in, one server

Home Assistant serves apps from `/api/hassio_ingress/<token>/`, so the app has
to build every URL with that prefix. Vite is started with the live ingress path
as its `--base`, an internal nginx normalises prefixed and unprefixed requests
so both entry points reach the same server, and a small injected script prefixes
the root-absolute URLs the app builds at runtime (`/api/...`, `/models/*.glb`).

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

Practical effects: the image is around 400 MB, the first build after install
takes several minutes while npm installs Cesium and friends, and the server
idles at a few hundred MB of RAM. The globe itself is rendered by your
**browser**, not by Home Assistant, so the machine you view it on needs the GPU,
not the one running Home Assistant.

## Storage

Everything persistent lives in the app's own `/data`:

| Path | Contents |
|---|---|
| `/data/.env` | keys saved from inside the app |
| `/data/cache` | CelesTrak, FIRMS, TomTom and terrain response caches |
| `/data/logs` | voice and realtime debug logs |

## Troubleshooting

**The sidebar panel is blank or stuck on "Initializing systems…"**
The globe needs to reach map tile providers on the internet. Check the app log
for proxy errors, and confirm Home Assistant itself has outbound access.

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
