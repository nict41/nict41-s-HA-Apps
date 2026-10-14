# Changelog

## 0.2.0

- Stop re-checking videos YouTube says are unavailable on every cycle. A
  playlist with a dozen private, removed, members-only, geo-blocked or
  age-restricted entries was spending a dozen of its
  `max_downloads_per_cycle` slots, and a dozen requests against the rate
  limit, re-learning the same thing every hour.
- A failed download is now classified before it is recorded. One that means
  the *video* cannot be fetched escalates through a backoff — 6 hours, a day,
  3 days, a week, then `recheck_unavailable_days` (default 30) — and is
  skipped in the meantime, before the plan is built, so it costs no download
  slot and no request. The cycle summary reports them as
  `skipped as unavailable`.
- Transient failures are deliberately untouched. A captcha, an HTTP 429 or
  403, `aria2c exited with code 22`, a dropped connection or a timeout still
  retries on the very next cycle, and a transient failure clears any backoff
  an earlier one had set. Anything unrecognised counts as transient, so a
  message nobody has seen before costs one retry rather than silently
  retiring a track.
- The classifier is careful about one case in particular: "Sign in to confirm
  you're not a bot" reads like an age gate but is a rate-limit challenge, and
  must never retire a track. It is checked before the age-restriction rules
  and is covered by its own test.
- A backoff rather than a blacklist, because unavailable is not always
  permanent: a private video can be made public again, a geo-block can lift,
  a copyright claim can be resolved, and an age-restricted video starts
  working the moment `cookies_file` is set. Set `recheck_unavailable_days` to
  `0` to retire them outright instead.
- The extractor and video id are stripped from yt-dlp's message before it is
  classified. A YouTube id is eleven characters of `[A-Za-z0-9_-]`, so it can
  legitimately contain `403`, `429` or `timeout`; matching against the id
  would have read a dead video as a retryable one and looped on it forever,
  which is the bug this release exists to end. Confirmed against real ids
  from a live run and against ids crafted to collide.
- New option `recheck_unavailable_days` (0-365, default 30).
- Databases from 0.1.x are migrated in place; `CREATE TABLE IF NOT EXISTS`
  would have left the new columns off an existing install, so they are added
  explicitly on open. Covered by a test that opens a hand-built 0.1.x
  database.

## 0.1.1

- Fix options silently reverting to their defaults. The reported symptoms —
  `use_aria2c` back off after a restart, playlists going missing, settings
  "sometimes" not sticking — were all one bug, and none of them were the
  stored configuration changing. Home Assistant still had exactly what you
  saved; the add-on just failed to read it and quietly used its own defaults
  instead.
- The startup script read each option with a separate `bashio::config` call.
  Those resolve through the Supervisor API and its cache in `/tmp`, and every
  failure on that path is silent: an empty or truncated read makes `jq` emit
  nothing *and exit 0*, that becomes an empty string, and the Python side
  treats an empty string as "not set" and substitutes its built-in default.
  Nothing logged a warning, so a run on substituted defaults looked exactly
  like a healthy one.
- What that actually did, beyond the two symptoms reported: `dry_run` flips
  back to **true**, so a cycle silently downloads nothing, and `music_dir`
  reverts to the default `/media/nas1/Music`, which is not the same path as
  `/media/NAS1/Music` on a case-sensitive mount.
- Options are now read once, from `/data/options.json` — a plain local file
  that Supervisor writes, with no API call and no cache to go wrong — with the
  Supervisor API kept only as a fallback. The result is checked before
  anything uses it, and if it cannot be read the add-on **refuses to start**
  and says why. Starting with a configuration nobody chose is worse than not
  starting.
- Booleans are no longer read with jq's `//` operator. In jq `false // x`
  yields `x`, so a stored `false` came back as the fallback — any boolean
  defaulting to true could not be turned off through that path.
- The log now states the configuration in force at every start: playlist
  count, `music_dir`, `dry_run`, `use_aria2c`, format and interval, then each
  playlist URL. The whole bug class was invisible because nothing ever said
  what the add-on thought its settings were.

## 0.1.0

- Initial release.
- Watches any number of public YouTube playlists and mirrors each entry's
  audio into `music_dir` (a Home Assistant network storage mount under
  `/media` or `/share`).
- Duplicate detection combining title matching and duration matching:
  normalized titles from both filenames and tags, YouTube packaging noise
  stripped, version markers (`Live`, `Remix`, `Acoustic`, ...) preserved and
  used to veto false duplicate verdicts, and a YouTube id stamped into every
  download's tags so it stays recognisable without the database.
- Entries are classified as new, duplicate, different version, or ambiguous;
  ambiguous ones are re-checked after download against their real duration and
  quarantined if they turn out to be duplicates.
- Library index cached against file size and mtime, so rescans over SMB only
  re-read what changed.
- Keeps YouTube's original audio stream by default (no re-encode), embeds
  metadata and square-cropped artwork, and removes non-music intro/outro
  segments via SponsorBlock.
- Publishes six MQTT-discovery entities (downloads, duplicates skipped,
  library size, last run, status, problem). MQTT is optional.
- `dry_run` defaults to true, so the first cycle reports its decisions without
  writing to the library.
- yt-dlp updates itself on start by default, since YouTube breaks extraction
  every few weeks.
