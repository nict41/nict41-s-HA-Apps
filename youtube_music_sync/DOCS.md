# YouTube Music Sync - configuration

![YouTube Music Sync logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/youtube_music_sync/logo.png)

Mirrors public YouTube playlists into a music folder, skipping tracks the
library already has. See [README.md](README.md) for how duplicate detection
works and why the defaults are what they are.

## Setup

### 1. Point `music_dir` at the right folder

The add-on can only write below `/media` or `/share`, which is where Home
Assistant surfaces network storage:

- A mount added under **Settings > System > Storage** with usage **Media**
  appears as `/media/<mount name>`.
- One added with usage **Share** appears as `/share/<mount name>`.

So a NAS share called `nas1` holding a `Music` folder is
`/media/nas1/Music`. If a path is wrong, the add-on says so in the log and
downloads nothing rather than writing into the container (where files would be
lost on the next update).

### 2. Add playlist URLs

```yaml
playlists:
  - https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx
  - https://www.youtube.com/playlist?list=PLyyyyyyyyyyyyyyyy
```

Public or unlisted playlists. The same track on two of them is downloaded once.

### 3. Run the first cycle as a dry run

`dry_run` is **true** out of the box. Start the add-on, watch the log, and
check the decisions:

```
Library indexed: 2431 audio files (2431 read fresh, 0 stale entries dropped) in 48.2s.
Playlist 'Driving': 180 available entries.
Already in library: 'Some Band - Song Name (Official Video)' (title 100% match and duration within 2s) -> /media/nas1/Music/Some Band - Song Name.mp3
Downloading 'Some Band - Song Name (Live)' despite a near match (title matches but version markers differ (live)) with /media/nas1/Music/Some Band - Song Name.mp3
DRY RUN - nothing was downloaded. 12 track(s) would be fetched, 167 already in the library, 1 filtered by duration.
```

If tracks you own are shown as "would download", loosen matching
(`title_similarity` down, `duration_tolerance_seconds` up). If tracks you don't
own are being treated as owned, tighten it. Then set `dry_run: false`.

The first scan is the slow one; afterwards only changed files are re-read.

## Options

### Core

| Option | Default | Description |
| --- | --- | --- |
| `playlists` | `[]` | Public playlist URLs to monitor. |
| `music_dir` | `/media/nas1/Music` | Where tracks are written. Must be under `/media` or `/share`. |
| `poll_interval_minutes` | `60` | How often to re-check every playlist (5-1440). |
| `dry_run` | `true` | Report what would happen, write nothing. |

### Audio

| Option | Default | Description |
| --- | --- | --- |
| `audio_format` | `best` | `best` keeps YouTube's own stream (Opus/AAC) with no re-encode. `m4a`, `opus`, `mp3`, `flac`, `vorbis` force a container or codec; `mp3` means a lossy-to-lossy transcode. |
| `output_template` | `%(title)s.%(ext)s` | yt-dlp output template. Match whatever naming the rest of the library uses. |
| `sponsorblock_remove_offtopic` | `true` | Trims non-music intro/outro segments from music videos using SponsorBlock's `music_offtopic` category. |
| `embed_thumbnail` | `true` | Embed the video thumbnail as cover art. |
| `crop_thumbnail_square` | `true` | Centre-crop that art to a square first (YouTube art is 16:9). |

### Duplicate detection

| Option | Default | Description |
| --- | --- | --- |
| `duration_tolerance_seconds` | `5` | How far two durations may differ and still count as the same recording (0-120). |
| `title_similarity` | `0.87` | Title match required before duration is even considered (0.5-1.0). `1.0` demands an exact normalized match. |
| `on_duplicate` | `quarantine` | What to do with a track only revealed as a duplicate *after* downloading: `quarantine` moves it to `.yt_duplicates/` inside `music_dir`, `delete` removes it, `keep` leaves it in place. |

Pre-download duplicates are always skipped without downloading, whatever this
is set to.

### Filtering and pacing

| Option | Default | Description |
| --- | --- | --- |
| `min_duration_seconds` | `60` | Skip anything shorter - shorts, stings, trailers. `0` disables. |
| `max_duration_seconds` | `0` | Skip anything longer - hour-long mixes, full albums. `0` disables. |
| `max_downloads_per_cycle` | `20` | Cap per cycle; the rest waits for the next one. |
| `sleep_between_downloads` | `5` | Seconds between downloads. Raise it if YouTube starts challenging requests. |
| `rate_limit` | `""` | Bandwidth cap, e.g. `1M`. Empty is unlimited. |

