# Changelog

## 0.2.5

- Set an album artist on every download. YouTube gives none, so Music
  Assistant filed every album under "Various Artists" and artist pages showed
  no albums. After each download the app looks at every library track with
  the same album tag: identical artists become the album artist, an artist
  credited on every track wins over guests, and anything else is "Various
  Artists". Same-titled singles from different years (two unrelated "Your
  Love"s) are kept apart. Sibling tracks are rewritten too, since a new
  track can turn a single-artist album into a compilation.

## 0.2.4

- Write one artist tag per credited artist. yt-dlp's `--embed-metadata`
  joins collaborators into a single "Calibre, High Contrast" string, which
  Music Assistant (rightly - real names contain commas) files as one made-up
  artist with no image. The app now asks yt-dlp for the artist list itself
  and, after each download, rewrites that joined tag as a multi-value one
  (Vorbis/Opus, MP3 and M4A). A tag that is not exactly yt-dlp's joined form
  is left alone.

## 0.2.3

- Fix a stale partial download permanently failing with HTTP 416. With
  `use_aria2c` off, yt-dlp's native downloader tried to resume from a
  `.part`/`.aria2` file an earlier interrupted attempt (aria2c crashing, the
  container restarting mid-download) left behind, and the server rejected
  the stale byte range every single time - one track failed this way for 49
  consecutive cycles across ~30 restarts. `--no-continue` is now always
  passed (these are small music files; resuming saved little), leftover temp
  files are cleared at startup, and a download that still hits HTTP 416
  clears the temp dir and retries once before giving up.
- Stop hammering YouTube once it starts asking for bot verification. A
  "Sign in to confirm you're not a bot" response used to be retried for
  every remaining item in the cycle - one every couple of seconds, which
  only makes the block worse. The first hit now aborts the rest of the
  cycle immediately (the remaining items are left untouched, not counted as
  failed or marked unavailable), logs one clear warning, and pauses future
  cycles for 6 hours, doubling on each consecutive hit up to a 24-hour cap.
  The pause survives a restart.
- Ordinary failures (not just ones classified "unavailable") now back off
  too, on their own shorter schedule - an hour, six hours, a day, then the
  same `recheck_unavailable_days` cap. Previously only "unavailable" backed
  off at all; everything else, including the HTTP 416 above, retried every
  cycle forever. The cycle summary's "skipped as unavailable" count is now
  "deferred (backing off)", since it covers both kinds.
- Fix the near-match count and log line. "Downloading X despite a near
  match..." was logged, and counted into "near matches kept", during
  planning - before the download was even attempted - so a near match that
  then failed to download (including one sitting in its own backoff window)
  still inflated the count and never got corrected. The decision to attempt
  it is now logged at DEBUG during planning; the count and an INFO line
  ("Saved ... kept despite near match with ...") only happen once the file
  is actually saved.
- "Already in library" - one line per already-owned track skipped, roughly
  2,500 lines a day on this library - moved from INFO to DEBUG. The
  "already owned" count in the cycle summary is enough at INFO.
- Duplicate playlist URLs are now recognised and skipped, even when they
  only differ by YouTube's `si=` share-tracking parameter or use a
  different host (`music.youtube.com`/`youtube.com`/`www.youtube.com` all
  serve the same playlist for the same `list=` id). Previously each counted
  as a separate playlist and was read in full every cycle.
- Switched from the deprecated `bashio::addon.config` to `bashio::app.config`,
  clearing a "is deprecated" warning on every start.
- Added `--root-user-action=ignore --disable-pip-version-check` to every
  `pip3 install` call, clearing pip's "Running pip as the 'root' user..."
  warning on every start (and during the image build).

## 0.2.2

- Fix "Failed to save: Missing option 'download_timeout_minutes' in root".
  The options added in 0.2.0 and 0.2.1 were declared required, and updating
  an add-on does not backfill new defaults into an existing install's saved
  options - so on any install that predates them the values were simply
  absent, and Home Assistant refused *every* save, including the one that
  would have supplied them. Changing any unrelated setting, such as turning
  `use_aria2c` off, failed with this.
- Every scalar option is optional now, not only the new ones, so adding
  another in future cannot do this again. Nothing is lost by it: both the
  startup script and `settings.py` already substitute a default for an
  absent option, and the checks that matter - a playlist being configured, a
  `music_dir` under /media or /share - are made at runtime where a real error
  message can be given.
- A test asserts it, reading `config.yaml` directly: every option has a
  schema entry and vice versa, and no scalar rule is required. It names any
  offender, so the next person to add an option gets told rather than a user
  getting an unsaveable configuration.
- If you are stuck on 0.2.1 and cannot save: Configuration tab, the ⋮ menu,
  *Edit in YAML*, and add `recheck_unavailable_days: 30`,
  `download_timeout_minutes: 120` and `progress_interval_seconds: 30`.

## 0.2.1

- Log progress while a track downloads. yt-dlp was run with `--no-progress`
  and its output captured in one go, so the log said nothing at all between
  "Downloading 7/60" and the result. Fine for a three-minute track, useless
  for a three-hour one. Output is now read as it arrives and a progress line
  is logged every `progress_interval_seconds` (default 30, `0` turns it off):
  percentage, size, speed and ETA.
- Fix a long track being retried for ever instead of finishing. The download
  timeout was a hardcoded 30 minutes covering the *whole* job - fetch,
  SponsorBlock trim, audio extract, thumbnail embed - and a long mix spends
  most of that in ffmpeg rather than on the wire. Going over raised
  `TimeoutExpired`, whose message reads "timed out after 1800 seconds", which
  classifies as a transient failure, so the track was retried from scratch on
  the next cycle, timed out again, and never completed. The cap is now
  `download_timeout_minutes` (default 120).
- The timeout is enforced from a separate thread rather than while reading
  output, so it still applies during a long post-process, when yt-dlp prints
  nothing for minutes at a time.
- Progress lines are tagged, so they are never mistaken for the last line of
  output that becomes the error message - a failure after thousands of
  progress frames still reports what actually went wrong.
- New options `download_timeout_minutes` (5-1440, default 120) and
  `progress_interval_seconds` (0-600, default 30).

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
