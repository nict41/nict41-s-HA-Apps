# Kiwix Manager for Docker

The same app as the [Home Assistant Kiwix add-on](../README.md), packaged for
any Docker host. Browse the Kiwix catalog, download Wikipedia and other ZIM
archives, and read them offline in the browser. Features:

- **Catalog browser** for library.kiwix.org (or your own mirror), filtered by
  language and category, with a quick-pick list of every Wikipedia edition
  and size.
- **Downloader** that resumes after pauses and restarts, splits one archive
  across up to 8 connections, runs up to 6 archives at once, and can keep
  transfers to an off-peak window (e.g. 23:00-07:00).
- **Library** of what's on disk: choose which archives are served, or
  delete them.
- **Reader**: kiwix-serve runs inside the container and is proxied under
  `/kiwix`, so the whole app is a single port.
- **Settings panel** for concurrency, connections, language and the
  download window. Changes apply live and are saved in `/data`.

## Quick start

```yaml
services:
  kiwix:
    image: ghcr.io/nict41/kiwix-manager:latest
    container_name: kiwix
    restart: unless-stopped
    ports:
      - "8099:8099"
    volumes:
      - ./kiwix-data:/data
      - /mnt/storage/kiwix:/zim
    environment:
      TZ: Europe/London
      PUID: "1000"
      PGID: "1000"
      KIWIX_USERNAME: kiwix
      KIWIX_PASSWORD: change-me
```

`docker compose up -d`, then open `http://<host>:8099`. A fuller example
with every option is in [docker-compose.yml](docker-compose.yml).

Images are published for `linux/amd64` and `linux/arm64`. To build one
yourself from a checkout of the repository:

```sh
docker build -f kiwix/docker/Dockerfile -t kiwix-manager kiwix
```

## Volumes

| Path | What it holds |
|------|---------------|
| `/zim` | The `.zim` archives. Use a large disk, because a full English Wikipedia is over 100 GB. |
| `/data` | App state: served selection, download queue, saved settings. Small. |

The container writes to `/zim` as `PUID:PGID`. Set those to the owner of the
host folder (`id <user>` on the host). The container chowns `/data` at start,
but it never chowns `/zim`, which may be a NAS share.

## Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `PUID` / `PGID` | `1000` | User and group the app runs as. |
| `TZ` | `UTC` | Timezone used for the off-peak download window. |
| `KIWIX_USERNAME` / `KIWIX_PASSWORD` | unset | Turns on a login prompt (HTTP Basic) for the whole app. **Set these** unless a reverse proxy already authenticates. Without them, anyone who can reach the port can download and delete archives. |
| `ZIM_PATH` | `/zim` | Storage folder inside the container. |
| `LIBRARY_SOURCE` | `https://library.kiwix.org` | Catalog to browse. |
| `CATALOG_LANGUAGE` | `eng` | ISO 639-3 language the catalog opens on. |
| `AUTO_SERVE_NEW` | `true` | Serve each finished download automatically. |
| `MAX_CONCURRENT_DOWNLOADS` | `1` | Archives downloading at once (1-6). |
| `DOWNLOAD_THREADS` | `1` | Connections per archive (1-8). 4 is a good start. |
| `DOWNLOAD_WINDOW_ENABLED` | `false` | Only download between the times below. |
| `DOWNLOAD_WINDOW_START` / `_END` | `23:00` / `07:00` | Off-peak window, 24-hour. It may cross midnight. |
| `BASE_PATH` | empty | Only for a reverse proxy at a subpath (see below). |
| `PORT` | `8099` | Port the app listens on inside the container. |

The download settings are starting values. Anything you change in the app's
Settings panel wins over them, and survives restarts.

## Behind a reverse proxy

At its own (sub)domain, just proxy to port 8099. No extra config is needed.

Under a subpath such as `https://example.com/kiwix-app/`, set
`BASE_PATH=/kiwix-app` and have the proxy **strip** the prefix before
forwarding. The reader needs the prefix to build its links. With nginx:

```nginx
location /kiwix-app/ {
    proxy_pass http://kiwix:8099/;
    proxy_set_header Host $host;
    proxy_buffering off;          # stream large article assets
}
```

If the proxy authenticates (Authelia, Authentik, basic auth), you can leave
`KIWIX_USERNAME`/`KIWIX_PASSWORD` unset.

## Health check

`GET /healthz` answers without credentials, and the image's own
`HEALTHCHECK` uses it.
