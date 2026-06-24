# Parcel Tracker

![Parcel Tracker logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/parcel_tracker/logo.png)

Automatically detects tracking numbers from your shipping emails and shows
delivery status for every parcel in one ingress dashboard - no manual
copy-pasting of tracking numbers required.

## How it works

1. The app periodically connects to a mailbox over IMAP and scans recent
   emails for tracking numbers. The connection is **read-only** - nothing
   is ever marked as read, moved, or deleted.
2. Emails from known retailers (AliExpress, eBay, Amazon, Cainiao) are
   parsed with a high-confidence label-based detector, since their
   notification emails reliably print "Tracking Number: ..." style text.
   Everything else falls back to generic carrier-pattern matching (UPS,
   USPS, FedEx, DHL, Royal Mail, DPD, Evri, YunExpress, and international
   postal tracking numbers used by China Post/ePacket, Hongkong Post,
   Singapore Post, and others), gated behind shipping-related context to
   keep false positives down.
3. High-confidence matches are tracked automatically. Lower-confidence
   matches land in a **needs confirmation** queue on the dashboard, so
   nothing gets auto-tracked on a guess.
4. If a [17track](https://www.17track.net/) and/or
   [Track123](https://www.track123.com/) API key is configured, the app
   registers each tracking number for auto-detected carrier lookup (useful
   for AliExpress/eBay shipments, which are often routed through one of
   dozens of regional carriers) and pulls live status on every check -
   including for parcels still awaiting confirmation, so the detected
   carrier and a status preview show up before you've even confirmed it's a
   real parcel. Each parcel sticks to whichever provider it was first
   registered with, even if you add or remove the other provider's key
   later. Without any provider configured, parcels are still detected and
   listed, just with a carrier tracking link instead of live status.
5. Delivered parcels are kept on the dashboard for a configurable number
   of days, then automatically archived.

## Setup

### 1. Connect one or more mailboxes

The app can poll multiple email accounts, so shipping notifications landing
in different inboxes (e.g. a personal account and a shared family account)
all show up on the same dashboard. For each account you want monitored, you
have two options:

**Option A - connect the real mailbox directly.** Give the app IMAP
credentials for the mailbox that receives your shipping emails. For Gmail,
enable IMAP access (**Settings → Forwarding and POP/IMAP**) and create an
[app password](https://myaccount.google.com/apppasswords) rather than
using your normal account password. Other providers (Outlook, iCloud,
Fastmail, etc.) have similar app-password mechanisms for third-party IMAP
clients.

**Option B - forward to a dedicated mailbox.** If you'd rather not give
the app credentials to your primary mailbox, set up mail forwarding (or a
filter that copies matching mail) from your primary account to a separate
mailbox created just for this, then point one of the app's mailbox entries
at that dedicated mailbox instead. This is purely a setup choice on your
mail provider's side - the app's IMAP polling works identically either way.

You can freely mix both options across entries - one account connected
directly, another via a forwarding mailbox, and so on.

### 2. Configure the app

Set the options below (**Settings → Add-ons → Parcel Tracker →
Configuration**), then start the app.

Under **Mailboxes**, add one entry per email account to scan:

| Field | Default | Description |
|---|---|---|
| `host` | _(required)_ | IMAP server hostname, e.g. `imap.gmail.com`. |
| `port` | `993` | IMAP port. |
| `use_ssl` | `true` | Use IMAP over SSL. Turn off for STARTTLS. |
| `username` | _(required)_ | Mailbox login, usually the full email address. |
| `password` | _(required)_ | Mailbox password or app-specific password. |
| `folder` | `INBOX` | Folder to scan. |

The remaining options apply across all configured mailboxes:

| Option | Default | Description |
|---|---|---|
| `lookback_days` | `14` | How far back to scan emails on each check. |
| `poll_interval_minutes` | `30` | How often to check mail and refresh status. |
| `auto_archive_after_days` | `14` | Auto-archive parcels this many days after delivery. `0` disables. |
| `trusted_senders` | _(blank)_ | Comma-separated extra sender domains to treat as high-confidence retailers. |
| `ignore_senders` | _(blank)_ | Comma-separated sender domains to skip entirely. |
| `seventeentrack_api_key` | _(blank)_ | Optional [17track](https://www.17track.net/en/api) API key for live status. |
| `track123_api_key` | _(blank)_ | Optional [Track123](https://www.track123.com/) API key for live status. |

### 3. (Optional) Get a tracking provider API key

Without any provider key, the dashboard still detects and lists every
parcel, with a tracking link per carrier (or 17track's own universal
tracker for carriers without a known link). With a key configured for
either provider below, the dashboard instead shows live status (in
transit, out for delivery, delivered, exception) pulled directly into
each parcel's card, including a carrier/status preview on cards still
awaiting confirmation.

You can configure one provider or both:

- **[17track](https://www.17track.net/en/api)** - new accounts get a
  one-time trial allocation of 200 tracking numbers that does **not**
  renew monthly; beyond that it's a paid prepaid quota. Good for a
  starting batch, but not a sustainable free option on its own.
- **[Track123](https://www.track123.com/)** - the free tier covers 50
  tracking numbers and renews every month, making it the better default
  for ongoing use.

If both are configured, new parcels register with Track123 first (since
its free quota renews) and fall back to 17track only once Track123 is
unconfigured or unavailable. Once a parcel is registered with a provider,
it keeps using that same provider on every later refresh rather than
switching (and consuming quota on both).

## Using the dashboard

Open the app's ingress panel from your sidebar:

- **Needs confirmation** - lower-confidence detections. Each card shows a
  short preview of the source email, with a "View full email" section to
  expand the sender, subject, and full body, plus a live carrier/status
  preview if a tracking provider key is configured - useful for judging
  whether it's a real tracking number before you decide. Confirm to start
  tracking, or dismiss if it isn't actually a parcel.
- **In transit** - actively tracked parcels with their latest status.
- **Delivered** - parcels marked delivered, until they're auto-archived.
- **Archived** - dismissed or archived parcels, with an option to delete.
- An **add parcel** form lets you track a number manually, e.g. for a
  shipment that didn't arrive by email at all.

"**Check mail now**" runs a sync immediately instead of waiting for the
next scheduled check.

## Storage

All parcel and sync state is stored locally in `/data/parcels.db`
(SQLite). The mailbox itself is never modified.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **Parcel Tracker**
from the app store.
