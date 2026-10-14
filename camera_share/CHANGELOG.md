# Changelog

## 1.0.0

- First release. Serves one Home Assistant camera as a read-only public web
  page on a LAN port, so it can be published through a Cloudflare tunnel
  without handing anyone a Home Assistant login.
- **One subscription to Home Assistant, however many people are watching.**
  The app opens a single `camera_proxy_stream` when the first viewer arrives
  and closes it about 15 seconds after the last one leaves, fanning the
  frames out in memory. The printer's camera allows exactly one connection
  and crashes when something opens a second, so this is the whole point of
  the app rather than an optimisation.
- Requires the Elegoo integration at 2.12.3.0 or newer, whose frame hub owns
  that one connection to the printer and fans it out to Home Assistant
  consumers.
- Detects the failure that actually happens: the printer's camera server
  dying leaves the HTTP response open and silent, so ten seconds without a
  frame closes the subscription rather than holding it open forever.
  Reconnects back off from 2 s to 60 s with jitter, and never retries in a
  tight loop.
- Per-viewer frame rate cap with frames **skipped, not queued**, so a slow
  phone falls behind in time rather than in memory and can't hold up anyone
  else.
- `access_key` option: the key is required once via `?key=`, then remembered
  in an HttpOnly, SameSite=Lax cookie (Secure over the tunnel) that holds a
  digest rather than the key. A startup warning says so loudly when the key
  is left blank.
- `max_viewers` cap, security headers on every response, no CORS, and four
  routes in total - `/`, `/stream.mjpg`, `/snapshot.jpg` and `/healthz`.
  Nothing else in Home Assistant is reachable through this app, and
  `/healthz` deliberately starts nothing.
- Mobile-friendly page with a live status line, a friendly placeholder when
  the camera is offline, and automatic recovery when frames return.
