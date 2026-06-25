# Changelog

## 0.6.3

- "Check mail now" now shows a spinner and disables the button for the
  duration of the check, instead of giving no feedback that the click
  registered. The check itself can take a while (IMAP connections,
  tracking-provider lookups), so without this it looked like nothing was
  happening until the page eventually reloaded.

## 0.6.2

- Fixed Track123-tracked parcels getting stuck showing "in transit" with no
  status text, despite the carrier's own tracking page showing real
  updates. A cross-border parcel (e.g. AliExpress/Cainiao) is tracked by
  Track123 in two legs: an international leg, then a local last-mile
  courier once it reaches the destination country - and the last-mile
  leg's events, which are the freshest ones once that handoff happens,
  were never read, only the (by then stale) international leg's. Both legs
  are now checked, preferring the last-mile leg's events and carrier name
  once it has any.

## 0.6.1

- Fixed the Lovelace card not showing up in the card picker, and failing
  to load when added manually via YAML (`Custom element doesn't exist:
  parcel-tracker-card`), even though the resource URL loaded fine when
  opened directly in a browser. Home Assistant's frontend loads a
  "JavaScript module" resource via a cross-origin `import()` - the add-on's
  direct port differs from HA's own frontend port - which browsers silently
  block without an `Access-Control-Allow-Origin` header, so the card's
  script never ran and never registered itself. A plain browser
  navigation to the same URL isn't subject to that restriction, which is
  why the resource appeared to load fine on its own. The add-on's static
  asset route now sends that header.

## 0.6.0

- Fixed a freeze during mail sync: IMAP connections had no socket timeout,
  so an unresponsive mail server could hang a sync indefinitely - and since
  the "Check mail now" button ran the sync directly on the dashboard's
  request-handling thread, that hang froze the entire dashboard, not just
  the sync. Connections now time out after 30 seconds, and "Check mail now"
  runs the sync off the dashboard's request thread instead of blocking it.
- Added a `folders` option per mailbox, to scan several folders (e.g.
  `INBOX` plus a "Shipping" label or a filtered-into folder) instead of
  just one. Existing single-`folder` configs keep working unchanged. One
  folder erroring out (a timeout, a transient server hiccup) no longer
  stops the rest of that account's folders, or any other configured
  mailbox, from being scanned.
- A tracking number a configured provider (17track/Track123) has *never*
  positively recognised - our own pattern-matching guessed wrong, e.g. an
  order ID that happened to look like a carrier's tracking-number format -
  is now automatically dismissed after `dismiss_unconfirmed_after_days`
  (default 3, `0` disables), since the provider's response is the strongest
  signal available for whether something is actually a real tracking
  number. A parcel a provider has confirmed even once stays exempt from
  this even if a later check is inconclusive, and the grace period only
  starts once a provider has actually had a chance to check a number, so
  upgrading (or newly configuring a provider) doesn't put existing parcels
  at risk of being dismissed on the very next check.

## 0.5.0

- Every tracked parcel is now exposed as a Home Assistant sensor entity
  (`sensor.parcel_tracker_<tracking-number>`), plus a
  `sensor.parcel_tracker_summary` entity carrying counts and full parcel
  detail - using the add-on's own Supervisor-granted Home Assistant API
  access, with no MQTT setup or extra credentials required. Entities for
  archived/dismissed parcels are removed automatically. Lets automations
  trigger off a specific package's status.
- Added a companion Lovelace custom card (`parcel-tracker-card`), served
  from the add-on's direct port, that reads the summary entity and shows
  the same Needs confirmation / In transit / Delivered grouping as the
  app's own dashboard - so parcels can be checked from a normal Home
  Assistant dashboard without opening the add-on.
- Improved tracking-number detection accuracy:
  - Retailer notification mail sent from a subdomain (e.g.
    `notice.aliexpress.com`) is now correctly recognised as that retailer,
    rather than only matching the bare domain.
  - Added support for Cainiao/AliExpress Standard Shipping's `JJD`-prefixed
    tracking number format, alongside the existing `LP`-prefixed one.
  - eBay/AliExpress order-confirmation emails no longer have their Item
    ID, Order number, or invoice/transaction ID mistaken for a tracking
    number - these numeric IDs happened to collide with the generic
    carrier-shaped patterns (e.g. a 12-digit eBay Item ID matching the
    FedEx pattern) used as a fallback when no real tracking number is
    present yet.
  - A "Track delivery"/"Track shipment"-style button next to a retailer's
    tracking-number label is no longer mistaken for the tracking number
    itself.

## 0.4.0

- Added [Track123](https://www.track123.com/) as a second, independently
  metered tracking provider alongside 17track, since 17track's free
  allowance is now a one-time, non-renewing 200-number trial rather than a
  monthly quota. Configure either or both via the `track123_api_key` /
  `seventeentrack_api_key` options - a parcel sticks to whichever provider
  it was first registered with, and new parcels prefer whichever
  configured provider's quota renews (Track123) before falling back to
  the other.
- Parcels awaiting confirmation are now also registered with a configured
  tracking provider. When the provider positively recognises a queued
  number (it resolves a real carrier or returns an actual tracking event),
  the parcel is **auto-confirmed** and starts tracking with the provider's
  carrier - so e.g. an eBay shipment the email pattern-matched as the wrong
  carrier gets corrected and confirmed automatically. Numbers the provider
  can't identify stay in the needs-confirmation queue with a status
  preview, for you to confirm or dismiss by hand.

## 0.3.0

- Pending parcel cards on the dashboard now show a short preview of the
  source email, with an expandable "View full email" section showing the
  sender, subject, and full body - useful for judging whether a
  low-confidence detection is actually a real tracking number before
  confirming or dismissing it.

## 0.2.2

- Fixed a startup crash (`json.decoder.JSONDecodeError: Extra data`) with
  two or more mailboxes configured. The Supervisor emits each mailbox
  entry as its own JSON object with no enclosing array or separator
  between them, which a plain JSON parse can't handle. Mailbox config is
  now parsed as however many JSON values are present, in whatever shape
  they arrive (a bare mapping, several concatenated mappings, or a proper
  array).

## 0.2.1

- Fixed a crash (`AttributeError: 'str' object has no attribute 'get'`)
  during mail sync when exactly one mailbox was configured. The Supervisor
  can return the `mailboxes` option as a single mapping rather than a
  one-item list in that case; mailbox entries are now normalized into a
  list regardless of shape.

## 0.2.0

- Fixed a crash (`AttributeError: 'tuple' object has no attribute 'decode'`)
  during mail sync, caused by treating the IMAP literal-bearing FETCH
  response as raw message bytes instead of unwrapping the
  `(header, bytes)` tuple imaplib returns for it.
- Mailboxes are now configured as a repeatable list, so multiple email
  accounts can be scanned for shipping emails instead of just one. This
  replaces the previous flat `imap_host`/`imap_port`/`imap_username`/etc.
  options with a `mailboxes` entry per account.

## 0.1.0

- Initial release.
- Read-only IMAP polling detects tracking numbers from shipping emails,
  with high-confidence parsing for AliExpress, eBay, Amazon and Cainiao,
  and a generic carrier-pattern fallback (UPS, USPS, FedEx, DHL, Royal
  Mail, DPD, Evri, YunExpress, and international post via the UPU S10
  format) for everything else.
- Optional live delivery status via the 17track API; falls back to
  carrier tracking links when no API key is configured.
- Ingress dashboard for confirming low-confidence detections, viewing
  in-transit/delivered parcels, and manually adding tracking numbers.
- Auto-archives delivered parcels after a configurable number of days.
