# Changelog

## 1.1.6

- `config_extra` now applies on every restart, not just when the site is
  first created. Previously, changing it on an existing install did
  nothing, because the upstream image only evaluates it while generating
  a brand-new `wp-config.php`; a change is now detected and triggers a
  clean regeneration of just that file (the rest of the install and the
  database are untouched).
- Documented how to enable **Application Passwords** (needed by Jetpack
  and similar REST API integrations), which WordPress core hides unless
  the site is served over HTTPS or `WP_ENVIRONMENT_TYPE` is `local` -
  set via `config_extra`.

## 1.1.5

- Fixed a `403 Forbidden` ("client denied by server configuration") on
  every page after the 1.1.4 fix. Apache's `DocumentRoot` was correctly
  repointed at `/data/wordpress`, but its separate `<Directory>` access
  grant (both Debian's own default and the one the base image adds) was
  still scoped to the bare `/var/www/` prefix, so nothing under
  `/data/wordpress` was actually allowed to be served. Both config forms
  are now rewritten.

## 1.1.4

- Fixed another startup crash right after the 1.1.3 fix: `rm: cannot
  remove '/var/www/html': Device or resource busy`. The base image
  declares `/var/www/html` as a Docker `VOLUME`, which makes it a real
  mount point that can't be removed or symlinked over from inside the
  container. Instead of fighting that, Apache's DocumentRoot and the
  container's working directory now point directly at `/data/wordpress`
  (`docker-entrypoint.sh` operates purely relative to the working
  directory, with no hardcoded `/var/www/html` path in it), so the
  WordPress install lives there with no symlink involved.

## 1.1.3

- Fixed the real cause of the startup crash (exit code 141): on a fresh
  install, generating the auto database password piped `tr` from
  `/dev/urandom` into `head -c 24`. `head` exiting after 24 bytes sends
  `tr` a SIGPIPE, and because bashio runs the whole script under
  `set -o pipefail` + `set -e`, that killed the add-on immediately, before
  MariaDB or Apache ever started (the password itself was generated
  correctly; only the script's own exit-code handling caused the crash).
  Removes the diagnostic `set -x` tracing added in 1.1.2.

## 1.1.2

- Diagnostic build: `run.sh` now runs with `set -x` so the log shows every
  command as it executes. The add-on is exiting almost immediately on start
  (exit code 141 / SIGPIPE) before any of its own log lines print, which
  points at something inside bashio's own startup rather than `run.sh`
  itself — this build is to pin down exactly which command breaks. The
  trace will be removed again once the real fix lands.

## 1.1.1

- Fixed the add-on failing to start (`jq: command not found`, every
  `bashio::config` call failing): the Dockerfile vendors bashio directly
  from source instead of using a Home Assistant base image, and never
  installed `jq`, which bashio requires. `jq` is now installed alongside
  the other packages.

## 1.1.0

- The add-on is now **fully self-contained**: leave `db_host` empty (the new
  default) and it runs its own built-in MariaDB server, creating the
  WordPress database and user automatically on first start. No separate
  MariaDB add-on or manual database setup needed. The database password is
  auto-generated (persisted at `/data/.db_password`) unless you set
  `db_password` yourself.
- **External databases keep working unchanged**: if `db_host` is set, the
  add-on behaves exactly like 1.0.0. `db_host` and `db_password` are simply
  optional now.
- A compressed SQL dump of the built-in database is written to
  `/data/backups/` on every start and daily while running (newest 7 kept),
  so a Home Assistant backup taken while the add-on runs always contains a
  restorable copy of the database, not just the raw (possibly mid-write)
  database files.
- If the add-on ever starts with a fresh database directory but finds dumps
  in `/data/backups`, it restores the newest one automatically (disaster
  recovery after an unusable raw-datadir restore).
- Manual restore/migration: drop a `wordpress-restore.sql`(.gz) file into
  Home Assistant's `share` folder and restart — it's imported into the
  WordPress database and renamed. This is also the migration path from an
  external database to the built-in one.
- Clean shutdown handling: stopping the add-on now stops Apache first, then
  shuts the built-in database down cleanly (with a raised Supervisor stop
  timeout to give it time).

## 1.0.0

- Initial release, based on `wordpress:php8.3-apache`.
