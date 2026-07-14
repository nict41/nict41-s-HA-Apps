# Home Assistant App: Pingvin Share

![Pingvin Share logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/pingvin_share/logo.png)

Self-hosted file sharing (a WeTransfer alternative) for Home Assistant, built
on [Pingvin Share X](https://github.com/smp46/pingvin-share-x)
(`smp46/pingvin-share-x`), an actively-maintained fork of the original
[Pingvin Share](https://github.com/stonith404/pingvin-share), which was
archived in June 2025. The fork is drop-in compatible (same image layout,
environment variables and `config.yaml` format), so this app can be
repointed at the original image by editing the `FROM` line in the
`Dockerfile` if you ever prefer that instead.

The app is **fully self-contained**: it uses an internal SQLite database
and local file storage under `/data` by default, no separate database app
needed.

## Quickstart

1. Install the app and start it.
2. Open the **Web UI** (or `http://<home-assistant-host>:8095`) and click
   **Register**. The first account you create automatically becomes the
   admin account - there's no separate setup wizard.
3. Sign in and go to **Admin → Settings** to configure branding, sharing
   rules and (optionally) email, OAuth/LDAP login, S3 storage and legal
   pages. See [Admin UI settings](#admin-ui-settings) below.

## Networking (Cloudflare Tunnel / cloudflared)

The app exposes a single port, `3000/tcp` (mapped to `8095` on the host by
default, configurable from the app's **Network** tab). Everything - the
web UI and the API - is served through that one port via a built-in Caddy
reverse proxy, so a tunnel only needs to point at it; no extra paths or
ports need to be forwarded.

Point your `cloudflared` tunnel's ingress rule at
`http://<home-assistant-host>:8095` (or whatever host port you chose) and
map it to a subdomain of your domain as usual.

Two settings matter once the app is reachable at a real public URL:

- **`trust_proxy`** (this app's own option, enabled by default): tells the
  built-in Caddy proxy to trust `X-Forwarded-For` from whatever sits in
  front of it, so client IPs (used for rate limiting and share visitor
  limits) are recorded correctly instead of showing the tunnel's own
  address.
- **App URL & Secure Cookies** (in the app's own **Admin → Settings →
  General**, not an add-on option): set **App URL** to your real public
  address (e.g. `https://share.yourdomain.com`) - share links are
  generated from it - and enable **Secure Cookies** since the tunnel
  serves the site over real HTTPS. Both default to plain
  `http://localhost:3000`, which only works for local-only access.

### Security note

`allowRegistration` defaults to **on** in the app itself (**Admin →
Settings → Sharing**). That's convenient for creating your own first admin
account, but once the app is reachable from the public internet through
your tunnel, turn it off there unless you actually want open public
sign-ups.

## Adding a Home Assistant sidebar shortcut

The app declares a `webui` link, so **Settings → Add-ons → Pingvin Share**
always has a working **OPEN WEB UI** button. That's one click away, but not
in the sidebar itself.

Home Assistant's own **Ingress** (which gives other apps in this repository,
e.g. Glance, a real sidebar icon) is intentionally not used here, for the
same reason the WordPress app in this repository avoids it: Pingvin Share X
is a Next.js app that generates absolute (root-relative) URLs for its assets
and share links, which breaks once Ingress proxies it under a path prefix
like `/api/hassio_ingress/<token>/` instead of `/`.

The reliable way to get a real sidebar entry pointing at this app is Home
Assistant's built-in `panel_iframe` integration, added to
`configuration.yaml`:

```yaml
panel_iframe:
  pingvin_share:
    title: Pingvin Share
    icon: mdi:share-variant
    url: "https://share.yourdomain.com" # your real tunnel/public URL
```

Use your actual public URL (the same one set as **App URL** in the app, see
[Networking](#networking-cloudflare-tunnel-cloudflared) above) rather than
the LAN `http://<home-assistant-host>:8095` address, so the link keeps
working the same way from outside your network. Restart Home Assistant
Core after adding this for the sidebar entry to appear.

## Known upstream bug: custom links containing `_` silently fail

If you type a custom share link containing an underscore (e.g.
`my_share`) and click **Share**, nothing visibly happens - no error, the
modal just doesn't proceed. **This is a bug in Pingvin Share X itself**,
not something caused by this app's packaging; it reproduces the same way
running the upstream Docker image directly.

Cause: the frontend's own form validation and the backend both accept
`[a-zA-Z0-9_-]`, but a separate client-side check that runs right before
submitting (`isValidId` in `frontend/src/services/share.service.ts`) uses
an older, stricter pattern, `/^[a-zA-Z0-9-]+$/`, which excludes `_`. That
check throws an uncaught error for any link containing an underscore,
which silently aborts the click with no visible feedback.

Workaround: use hyphens instead of underscores in custom links (e.g.
`my-share`) - fully supported and has the same effect. If you'd like this
fixed upstream, it's a one-line regex fix in
[`share.service.ts`](https://github.com/smp46/pingvin-share-x/blob/main/frontend/src/services/share.service.ts) -
worth opening an issue/PR against `smp46/pingvin-share-x`.

## Configuration

| Option | Required | Description |
|---|---|---|
| `trust_proxy` | no | Whether the built-in Caddy proxy trusts `X-Forwarded-For` from what's in front of it. Default `true`, which is correct for the cloudflared setup above (or any other reverse proxy/tunnel). Turn off only if you expose the app with nothing in front of it. |
| `clamav_host` | no | Hostname/IP of a [ClamAV](https://www.clamav.net/) server to scan shares for malicious files. Leave empty (default) to disable scanning. |
| `clamav_port` | no | Port of the ClamAV server. Default `3310`. Ignored when `clamav_host` is empty. |
| `config_yaml` | no | Advanced escape hatch - see [Advanced: config.yaml](#advanced-configyaml). Leave empty (default). |

## Admin UI settings

Everything app-level - the parts an end user would think of as "configuring
Pingvin Share" - is managed from **Admin → Settings** inside the app itself
once you've signed in, not from add-on options:

- **General**: app name, App URL, secure cookies, session duration, home
  page visibility.
- **Sharing**: registration, unauthenticated shares, expiration limits,
  share ID length, max upload size, chunk size.
- **Email**: SMTP server and the templates used for share/reverse-share
  notifications, password resets and invites.
- **Authentication**: LDAP, and OAuth/OIDC login via GitHub, Google,
  Microsoft, Discord or a generic OIDC provider.
- **Storage**: local disk (default, under this app's own `/data`) or S3 /
  S3-compatible object storage.
- **Legal**: optional imprint and privacy policy pages.
- **Appearance** (Pingvin Share X only): theme color, radius, color scheme
  and custom CSS.

This is the same settings surface as the upstream Docker install; the
[project's documentation](https://stonith404.github.io/pingvin-share/setup/configuration)
covers each field in detail.

## ClamAV integration

To scan shares for malicious files, run a [ClamAV](https://www.clamav.net/)
server elsewhere on your network (there isn't a matching ClamAV app in this
repository) and point `clamav_host` (and `clamav_port` if not the default
`3310`) at it. ClamAV needs a fair amount of RAM - see the
[upstream integration docs](https://stonith404.github.io/pingvin-share/setup/integrations/#clamav)
for sizing guidance.

## Advanced: config.yaml

Instead of the Admin UI, Pingvin Share can be configured entirely from a
`config.yaml` file - but **not both at once**: as soon as a `config.yaml`
is present, the app disables editing settings from its own UI. This is
useful for fully declarative, version-controlled configuration, at the
cost of the nicer settings UI.

Two ways to provide it (a file takes precedence over the option):

- Drop a file at `/share/pingvin_share/config.yaml` in Home Assistant's
  `share` folder (edit it with a file-manager or Samba app) and restart.
- Or paste the full YAML into the `config_yaml` option (switch the app's
  **Configuration** tab to **Edit in YAML** to paste multi-line text).

Base it on
[`config.example.yaml`](https://github.com/smp46/pingvin-share-x/blob/main/config.example.yaml)
from the upstream project, which lists every available key (general,
share, cache, email, smtp, ldap, oauth, s3, legal, appearance, initUser).
Removing the file (or clearing the option) and restarting hands control
back to the Admin UI.

## Persistent storage

Everything lives under this app's own persistent `/data`: the SQLite
database, uploaded files (unless S3 storage is configured instead) and
custom branding images uploaded through the Admin UI. All of it is
included in Home Assistant's normal app backups/snapshots.

## Updating the bundled Pingvin Share X version

This app pins an exact image tag (e.g. `v1.21.0`) in its `Dockerfile` for
reproducibility rather than tracking `latest`. To pick up a newer release,
edit the `FROM` line and rebuild - check the
[release notes](https://github.com/smp46/pingvin-share-x/releases) first,
since Prisma database migrations run automatically on start but aren't
reversible.

## Troubleshooting

- **Share links point at `localhost:3000`**: set **App URL** under
  **Admin → Settings → General** in the app to your real public address -
  see [Networking](#networking-cloudflare-tunnel-cloudflared) above.
- **Large uploads fail or stall**: this app always uses the image's
  built-in Caddy proxy (not Home Assistant Ingress), which handles large
  file uploads properly - unlike the standalone (non-Docker) upstream
  install method, this is not a limitation here.
- **Settings page in the app says it can't be edited**: a `config.yaml` is
  active - see [Advanced: config.yaml](#advanced-configyaml). Remove the
  file/option and restart to restore the Admin UI.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **Pingvin Share**
from the app store. No database setup is needed - the app is fully
self-contained out of the box.
