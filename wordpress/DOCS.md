# Home Assistant App: WordPress

![WordPress logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/wordpress/logo.png)

Self-hosted WordPress for Home Assistant, built on the official
[docker-library/wordpress](https://github.com/docker-library/wordpress) image
(`wordpress:php8.3-apache`), with a **built-in MariaDB database server** —
no other apps or external services needed.

## Quickstart

1. Install the app and start it. No configuration is required: with the
   default (empty) `db_host`, the app runs its own MariaDB server
   internally and creates the WordPress database and user automatically on
   first start.
2. Open the **Web UI** (or `http://<home-assistant-host>:8080`) and finish
   the normal WordPress installation wizard (site title, admin user, etc.).

Everything lives in the app's own persistent storage: the WordPress
installation in `/data/wordpress`, the database in `/data/mysql`, and daily
database dumps in `/data/backups`. The auto-generated database password is
kept at `/data/.db_password` — you normally never need it (you can also set
your own via the `db_password` option instead).

## Configuration

| Option | Required | Description |
|---|---|---|
| `db_host` | no | Leave **empty** to use the built-in database server (recommended). Set to a hostname (e.g. `core-mariadb`) to use an external MySQL/MariaDB server instead — see [External database](#external-database-advanced). |
| `db_port` | no | Port of the external database server. Default `3306`. Ignored when using the built-in database. |
| `db_name` | yes | Database name to use for WordPress. Default `wordpress`. |
| `db_user` | yes | Database user. Default `wordpress`. |
| `db_password` | no | Password for the database user. Required when `db_host` is set; with the built-in database, leave empty to auto-generate one. |
| `table_prefix` | yes | WordPress table prefix. Default `wp_`. |
| `locale` | no | WordPress locale to install, e.g. `de_DE`. Leave unset for English. |
| `debug` | no | Enable `WP_DEBUG`. Default `false`. |
| `config_extra` | no | Raw PHP injected into `wp-config.php` for advanced settings (e.g. `define('WP_MEMORY_LIMIT', '256M');`). Applied on the next restart, even on an existing install. |

## Backups & restore

Home Assistant's normal app backups include everything: the WordPress
files, the database, and the dumps.

Because a backup can be taken while the database is running, the app also
writes a compressed SQL dump of the built-in database to `/data/backups/`
on every start and once a day while running (the newest 7 are kept). This
guarantees every backup contains a restorable copy of your site's content
even in the unlikely case the raw database files inside it were copied
mid-write.

Restore paths:

- **Normal case**: restoring a Home Assistant backup restores the whole
  site — nothing else to do.
- **Automatic disaster recovery**: if the app ever starts with a fresh
  (missing/empty) database directory but finds dumps in `/data/backups`, it
  automatically restores the newest one and says so in the log. (To reset
  the site on purpose instead, delete `/data/backups` along with
  `/data/mysql`.)
- **Manual restore / import**: place a dump named `wordpress-restore.sql`
  (or `.sql.gz`) in Home Assistant's `share` folder and restart the app.
  It is imported into the WordPress database and the file is renamed to
  `*.imported-<timestamp>` so it doesn't run twice.

The manual import is also the **migration path from an external database to
the built-in one**: dump your external database (e.g. from the MariaDB app:
`mysqldump wordpress > /share/wordpress-restore.sql`), clear `db_host` in
this app's configuration, and restart.

## External database (advanced)

If you prefer to keep the database in a separate service (for example the
official **MariaDB** app from the Home Assistant Community Add-ons, or any
external MySQL-compatible server), set `db_host`, `db_password` and the
other `db_*` options:

1. Install the MariaDB app and start it.
2. In the MariaDB app's configuration, add a database (e.g. `wordpress`)
   and a login (e.g. user `wordpress` with a strong password) for that
   database.
3. Note the MariaDB app's hostname. Apps reach each other on the
   internal Docker network using `core-mariadb` (or whichever slug the
   database app uses) as the hostname.
4. Set `db_host` to that hostname and fill in `db_password` (and the other
   `db_*` options if you changed them).

**Upgrading from 1.0.0 with an external database: nothing changes.** Your
saved `db_host`/`db_password` keep working exactly as before; the built-in
server only starts when `db_host` is empty. The startup dumps and
auto-restore only apply to the built-in database.

## Networking

The app exposes WordPress directly on port `8080` (configurable from the
app's **Network** tab). Home Assistant Ingress is intentionally not used:
WordPress generates absolute URLs for assets, links and the admin area, which
does not work well behind Ingress's path-prefixed proxy.

If you need HTTPS, put this app behind a reverse proxy app (e.g. Nginx
Proxy Manager) rather than relying on Ingress.

### Application Passwords (e.g. for Jetpack)

WordPress core hides the Application Passwords feature (used by Jetpack and
other tools that authenticate against the REST API) unless the site is
served over HTTPS or its environment type is `local`. Fix depends on how
you access the site:

- **LAN-only access** (e.g. `http://<home-assistant-host>:8080`, no real
  HTTPS anywhere): set `config_extra` to
  `define('WP_ENVIRONMENT_TYPE', 'local');` and restart.
- **Behind a reverse proxy or tunnel that terminates real HTTPS**
  (Cloudflare Tunnel, Nginx Proxy Manager, etc.): the proxy talks to this
  app over plain HTTP internally, so WordPress needs to trust the
  `X-Forwarded-Proto` header it sets to know the original request was
  HTTPS. Set `config_extra` to:

  ```php
  if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {
      $_SERVER['HTTPS'] = 'on';
  }
  ```

  Then manage Application Passwords (and use Jetpack) via the site's real
  public URL, not the LAN IP - the REST API calls this feature relies on
  are built from WordPress's configured **Site Address**
  (Settings → General), so they only succeed when accessed at that same
  URL.

`config_extra` is applied on every restart (including on an existing
install, not just when the site is first created) - no rebuild needed.

## Persistent storage

The entire WordPress installation (core files, themes, plugins and uploads
in `wp-content`) is stored under the app's own persistent `/data`
directory, so it survives app restarts and updates — as are the built-in
database (`/data/mysql`) and its dumps (`/data/backups`). All of it is
included in Home Assistant's normal app backups/snapshots.

WordPress core updates (via the in-admin updater or plugin/theme updates)
are written there directly and persist normally. Updating this app
itself only changes the underlying PHP/Apache/WordPress-core base image; it
does not touch your existing `/data` content.

## Updating the bundled WordPress/PHP version

This app tracks the `php8.3-apache` tag of the official image. To pick up
a different PHP version, edit the `FROM` line in the app's `Dockerfile`
and rebuild.

## Troubleshooting

- **Built-in database fails to start**: check the app log — the most
  common cause on small boards is running out of memory (the bundled
  MariaDB is tuned small, roughly 150–250 MB). The log shows MariaDB's own
  error messages on startup.
- **"Error establishing a database connection"** (external database):
  double-check `db_host`, `db_port`, `db_name`, `db_user` and `db_password`
  against what you configured in your MariaDB/MySQL app, and make sure
  that app is running.
- App logs show whether it is still waiting for an external database to
  become reachable on startup.
- **Downgrading to 1.0.0**: version 1.0.0 requires an external `db_host`,
  so after a downgrade the app refuses to start until one is configured.
  Your data in `/data/mysql` and `/data/backups` is left untouched, and
  re-upgrading resumes normally.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **WordPress**
from the app store. No database setup is needed — the app is fully
self-contained out of the box.
