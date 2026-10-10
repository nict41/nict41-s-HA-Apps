# Changelog

## 1.1.1

- Move the vendored bashio from 0.17.5 to 0.19.0.
- 0.18.0 renamed every `bashio::addon.*` function to `bashio::app.*` with
  no aliases left behind, which is the one way this bump could have bitten.
  This app uses none of them — only `bashio::config`,
  `bashio::config.has_value`, `bashio::log.*` and `bashio::exit.nok`,
  all of which still exist at 0.19.0 and were loaded and called from the
  0.19.0 library to confirm it.
- 0.19.0 also turns on `inherit_errexit`, so a failing command inside a
  `$(...)` now aborts the script rather than passing an empty value along.
  Every command substitution here was checked against that: the
  `bashio::config` calls all return success, and the pipelines that can
  fail already carry their own `|| true`.
- The install itself is unchanged — the release still ships a `lib/`
  directory with the `bashio` entry script, which is what the Dockerfile
  moves into place.

## 1.1.0

- Move to `smp46/pingvin-share-x:v2.0.0`, up from v1.21.0.
- **Back up before updating.** The upstream entrypoint runs
  `prisma migrate deploy` at start, and 2.0.0 adds columns and tables to
  the schema. The migration is applied to the database in `/data` and is
  one-way: going back to 1.x afterwards is not supported. Take a Home
  Assistant backup of this add-on first.
- **S3 users: one breaking change applies to you.** 2.0.0 moves S3 to
  direct uploads and downloads with presigned URLs, which means the
  bucket now needs CORS rules permitting your Pingvin origin. Everyone on
  the default local storage under `/data` is unaffected, and the app's own
  S3 settings page links to the rules it needs.
- Nothing in this add-on changed. Every upstream file it reaches into is
  byte-identical between the two releases — `scripts/docker/entrypoint.sh`
  it hands off to, `scripts/docker/create-user.sh` kept as the entrypoint
  for `PUID`/`PGID`, the Caddyfile that fronts port 3000, and the
  `/opt/app` layout the branding symlink and `config.yaml` depend on.
  Storage is still SQLite through `DATABASE_URL`.
- New upstream features, none of which need configuring here: per-user
  storage quotas and feature permissions, account display names, a
  customisable password policy, sharing directly with other users, HTTP
  range requests, and a reorganised admin configuration menu. The
  `config_yaml` option still works — upstream kept support for it
  explicitly in this release.

## 1.0.0

- Initial release, based on `smp46/pingvin-share-x:v1.21.0`.
