# Changelog

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
