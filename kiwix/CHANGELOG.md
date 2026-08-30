# Changelog

## 0.2.0

- **Faster downloads.** An archive can now be fetched over several
  connections at once, each pulling its own byte range into the same file.
  Set **Connections per archive** in the new Settings panel (1-8, 4 is a
  good default). A mirror that refuses range requests falls back to a single
  connection on its own.
- **More than one archive at a time.** **Archives at once** now goes up to
  6, and changing it takes effect immediately instead of at the next
  restart.
- **Off-peak scheduling.** Downloads can be confined to a time window, e.g.
  23:00-07:00, so a 100 GB transfer doesn't compete with everything else
  during the day. Running transfers stand down when the window closes and
  resume by themselves when it opens; **Download now** overrides it for one
  archive.
- **Settings in the app.** A Settings panel (the gear icon) holds the
  download knobs, the catalog language and the off-peak window, and applies
  changes live. The add-on options are still there and now act as its
  defaults - saving one of those restarts the add-on, which is exactly what
  you don't want while a 100 GB download is running.
- **A way back from the reader.** Reading now happens under a bar with a
  **Library** button, so you can return however deep into an archive you
  have clicked, instead of being stuck without browser chrome inside Home
  Assistant's panel.
- **Better browsing.** Catalog results can be sorted by size, article count,
  publication date or title, and **Details** on any result shows its full
  description, article and media counts, publisher, contents (pictures,
  videos, full-text search), exact filename and a preview link.
- **Downloads survive a restart.** The download list is now saved, so
  updating the add-on or rebooting Home Assistant no longer loses track of
  what was in flight - transfers pick themselves back up automatically,
  from where they stopped. A `.part` file left by an earlier version is
  adopted too: resuming it looks its source up in the catalog by filename,
  so nothing already downloaded is re-fetched.

## 0.1.0

- First release. Packages `kiwix-serve` (kiwix-tools 3.8.2) as a Home
  Assistant app with its own sidebar panel, so offline Wikipedia and other
  ZIM archives can be browsed, downloaded and read without any Docker or
  command-line work.
- Archives are stored on a network share mapped into Home Assistant as Media
  storage, set with the **ZIM storage path** option. Nothing is stored on
  Home Assistant's own disk except a few kilobytes recording which archives
  are being served. A share that is unmounted, read-only or not yet
  configured is reported as a clear message in the panel - the app keeps
  running, keeps its serving selection, and recovers on its own when the
  share comes back.
- **Browse & download** opens on the Wikipedia editions for your language
  with their flavours (full / no pictures / mini) side by side, and searches
  the whole Kiwix catalog for everything else. Downloads run onto the share
  with live progress, transfer rate and time remaining, are resumable after
  an interruption, and are refused up front if they wouldn't fit in the free
  space on the share.
- The **Library** tab lists what is on the share with its size, and controls
  which archives `kiwix-serve` is serving; changes take effect immediately
  without a restart.
- Reading happens in the same sidebar panel: `kiwix-serve` runs on loopback
  only and is reverse-proxied under the panel's own path, so articles are
  behind Home Assistant's authentication with no extra port to expose.
