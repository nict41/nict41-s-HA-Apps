# Changelog

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
