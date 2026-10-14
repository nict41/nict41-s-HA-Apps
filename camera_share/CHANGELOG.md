# Changelog

## 1.0.1

- **`fps` is now capped at 10** (was 15). This is a camera-safety limit
  rather than a bandwidth one: the ceiling stays at 10 until a full idle
  test has passed on the real printer. The default of 5 is unchanged, so
  nothing needs doing unless you had set it above 10, in which case Home
  Assistant will ask you to correct the option before the app starts.
- **Failures now log the exception class, not just its message.** httpx's
  timeout exceptions carry an empty message, so a real incident logged as
  `Camera subscription failed: ` and hid that it was a `ReadTimeout`. The
  class name is the diagnosis, and it is now always there.
- **A stuck frame hub is named as such.** Home Assistant accepting the
  connection and then sending nothing - not even response headers - is a
  different fault from the camera going quiet mid-stream, and the two are
  now reported differently: *"accepted the stream but sent no headers in
  20s - the frame hub is most likely stuck"*, with the fix (reload the
  Elegoo integration) in the message. A connect timeout says so too.
- **The app logs the host port to point the tunnel at**, read from its own
  Network configuration at startup. A default in `config.yaml` only applies
  to a fresh install - an app that already existed under the same slug
  keeps the mapping it had - so the docs no longer claim to know the port,
  and say to read the Network tab.
- The startup warning about a blank `access_key` is now impossible to
  scroll past, since it decides whether the page is public to anyone who
  learns the hostname.

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
