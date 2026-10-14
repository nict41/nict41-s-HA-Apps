# Home Assistant App: YouTube Music Sync

![YouTube Music Sync logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/youtube_music_sync/logo.png)

Watches one or more **public YouTube playlists** and mirrors each video's audio
into a music folder, so the playlist always has a saved local copy. Tracks the
library already contains are recognised and skipped using a combination of
**title matching and duration matching**, so a library built up over years
doesn't gain a second copy of everything.

## Why

Pulling tracks down by hand (with Seal on Android, say) works but doesn't
scale: every new addition to a playlist is a manual round trip, and nothing
stops the same song being saved twice under a slightly different name. This
add-on does the same job on a schedule, writing straight into the music folder
on the NAS, and refuses to re-add what's already there.

## What it does

Every cycle (hourly by default):

1. **Indexes the music library** under `music_dir` - filenames, tags, and
   durations. The index is cached against each file's size and mtime, so a
   rescan over SMB only re-reads files that actually changed.
2. **Lists each playlist** with a single cheap `--flat-playlist` request,
   which returns per-entry durations without downloading anything.
3. **Decides, per entry**, whether the library already has it (see below).
4. **Downloads what's missing** with yt-dlp, rate-limited, keeping the original
   audio stream rather than re-encoding it.
5. **Re-checks each download** against the library now that its real duration
   is known, and quarantines anything that turns out to be a duplicate after
   all.
6. **Publishes MQTT sensors** so the whole thing is visible in Home Assistant.

## Duplicate detection

The expensive mistake is a second copy of a track under a different name; the
cheap mistake is skipping something and fetching it by hand. The matcher is
tuned accordingly - it needs **both** a title match and a duration match before
it calls something a duplicate.

**Titles** are normalized first. `Some Band - Song Name (Official Music Video)
[HD]`, `01 - Some Band - Song Name.mp3`, and a file tagged
`artist=Some Band, title=Song Name` all collapse onto the same key. Packaging
noise (`Official Video`, `Lyrics`, `[4K]`, `| Audio`, leading track numbers,
`- Topic`, accents, `&` vs `and`) is stripped. Both the filename **and** the
tags produce comparison keys, and the best score across all of them wins -
that's what makes it work on a library where some files are named
`Artist - Title` and others are just `Title` with the artist only in the tags.

**Version markers are deliberately kept.** `(Live)`, `(Remix)`, `(Acoustic)`,
`(Extended Mix)` and friends survive normalization, and any asymmetry in them
vetoes a duplicate verdict outright. Silently treating a remix as a duplicate
of the studio cut is the one failure mode that loses music without telling
anyone.

Each playlist entry ends up in one of four states:

| Verdict | Meaning | Action |
| --- | --- | --- |
| **New** | Nothing similar in the library | Download |
| **Duplicate** | Title and duration both match | Skip, without downloading |
| **Different version** | Title matches, duration or version markers don't | Download, and log the near match |
| **Ambiguous** | Title matches, duration unknown on one side | Download, then re-check with the real duration |

Three further safeguards:

- Every download is **stamped with its YouTube id** in the file's tags, so it
  stays recognisable even if the state database is lost or the file is renamed.
- A track appearing on **two monitored playlists** is downloaded once: the live
  index is updated as the cycle runs.
- `--no-overwrites` means an existing library file is **never clobbered** by a
  download that happens to share its name.

## What it produces

- **Files:** audio written into `music_dir`, with metadata and square-cropped
  artwork embedded.
- **Entities**, grouped under an MQTT device named **YouTube Music Sync**:
  - `sensor.youtube_music_sync_tracks_downloaded`
  - `sensor.youtube_music_sync_duplicates_skipped`
  - `sensor.youtube_music_sync_library_tracks`
  - `sensor.youtube_music_sync_last_run`
  - `sensor.youtube_music_sync_status`
  - `binary_sensor.youtube_music_sync_problem`
- Attributes on the first and last of those carry the recent download list, a
  breakdown of why things were skipped, and last-cycle counts.

MQTT is optional - without a broker the add-on still mirrors playlists, it just
has no sensors.

## First run is a dry run

`dry_run` defaults to **true**. The first cycle reports what it *would*
download and what it considers already owned, without writing anything. Read
the log, confirm the matching looks sane against your library, then set
`dry_run` to false.

This default exists because downloads land directly in a curated music folder.
It's much easier to adjust `title_similarity` or `duration_tolerance_seconds`
before 200 files appear than after.

## Notable options

| Option | Default | Notes |
| --- | --- | --- |
| `playlists` | `[]` | One or more public playlist URLs |
| `music_dir` | `/media/nas1/Music` | Must be under `/media` or `/share` |
| `dry_run` | `true` | Report only; write nothing |
| `audio_format` | `best` | `best` remuxes the original stream - no re-encode |
| `duration_tolerance_seconds` | `5` | How far durations may differ and still match |
| `title_similarity` | `0.87` | Raise toward `1.0` for stricter title matching |
| `on_duplicate` | `quarantine` | For duplicates only caught after downloading |
| `sponsorblock_remove_offtopic` | `true` | Trims non-music intro/outro segments |
| `min_duration_seconds` | `60` | Keeps shorts and stings out of the library |

Full reference in [DOCS.md](DOCS.md).

## Design notes

- **`audio_format: best` does not re-encode.** YouTube serves Opus or AAC;
  "best" remuxes that stream into a suitable container. Choosing `mp3` instead
  means a lossy-to-lossy transcode - fine for compatibility, a real quality
  cost otherwise.
- **yt-dlp updates itself at startup** (`auto_update_ytdlp`). YouTube changes
  extraction every few weeks, so a version frozen at image-build time starts
  failing every download. The pinned version in `requirements.txt` is the
  floor, not the ceiling.
- **Downloads are staged on `/data`, not the NAS.** Partial files and the
  thumbnail/remux shuffle over SMB are slow and leave debris on failure. Only
  the finished file is moved into the library.
- **Quarantine, not delete, by default.** The matcher is good, not infallible.
  A wrongly-judged track sitting in `.yt_duplicates/` can be moved back; a
  deleted one has to be fetched again.
- **Thumbnails are cropped square** before embedding. YouTube art is 16:9,
  which most music players letterbox or crop badly.

## Known limitations

- **Bot checks.** YouTube challenges some IPs with "Sign in to confirm you're
  not a bot". If that appears in the log, point `cookies_file` at an exported
  `cookies.txt` and consider a slower `sleep_between_downloads`.
- **Nothing is ever deleted** when a video leaves a playlist or goes private.
  The local copy stays.
- **Duration tolerance cuts both ways.** SponsorBlock trimming shifts
  durations; too tight a tolerance re-downloads tracks, too loose a one may
  skip a genuinely different edit. `5` seconds suits most music.
- **Private playlists are out of scope.** Public (or unlisted) URLs only.

## Legal

Downloading content from YouTube is contrary to their Terms of Service, and
copying commercial music is copyright infringement in most jurisdictions
regardless of whether it sits in your playlist. Appropriate for your own
uploads, Creative Commons material, or anything you're otherwise licensed for.
Use is at your own discretion and risk.
