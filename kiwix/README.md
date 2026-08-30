# Kiwix

![Kiwix logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/kiwix/logo.png)

Browse, download and serve offline Wikipedia (and other ZIM) archives, stored
on a network share rather than on Home Assistant's own disk.

The app packages [kiwix-serve](https://wiki.kiwix.org/wiki/Kiwix-serve) and
adds a manager UI on top of it, all inside a single sidebar panel: pick an
archive from the Kiwix catalog, watch it download onto your NAS, choose which
archives are served, and read them — without leaving Home Assistant, and
without a single Docker or CLI command.

## ⚠️ ZIM files are enormous

A ZIM archive is an entire wiki in one file. Check the size before you press
**Download** — these are the current English Wikipedia editions:

| Edition | Flavour | Size |
|---|---|---|
| Complete Wikipedia (19M articles) | Full, with images | **124 GB** |
| Complete Wikipedia | No pictures | **53 GB** |
| Complete Wikipedia | Mini (intros only) | **12 GB** |
| Top 1M articles | Full, with images | **49 GB** |
| Top 1M articles | No pictures | **17 GB** |
| Best 45,000 articles (WP1 0.8) | Full, with images | **8.5 GB** |

If space on the share is at all tight, take the **no-pic** or **mini**
flavour, or one of the smaller selections — the text of every article is
still there. The app refuses to start a download that doesn't fit in the free
space it can see on the share, but it can't stop you filling the share with
several archives over time.

Downloads are resumable and restartable: an interrupted transfer keeps its
`.part` file and picks up where it left off, including after the add-on is
updated or Home Assistant reboots. A 124 GB download surviving a restart is
normal rather than a disaster - nothing needs re-fetching but the last few
seconds.

## Setup

### 1. Map your NAS as network storage

In Home Assistant, go to **Settings → System → Storage → Add network
storage** and add the share the archives should live on, with **Usage** set
to **Media**. Home Assistant mounts it at `/media/<share name>` and this app
is given access to it (`media:rw`).

Nothing is stored on the Home Assistant machine itself except a few kilobytes
of bookkeeping — the archives live only on the share, and are read from it
directly while they are served.

### 2. Point the app at a folder on the share

Set the **ZIM storage path** option to the folder you want archives kept in,
either relative to `/media`:

```
NAS1/Kiwix
```

or as a full path:

```
/media/NAS1/Kiwix
```

The folder is created for you if the share is mounted and the folder simply
doesn't exist yet. The option is deliberately empty by default — the app will
tell you it has nowhere to store archives rather than quietly filling Home
Assistant's own disk.

### 3. Open Kiwix from the sidebar

The app appears in the Home Assistant sidebar as **Kiwix**. Everything —
catalog, downloads and the reader itself — is served through Home Assistant's
ingress, so it is behind the same authentication as the rest of Home
Assistant and needs no extra port, port forward or URL.

### 4. Download an archive

Open **Browse & download**. It opens on the Wikipedia editions for your
configured language, with each edition's flavours side by side:

- **Full** — everything, images included. Largest by far.
- **No pictures** — full text, no images. Roughly a fifth of the full size.
- **Mini** — introductions and infoboxes only. Smallest.

Search the whole catalog from the same tab for anything else the Kiwix
library carries (Wiktionary, Stack Exchange, Project Gutenberg, TED,
WikiHow, the DevDocs programming references, and so on), and sort the
results by size, article count, publication date or title. **Details** on
any result opens its full description, article and media counts, publisher,
what it contains (pictures, videos, full-text search) and its exact
filename, plus a link to preview it on library.kiwix.org.

Press **Download** and the transfer starts onto the share, with live
progress, transfer rate and estimated time on the **Library** tab.

### 5. Read it

Finished downloads are added to what kiwix-serve serves (unless you turn
**Serve newly downloaded archives** off), and the **Read** button in the
header opens the reader in the same panel, under a bar with a **Library**
button that brings you back however deep into an archive you have clicked. Use **Serve** / **Stop serving**
on the Library tab to control which archives are searchable and readable;
changes take effect immediately, without restarting anything.

## Options

| Option | Default | Description |
|---|---|---|
| `zim_path` | _(blank)_ | Folder the archives are stored in, relative to `/media` or as a full path. Required. |
| `library_source` | `https://library.kiwix.org` | Base URL of the catalog to browse. Point it at your own mirror if you run one. |
| `catalog_language` | `eng` | ISO 639-3 language the catalog is filtered by when the browser opens. |
| `auto_serve_new` | `true` | Serve each archive automatically once it finishes downloading. |
| `max_concurrent_downloads` | `1` | How many downloads may run at once. |

## Download speed and scheduling

The gear icon in the header opens **Settings**. Everything there applies
immediately, including to transfers already running - unlike the add-on
options below it, saving which restarts the add-on and interrupts them.

| Setting | What it does |
|---|---|
| **Archives at once** | How many separate downloads run in parallel (1-6). |
| **Connections per archive** | Splits one archive across several byte ranges fetched at the same time (1-8). This is usually the one that makes a large download faster; 4 is a good default. |
| **Off-peak window** | Confines transfers to a time range, e.g. 23:00-07:00, so a 100 GB download isn't competing with everything else during the day. The window may cross midnight, and uses Home Assistant's timezone. |

When the window closes, running transfers stand down and show as *waiting
for the window*; they resume by themselves when it opens again, from exactly
where they stopped. **Download now** on any waiting transfer starts it
regardless, and that archive keeps ignoring the window until it finishes.

More connections is not always faster: mirrors rate-limit per connection, so
4-8 helps on a fast line, while a slow connection or a busy NAS may be
happier with 1-2. If a mirror refuses range requests, the download quietly
falls back to a single connection.

## How it works

`kiwix-serve` runs inside the app on loopback only and is never exposed as a
port. The manager reverse-proxies it under the panel's own `/kiwix` path, so
reading an article goes through Home Assistant's authentication exactly like
the rest of the panel.

Because kiwix-serve writes absolute links into the pages it serves, it is
started with Home Assistant's ingress path as its URL root
(`--urlRootLocation`), which is also why the reader only works from the
sidebar and not over the add-on's optional direct port. Which archives are
served is materialised as a kiwix library XML file that kiwix-serve watches
(`--monitorLibrary`), so serving and un-serving take effect without a
restart.

## Storage

- `/media/<zim_path>/*.zim` — the archives, on your share.
- `/media/<zim_path>/*.zim.part` — in-progress downloads, resumable.
- `/media/<zim_path>/*.zim.part.json` — which byte ranges of a split
  download have arrived. A `.part` without one is a plain sequential
  download, which is how single-connection transfers are stored.
- `/data/state.json`, `/data/library.xml`, `/data/downloads.json`,
  `/data/settings.json` — which archives are served, the download list and
  your in-app settings (a few kilobytes, on Home Assistant's own disk).

## Troubleshooting

**"Storage unavailable — the network share looks like it isn't mounted."**
The share is gone or was renamed. The app keeps running, keeps your serving
selection, and recovers on its own once the share is back — check
**Settings → System → Storage**, then reload the page. No restart needed.

**A download stopped when the share dropped.** Its `.part` file is kept.
Start the same archive again from **Browse & download** and it resumes from
where it stopped rather than starting over.

**"is not writable by the add-on".** The share is mounted read-only, or the
credentials it is mounted with don't have write permission on that folder.

**The reader is empty.** Nothing is selected to serve — use **Serve** on an
archive in the Library tab.

**A download says "waiting for the window".** It is outside the off-peak
hours set in Settings. It will start on its own when the window opens, or
press **Download now** to start it immediately.

**A `.part` file is much bigger than the progress shown.** A split download
writes to several places in the file at once, so its length runs ahead of
how much has actually arrived. The progress figure (and the `.part.json`
beside it) is the accurate one.

**A download says it was interrupted and can't find its archive.** It was
resumed from a `.part` file whose edition is no longer in the catalog - ZIM
editions are replaced every month or two. Delete it and download the current
edition; resuming into a different edition's file would corrupt it.

**The catalog won't load.** Browsing needs internet access; archives already
on the share keep working offline, which is rather the point.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **Kiwix** from the
app store.
