# Camera Share

![Camera Share logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/camera_share/logo.png)

Shares **one** Home Assistant camera as a read-only web page on a LAN port,
so you can publish it to friends through the
[Cloudflared app](https://github.com/brenner-tobias/addon-cloudflared) without
giving anyone a Home Assistant login.

It was written for the chamber camera on an Elegoo Centauri Carbon 2, which
is why it is as careful as it is: that camera allows exactly **one**
connection and its firmware crashes when something opens a second.

```
HA camera_proxy_stream  (one subscription from this app, only while >= 1 viewer)
   |
   +-> this app's fan-out (latest frame in memory)
         +-> viewer 1   (MJPEG, per-viewer fps cap)
         +-> viewer 2
         +-> ...
```

Ten people on the page is still **one** connection to Home Assistant, and
Home Assistant's own frame hub is still the only thing talking to the
printer.

## ⚠️ Requirement: Elegoo integration 2.12.3.0 or newer

This app needs
[nict41/HACS-elegoo-integration](https://github.com/nict41/HACS-elegoo-integration)
at **2.12.3.0 or newer**. Check the version in **Settings → Devices &
Services → Elegoo → ⋮ → Version**, and update through HACS if it is older.

That release added the **frame hub**: the integration holds the single
connection to the printer's camera itself and fans the frames out to every
Home Assistant consumer, starting on demand and stopping 60 seconds after
the last consumer goes away. Before the hub existed, each consumer opened
its own connection to the printer - and an earlier version of this app
crashed the camera firmware doing exactly that.

So: never point this app, or anything else, at the printer's camera
directly. Everything goes through Home Assistant.

## What it serves

| Path | What it is |
|---|---|
| `/` | The page: a live view, a status line, nothing to click. |
| `/stream.mjpg` | The MJPEG stream the page's `<img>` reads. |
| `/snapshot.jpg` | The latest frame from this app's buffer, if it is fresh (otherwise `503`). It never wakes the camera - with nobody watching there is nothing to serve. |
| `/healthz` | Status as JSON: upstream state, viewer count, age of the last frame. Needs no access key and starts nothing, so it is safe to point an uptime monitor at. |

There are no other routes. The app cannot be made to proxy any other Home
Assistant endpoint or any other entity, and the Supervisor token never
leaves the container.

## Setup

### 1. Install and configure

| Option | Default | What it does |
|---|---|---|
| `camera_entity` | `camera.centauri_carbon_2_chamber_camera` | The one camera to share. |
| `page_title` | `3D Printer Cam` | Heading and tab title. Everyone you share the link with sees it. |
| `access_key` | _(blank)_ | Shared secret the page demands. **Set this.** |
| `fps` | `5` | Cap on frames per second, per viewer, and the rate asked of Home Assistant. |
| `max_viewers` | `5` | How many people may watch at once. |

The app listens on container port `8099`, mapped to host port **8096** by
default (8095 is already taken by Pingvin Share). Change it on the app's
**Network** tab if you need to.

### 2. Set an access key

Put something long and random in `access_key` - the output of
`openssl rand -hex 16` is ideal. The app logs a **warning** at startup if it
is blank, because the page is about to be published on the internet.

Without a key, anyone who learns the hostname can watch your printer.

### 3. Point a Cloudflare tunnel at it

In the **Cloudflare Zero Trust** dashboard, open **Networks → Tunnels**, pick
the tunnel the Cloudflared app runs, and add a **public hostname**:

| Field | Value |
|---|---|
| Subdomain | e.g. `printer` |
| Domain | your domain |
| Type | `HTTP` |
| URL | `http://<your Home Assistant LAN IP>:8096` |

Use the LAN IP (e.g. `192.168.1.20`), not `localhost` - the tunnel runs in
its own container and `localhost` there is not Home Assistant.

Nothing else needs forwarding, and this app is the only thing behind that
hostname: the tunnel never sees Home Assistant itself.

### 4. Share the link

```
https://printer.yourdomain.com/?key=<your access key>
```

The key is checked once and then remembered in a cookie (HttpOnly, SameSite
=Lax, and Secure over the tunnel), so a reload or a bookmark of the bare
address keeps working. The page also carries the key into its own stream
URL, so it still works for anyone who blocks cookies outright.

A wrong key gets `403`, a missing one `401`, and neither page says anything
about what the key is.

### 5. Optional: put Cloudflare Access in front of it

If you would rather hand out logins than a key, add a **Cloudflare Access**
policy to that hostname (e.g. one-time PIN to a list of email addresses).
Access and the `access_key` work together - keep the key set as a backstop
in case a policy is ever edited by accident.

## Watching the camera

The page shows a live view, a status line (**Live** / **Connecting** /
**Offline** / **Busy**) and a "read-only view" footer. There is nothing to
click, no way to move the camera, and no way to reach anything else.

When the camera stops sending, the page swaps the image for a placeholder
and keeps trying in the background, backing off as it goes. It recovers on
its own the moment frames return - nobody needs to reload anything.

A tab in the background lets go of its viewer slot, and the app releases the
camera about 15 seconds after the last viewer leaves. Nothing is being
watched means nothing is connected.

## How it protects the camera

- **One subscription, total.** Opened when the first viewer arrives, closed
  about 15 seconds after the last one leaves. Never one per viewer, and
  never a snapshot poll.
- **Stall detection.** When the printer's camera server dies the HTTP
  response stays open and simply goes quiet, so silence is the only symptom
  there is. Ten seconds without a frame closes the subscription rather than
  holding it open forever.
- **Backed-off reconnects.** 2 s, 4 s, 8 s … up to 60 s, with jitter, reset
  once frames flow again. There is no code path that retries in a tight
  loop.
- **Frames are skipped, not queued.** Each viewer is sent whatever the
  latest frame is when it is ready for one. A slow phone on a train falls
  behind in time, never in memory, and can't hold up anyone else.

## Troubleshooting

**"Camera offline" on the page.** The printer's camera server has stopped
sending. This is the common one, and it is a printer problem rather than an
app problem: check the app log for `no data for 10s - the printer's camera
server has most likely stopped sending`.

Power-cycle the printer to bring it back — **only when it is not printing**.
If the camera is dead mid-print, leave it; the print is fine, and the page
will pick the camera back up by itself when the printer is next restarted.

**"Camera busy".** `max_viewers` is reached. Raise it if your connection can
carry it; the printer sees one connection either way.

**The page says Connecting and never goes Live.** Check the app log.

- `Home Assistant returned 404` — the `camera_entity` is wrong, or the
  Elegoo integration isn't loaded. Confirm the entity id in **Developer
  tools → States**.
- `Home Assistant returned 500` or no boundary in the Content-Type — almost
  always an Elegoo integration older than 2.12.3.0, i.e. no frame hub.
- `No SUPERVISOR_TOKEN` — reinstall the app; it is granted the Home
  Assistant API by its own config.

**Nothing loads through the tunnel but the LAN address works.** The tunnel's
public hostname is pointing somewhere else. It must be
`http://<HA LAN IP>:8096`, not `https://`, not `localhost`, not Home
Assistant's own port.

**The link works once and then asks for the key again.** The cookie was
refused (private browsing, or a cookie-blocking extension). Share the full
`?key=` link instead of the bare hostname - it works every time.

**Is the frame hub really only connected once?** Watch the Home Assistant
log with the Elegoo integration at debug level: the hub logs under `CC2CAM`,
and should show one connection opening when the first person loads the page,
whatever happens after that.

## Privacy

This page is public once you tunnel it. It shows a camera and a title and
nothing else — no Home Assistant branding, no entity names, no other
entities, no API. Still, give it an access key and a title that doesn't say
where you live.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **Camera Share** from
the app store.