### Reliability

| Option | Default | Description |
| --- | --- | --- |
| `cookies_file` | `""` | Path to an exported `cookies.txt`. A bare filename is read from `/addon_configs/<slug>/`. Needed if YouTube starts asking to confirm you're not a bot. |
| `use_aria2c` | `false` | Use aria2c as the downloader - multi-connection, more robust on flaky fragments. |
| `auto_update_ytdlp` | `true` | Update yt-dlp on every start. Leave on: YouTube changes extraction every few weeks. |
| `extra_args` | `""` | Extra yt-dlp flags, appended last so they override anything above. |
| `log_level` | `info` | `debug` logs every yt-dlp command line. |

## Entities

Published via MQTT discovery under a device named **YouTube Music Sync**:

| Entity | Shows |
| --- | --- |
| `sensor.youtube_music_sync_tracks_downloaded` | Running total; attributes carry the last 10 titles, skip breakdown and last-cycle counts |
| `sensor.youtube_music_sync_duplicates_skipped` | Running total of skipped entries |
| `sensor.youtube_music_sync_library_tracks` | Files found in the library at the last scan |
| `sensor.youtube_music_sync_last_run` | Timestamp of the last completed cycle |
| `sensor.youtube_music_sync_status` | `idle`, `scanning`, `comparing`, `downloading`, `error`, `misconfigured` |
| `binary_sensor.youtube_music_sync_problem` | On when the last cycle had failures or a playlist couldn't be read |

A notification automation, for example:

```yaml
automation:
  - alias: "Notify on new music"
    trigger:
      - platform: state
        entity_id: sensor.youtube_music_sync_tracks_downloaded
    condition:
      - "{{ trigger.to_state.state | int > trigger.from_state.state | int }}"
    action:
      - service: notify.mobile_app
        data:
          message: >-
            {{ state_attr('sensor.youtube_music_sync_tracks_downloaded',
               'recent_downloads') | first }} saved to the library.
```

## Troubleshooting

**"music_dir ... is not there."**
The mount isn't present. Check **Settings > System > Storage**; the add-on sees
media mounts as `/media/<name>` and share mounts as `/share/<name>`. Nothing is
downloaded until the path exists - deliberately, so a dropped NAS doesn't cause
a re-download of the whole playlist into the container.

**"No audio files found under ..."**
The scan found nothing, so every entry will look new. Almost always the wrong
`music_dir`.

**"Sign in to confirm you're not a bot"**
Export a `cookies.txt` from a browser logged into YouTube, drop it in
`/addon_configs/youtube_music_sync/`, and set `cookies_file: cookies.txt`.
Raising `sleep_between_downloads` and setting a `rate_limit` helps too.

**Tracks downloaded that are already owned**
Check the log line for the verdict. Common causes: the library file has no
readable tags *and* a filename that doesn't resemble the YouTube title, or
SponsorBlock trimmed enough to push the duration outside tolerance. Raise
`duration_tolerance_seconds`, or lower `title_similarity` a little.

**Tracks skipped that you actually wanted**
Raise `title_similarity` toward `1.0` so only near-exact titles count as owned.
Everything skipped is logged with the file it matched against.

**"a file of that name already exists in the library"**
`--no-overwrites` stopped a download from clobbering an existing file of the
same name, but the matcher hadn't called it a duplicate. Usually a genuinely
different track that normalizes to the same filename under
`output_template` - give the template more fields, e.g.
`%(title)s [%(id)s].%(ext)s`.

**Settings look like they did not stick**
Every start now logs the configuration in force, as an `Options:` line from
the startup script and a `Library: ... | dry_run: ... | aria2c: ...` line from
the app, followed by each playlist URL. Compare those against the
Configuration tab. If they disagree, the options were not read - which the
add-on now refuses to start on, rather than running with its own defaults.
Before 0.1.1 that substitution was silent, and showed up as `use_aria2c`
turning itself off, playlists going missing, or a cycle quietly doing nothing
because `dry_run` had reverted to true.

**Starting over**
Delete the add-on's `/data/state.db` (uninstall/reinstall) to clear the
download history and library cache. Files already in the library are untouched
and will simply be re-recognised on the next scan.
