# Home Assistant App: WordPress

![WordPress logo](logo.png)

Self-hosted WordPress for Home Assistant, built on the official
[docker-library/wordpress](https://github.com/docker-library/wordpress) image
(`wordpress:php8.3-apache`).

## Prerequisites

This app needs a MySQL-compatible database. It does not bundle one, so
install one first, for example the official **MariaDB** app from the
Home Assistant Community Add-ons (or any other app/external server that
speaks the MySQL protocol):

1. Install the MariaDB app and start it.
2. In the MariaDB app's configuration, add a database (e.g. `wordpress`)
   and a login (e.g. user `wordpress` with a strong password) for that
   database.
3. Note the MariaDB app's hostname. Apps reach each other on the
   internal Docker network using `core-mariadb` (or whichever slug the
   database app uses) as the hostname.

## Configuration

| Option | Required | Description |
|---|---|---|
| `db_host` | yes | Hostname of the MySQL/MariaDB server, e.g. `core-mariadb`. |
| `db_port` | yes | Port of the database server. Default `3306`. |
| `db_name` | yes | Database name to use for WordPress. |
| `db_user` | yes | Database user. |
| `db_password` | yes | Database password. |
| `table_prefix` | yes | WordPress table prefix. Default `wp_`. |
| `locale` | no | WordPress locale to install, e.g. `de_DE`. Leave unset for English. |
| `debug` | no | Enable `WP_DEBUG`. Default `false`. |
| `config_extra` | no | Raw PHP injected into `wp-config.php` for advanced settings (e.g. `define('WP_MEMORY_LIMIT', '256M');`). |

After filling these in, start the app and open the **Web UI** (or
`http://<home-assistant-host>:8080`) to finish the normal WordPress
installation wizard (site title, admin user, etc.).

## Networking

The app exposes WordPress directly on port `8080` (configurable from the
app's **Network** tab). Home Assistant Ingress is intentionally not used:
WordPress generates absolute URLs for assets, links and the admin area, which
does not work well behind Ingress's path-prefixed proxy.

If you need HTTPS, put this app behind a reverse proxy app (e.g. Nginx
Proxy Manager) rather than relying on Ingress.

## Persistent storage

The entire WordPress installation (core files, themes, plugins and uploads
in `wp-content`) is stored under the app's own persistent `/data`
directory, so it survives app restarts and updates. It is included in
Home Assistant's normal app backups/snapshots.

WordPress core updates (via the in-admin updater or plugin/theme updates)
are written there directly and persist normally. Updating this app
itself only changes the underlying PHP/Apache/WordPress-core base image; it
does not touch your existing `/data` content.

## Updating the bundled WordPress/PHP version

This app tracks the `php8.3-apache` tag of the official image. To pick up
a different PHP version, edit the `FROM` line in the app's `Dockerfile`
and rebuild.

## Troubleshooting

- **"Error establishing a database connection"**: double-check `db_host`,
  `db_port`, `db_name`, `db_user` and `db_password` against what you
  configured in your MariaDB/MySQL app, and make sure that app is
  running.
- App logs show whether it is still waiting for the database to become
  reachable on startup.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **WordPress**
from the app store. Remember to install and configure a MySQL-compatible
database app first — see [Prerequisites](#prerequisites) above.
