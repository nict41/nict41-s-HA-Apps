# Changelog

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
