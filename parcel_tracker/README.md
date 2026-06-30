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
   including for parcels still in the needs-confirmation queue. When a
   provider positively recognises a queued number (it resolves a real
   carrier or returns an actual tracking event), that number is
   **auto-confirmed** and starts tracking with the provider's carrier,
   replacing our pattern guess - so a number an email mislabelled gets
   corrected automatically. A number the provider has *never once*
   recognised - our own pattern-matching guessed wrong, e.g. an order ID
   that happened to look like a tracking number - is automatically
   **dismissed** after `dismiss_unconfirmed_after_days`, since the
   provider's response is the authority on whether something is a real
   tracking number. Each parcel sticks to whichever provider it was first
   registered with, even if you add or remove the other provider's key
   later. Without any provider configured, parcels are still detected and
   listed (and never auto-dismissed), just with a link to Track123's web
   tracker instead of live status.
5. Delivered parcels are kept on the dashboard for a configurable number
   of days, then automatically archived.
6. Every parcel is also exposed as a Home Assistant sensor entity (no
   extra setup), so you can build automations or a Lovelace card around
   your packages without opening the app at all - see
   [Home Assistant entities](#home-assistant-entities) below.

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
| `folders` | `INBOX` | List of folders to scan - e.g. `INBOX` plus a "Shipping" label or filtered-into folder. Defaults to just `INBOX` when left blank. |

The remaining options apply across all configured mailboxes:

| Option | Default | Description |
|---|---|---|
| `lookback_days` | `14` | How far back to scan emails on each check. |
| `poll_interval_minutes` | `30` | How often to check mail and refresh status. Dashboard actions (confirm, archive, delete, etc.) update Home Assistant sensors immediately regardless of this interval. |
| `auto_archive_after_days` | `14` | Auto-archive parcels this many days after delivery. `0` disables. |
| `dismiss_unconfirmed_after_days` | `3` | Auto-dismiss a candidate this many days after a tracking provider first checks it without ever confirming it's a real number. `0` disables. Only applies when a tracking provider is configured. |
| `trusted_senders` | _(blank)_ | Comma-separated extra sender domains to treat as high-confidence retailers. |
| `ignore_senders` | _(blank)_ | Comma-separated sender domains to skip entirely. |
| `allowed_senders` | _(blank)_ | Comma-separated sender domains to scan exclusively - everything else is excluded before it's even fetched, speeding up mail checks. Leave blank to scan every sender. |
| `seventeentrack_api_key` | _(blank)_ | Optional [17track](https://www.17track.net/en/api) API key for live status. |
| `track123_api_key` | _(blank)_ | Optional [Track123](https://www.track123.com/) API key for live status. |

### 3. (Optional) Get a tracking provider API key

Without any provider key, the dashboard still detects and lists every
parcel, with a tracking link to Track123's own web tracker, which
auto-detects the carrier from the number regardless of whether its API is
configured. With a key configured for either provider below, the
dashboard instead shows live status (in transit, out for delivery,
delivered, exception) pulled directly into each parcel's card, including
a carrier/status preview on cards still awaiting confirmation.

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

- **Needs confirmation** - lower-confidence detections a tracking provider
  couldn't yet recognise (ones it can recognise are auto-confirmed straight
  into **In transit**). Each card shows a live carrier/status preview if a
  tracking provider key is configured - useful for judging whether it's a
  real tracking number before you decide. Confirm to start tracking, or
  dismiss if it isn't actually a parcel.
- **In transit** - actively tracked parcels with their latest status.
- **Delivered** - parcels marked delivered, until they're auto-archived.
- **Archived** - dismissed or archived parcels, with an option to delete.
  This includes numbers auto-dismissed because a tracking provider never
  confirmed them - if that happens to a real parcel, just re-add it by hand.
- An **add parcel** form lets you track a number manually, e.g. for a
  shipment that didn't arrive by email at all. Re-adding a tracking number
  that's already tracked asks for confirmation first, since it would
  otherwise silently overwrite that parcel's carrier and description.

Click any card to expand its full tracking history - every event a
tracking provider has recorded for that parcel, not just the latest one
shown on the card itself - along with a carrier tracking link, which
provider confirmed it, and when it was first detected vs. last checked.
Parcels with no recorded history yet (e.g. no tracking provider
configured) show a simple "no history available" message instead.

For any parcel that was detected from an email - in any section, not just
**Needs confirmation** - the card's **⋮** menu has a **View email** action
that opens the original message rendered as it was sent. Remote images
(including tracking pixels) stay blocked until you tick **Load remote
images**, and the message is shown in a locked-down sandbox with scripts
disabled, so opening one is safe.

Every tracked parcel also has a **Reset** action, which puts it back into
**Needs confirmation** and clears its tracking status - useful if a parcel
got mismatched to the wrong carrier, or you just want it re-checked from
scratch. If it came from an email, that email is re-scanned and re-detected
on the next mail check rather than being skipped as already processed.

"**Check mail now**" runs a sync immediately instead of waiting for the
next scheduled check, showing a live "checked X/Y" count for however long
it takes. (A manual check always does a full tracking refresh, ignoring the
refresh throttle described under Settings below.)

The count cards at the top double as section toggles - click one (or a
section's header) to collapse or expand that section's cards. The choice is
remembered per browser.

The **gear icon** in the header opens a **Settings** page for the
operational knobs you'd otherwise have to change in the add-on
configuration (and restart for):

- **Check email every** - how often mailboxes are scanned and tracking is
  refreshed. Applied immediately, without an add-on restart.
- **Refresh tracking status no more than every** - caps how often each
  parcel is re-queried from the tracking provider, to conserve API quota,
  independently of how often mail is checked. `0` means refresh on every
  check.
- **Scan emails from the last** - how far back each mail check looks.
- **Only scan / Trusted / Ignored sender domains** - the same sender-list
  controls as the add-on options.
- **Auto-archive** and **auto-dismiss** timings.

These override the matching add-on options while set. Mailbox accounts and
tracking-provider API keys stay in the add-on configuration (the managed,
secret store) and are not editable from here.

A **Data management** menu near the top of the dashboard lets you export
every tracked parcel as a JSON file, or wipe all parcels and sync history
to start fresh - the latter requires typing `RESET` to confirm, since it
can't be undone.

## Home Assistant entities

With no extra setup beyond installing the add-on, your parcels are also
exposed as Home Assistant sensor entities - using the add-on's own
Supervisor-granted access to the Home Assistant API, so no MQTT setup or
extra credentials are needed. If you're upgrading from an older version,
restart the add-on once so this new permission takes effect.

- `sensor.parcel_tracker_summary` - state is the number of currently
  tracked parcels (everything except archived/dismissed). Attributes
  include `pending_confirmation`, `in_transit`, and `delivered` counts,
  plus a `parcels` list with full detail for every tracked parcel (this
  is what the Lovelace card below reads).
- `sensor.parcel_tracker_<tracking-number>` - one entity per tracked
  parcel, state is its status (`pending`, `active`, `exception`, or
  `delivered`) - useful as an automation trigger for one specific
  package. The entity disappears once that parcel is archived or
  dismissed.

Example automation, notifying when a specific parcel is delivered (find
the exact entity id on the dashboard or under **Developer Tools →
States**, searching for `parcel_tracker`):

```yaml
automation:
  - alias: Notify when a parcel is delivered
    trigger:
      - platform: state
        entity_id: sensor.parcel_tracker_1z999aa10123456784
        to: "delivered"
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "Delivered: {{ trigger.to_state.attributes.description }}"
```

States set this way aren't tied to a registered integration, so they
briefly disappear across a Home Assistant restart until the next
scheduled check (governed by `poll_interval_minutes`) re-creates them.

## Lovelace card

A companion dashboard card ships with the add-on. It reads the parcel
list from `sensor.parcel_tracker_summary` via the card's normal `hass`
property.

### Recommended: serve from Home Assistant's own `/local/` path

The add-on automatically copies the card's JavaScript file into Home
Assistant's `/config/www/` folder on startup (and again whenever it
changes, on an upgrade), so it's reachable at a normal HA frontend URL -
this works through whatever remote-access setup Home Assistant itself is
reachable through (a Cloudflare Tunnel, Nabu Casa, etc.), not just the
local network the add-on's own direct port is limited to.

1. If you're upgrading from an older version, restart the add-on once so
   its newly-required `/config` write access takes effect.
2. **Settings → Dashboards → Resources → Add resource**. URL:
   `/local/parcel-tracker-card.js`, resource type **JavaScript module**.
3. Edit a dashboard, add a card, choose **Manual**, and use:
   ```yaml
   type: custom:parcel-tracker-card
   title: Parcels
   ```

Because `/local/` is served by Home Assistant itself rather than the
add-on, the card needs to be told where the add-on actually lives for its
on-demand fetches (the Archived group and a row's full tracking history -
see below). Add `api_base` pointing at the add-on's direct port:
```yaml
type: custom:parcel-tracker-card
title: Parcels
api_base: "http://<home-assistant-host-or-ip>:8000"
```

### Fallback: the add-on's direct port

If you'd rather not grant the add-on write access to `/config`, or just
haven't restarted yet, the card is still served from the add-on's own
direct port too, exactly as before (no `api_base` needed - this method
only works on the local network, not through remote access):

1. Make sure direct port access is enabled for the add-on (**Settings →
   Add-ons → Parcel Tracker**, the port row next to `8000`).
2. **Settings → Dashboards → Resources → Add resource**. URL:
   `http://<home-assistant-host-or-ip>:8000/static/parcel-tracker-card.js`,
   resource type **JavaScript module**.
3. Same manual card step as above.

The card mirrors the app's own dashboard: parcels are grouped into
collapsible Needs confirmation / In transit / Delivered / Archived
sections (collapsed state persists per-browser), each row shows a
progress stepper, carrier/ETA chips, and a confidence meter while
unconfirmed, with a link out to each carrier's tracking page. Click a
row to expand its full tracking history, fetched on demand from the
add-on's own `/api/parcels` endpoint - the entity attribute only ever
carries each parcel's latest status, since a full per-event history for
every parcel wouldn't fit within Home Assistant's attribute size limit.
The Archived section is also sourced from `/api/parcels`, since archived
and dismissed parcels aren't synced to Home Assistant at all (see
above). When installed via the direct-port fallback, both reuse the same
origin the card's own script was loaded from, so it works with nothing
extra to configure; when installed via `/local/`, they use the `api_base`
URL set above instead.

The card is read-only by design - it only ever reads parcel data, never
confirms, archives, or deletes anything. Use the app's own dashboard
(or its ingress panel) for those actions.

## Storage

All parcel and sync state is stored locally in `/data/parcels.db`
(SQLite). The mailbox itself is never modified.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **Parcel Tracker**
from the app store.
