# Changelog

## 0.7.0

- Reworked the dashboard from the ground up. It now follows the device's
  light/dark setting, uses a consistent design system (typography,
  spacing, elevation, one accent colour) instead of ad-hoc styling, and
  leads with a summary row (needs confirmation / in transit / delivered /
  needs attention) for an at-a-glance read.
- Parcel cards now show a courier-style progress stepper (Detected → In
  transit → Out for delivery → Delivered) derived from each parcel's
  status, a carrier chip, a confidence meter on unconfirmed candidates,
  and clearer status badges and action buttons with icons.
- Timestamps are now humanized in the browser ("2h ago", "Today",
  "Tomorrow", "Jun 27, 07:05") rather than shown as raw ISO strings, and
  the expanded tracking history is rendered as a proper vertical timeline.
- The whole page stays self-contained (inline icon set, no external font
  or CDN request) so it still renders identically offline, is responsive
  down to phone widths, is keyboard-operable, and respects reduced-motion
  preferences.

## 0.6.18

- Fixed a regression from 0.6.17 that broke the Lovelace card entirely: it
  used `import.meta.url` to find the origin to fetch tracking history
  from, but that's a parse-time error for any script that isn't loaded as
  an ES module - which silently took down the whole card (the custom
  element never registered at all) for anyone whose Lovelace resource
  ended up registered as "JavaScript File" instead of "JavaScript Module"
  (an easy mix-up in the resource dialog). The origin is now found by
  looking up the card's own `<script src="...">` tag in the page instead,
  which Home Assistant's resource loader injects for both resource types,
  so the card now works (and still fetches history from the right origin)
  regardless of which one was picked.

## 0.6.17

- The Lovelace card's rows are now clickable to reveal the same full
  tracking history panel added to the dashboard in 0.6.16 - carrier link,
  tracking provider, first-detected/last-checked times, and every
  recorded event. Since a full history for every parcel wouldn't fit in
  Home Assistant's entity attribute size limit, the panel is fetched on
  demand from the add-on's own `/api/parcels` endpoint (the same origin
  the card's script itself was loaded from) the first time a row is
  expanded, rather than being carried in `hass.states` at all.
- Fixed a bug where Home Assistant entity states for pending/in-transit
  parcels never actually used the simplified `pending`/`active` values
  documented in the README - they leaked the internal
  `pending_confirmation`/`in_transit` status strings instead, which meant
  the Lovelace card's "Needs confirmation" and "In transit" groupings
  (and any automation written against the documented states) silently
  never matched real parcels.

## 0.6.16

- Cards on the dashboard are now clickable to reveal a full tracking
  history panel: every event Track123/17track has recorded for that
  parcel (time, description, and location when available), not just the
  single latest one previously shown in the status line.
- The expanded panel also surfaces a few things that weren't shown
  anywhere before: a carrier tracking link on every parcel (delivered and
  archived parcels previously had no tracking link at all), which
  provider confirmed the parcel, and when it was first detected vs. last
  checked.
- Parcels with no recorded history yet (e.g. carrier links only, no
  tracking provider configured) show a plain "No tracking history
  available yet" message instead of an empty panel.

## 0.6.15

- Fixed the "Check mail now" spinner spinning on every page load, sync or
  not: its CSS set `display` unconditionally, which silently overrides the
  browser's own `[hidden] { display: none }` rule regardless of the
  `hidden` attribute actually being present on the element.
- The dashboard now also asks the server on every load whether a sync is
  already running, instead of just assuming "Check mail now" is accurate.
  A sync keeps running on the server once started regardless of whether
  the page that triggered it is still around, so navigating away mid-sync
  and back (or just reloading) used to show the idle button even while a
  sync was genuinely still in progress - now it resumes showing live
  progress until the sync actually finishes.

## 0.6.14

- Track123 could leave a real, trackable number stuck showing "No status
  yet" indefinitely: the batch `track/query` endpoint it's normally polled
  through only reflects however far Track123's own background polling of
  the carrier has gotten, which can lag well behind the carrier's actual
  state for slower-to-sync cross-border networks (e.g. Cainiao) - even
  though Track123's own web tracker showed real events for the same number
  the whole time, since it queries the carrier live instead. A number
  accepted but still showing no tracking events now gets one rate-limited
  fallback call to that same live endpoint (`track/query-realtime`) before
  giving up for this cycle.
- The "Check mail now" progress count used to stop updating the instant the
  mail scan finished, then sit frozen for however long the tracking-provider
  refresh that follows took - indistinguishable from no progress being made
  at all. Live progress now spans both phases of a sync, with the button
  label switching to "Checking carrier status…" once the mail scan hands
  off to the provider refresh.
- The dashboard page is no longer cached by the browser, so a tab left open
  across an add-on rebuild always picks up the latest version on next load
  instead of continuing to serve whatever JS/HTML it loaded before.

## 0.6.13

- 0.6.12's switch to letting Track123 fully auto-detect the courier for
  Cainiao/AliExpress Standard Shipping numbers (`LP`/`JJD`-prefixed) turned
  out to reintroduce an older bug: auto-detect can reject one of these
  numbers outright at registration (`A0400: trackNo not registered`), even
  though Track123's own web tracker resolves it fine once a courier is
  known. Back to supplying a courier code for these numbers, using
  `cainiao` (the network handoff itself) rather than 0.6.10's `aliexpress`
  guess - the value that was actually getting them registered before.

## 0.6.12

- Reverted forcing Track123's `aliexpress` courier code onto Cainiao/
  AliExpress Standard Shipping numbers (`LP`/`JJD`-prefixed). Guessing
  `cainiao` vs. `aliexpress` ourselves wasn't actually more reliable than
  Track123's own auto-detection, which now decides the courier for these
  numbers same as every other carrier.

## 0.6.11

- Sped up mail checks: every message in the lookback window used to have
  its *entire* body (HTML, embedded images, attachments) fetched over IMAP
  on every single check, even ones already processed on a prior cycle -
  the dedup check only happened afterwards. A small headers-only fetch
  (Message-ID/From/Subject/Date) now happens first, cheap enough to do for
  the whole lookback window every time, and the much costlier full-body
  fetch only follows for messages that turn out to be new and not from an
  ignored sender.
- Added an `allowed_senders` option: a comma-separated list of sender
  domains to scan exclusively. When set, everything else is excluded
  straight out of the IMAP search itself, before any fetch happens at all
  - the fastest option on a general-purpose inbox where shipping
  notifications only ever come from a known handful of senders. Leave
  blank (the default) to keep scanning every sender, as before.
- "Check mail now" now shows a live "checked X/Y" count for however long
  the check takes, instead of just a spinner with no sense of progress.

## 0.6.10

- Cainiao/AliExpress Standard Shipping numbers (the `LP`/`JJD`-prefixed
  formats) are now registered with Track123 under its dedicated `aliexpress`
  courier code instead of the generic `cainiao` one. Track123 tracks
  AliExpress as its own courier, separate from the broader Cainiao network
  these numbers were previously registered under.

## 0.6.9

- Fixed a tracked parcel's status reverting to "No status yet" after
  previously showing real tracking events. A refresh cycle that came back
  from the tracking provider with no fresh status text (a momentary gap in
  the provider's response, a not-yet-indexed registration, etc.) was
  unconditionally overwriting the parcel's last-known-good status, event
  time, and estimated delivery with blanks instead of just leaving them as
  they were. The dashboard now keeps showing the most recent real status
  until the provider actually has something new to report.
- 17track/Track123 requests that succeed but don't return any usable
  tracking data for a specific number are now noted in the add-on's logs,
  to make it easier to tell a provider-side gap apart from a bug in this
  add-on if a parcel's status still looks wrong after the above fix.

## 0.6.8

- Removed the single-`folder` mailbox option. The `folders` list option
  introduced in 0.6.0 covers the same need (and defaults to `INBOX` when
  left blank), so configuring both was redundant - any mailbox still using
  `folder` should switch to `folders` before upgrading.
- Manually adding a tracking number that's already tracked (in any state,
  including archived/dismissed) now asks for confirmation first, since the
  tracking number column is unique and re-adding it would otherwise
  silently overwrite that parcel's carrier and description.
- Added a **Reset** action to every tracked parcel, which puts it back into
  needs-confirmation and clears its tracking status - useful when a parcel
  ended up mismatched to the wrong carrier, or you just want it picked up
  fresh. If the parcel came from an email, that email is re-scanned and
  re-detected on the next mail check instead of being skipped as already
  processed.
- Added a **Data management** menu to the dashboard, with an option to
  export every tracked parcel as a JSON file, and an option to wipe all
  parcels and sync history to start over completely - the latter requires
  typing `RESET` to confirm, since it can't be undone.

## 0.6.7

- Cainiao/AliExpress Standard Shipping numbers (the `LP`/`JJD`-prefixed
  formats) are now registered with Track123 along with an explicit courier
  code, instead of leaving carrier detection entirely to Track123's own
  auto-detect. Track123's docs recommend supplying a courier code at
  registration when known, and some Cainiao-network numbers were being
  rejected (`A0400: trackNo not registered`) under auto-detect despite
  Track123's own web tracker resolving them correctly once a courier is
  known. Numbers for every other carrier are unaffected and keep
  auto-detecting as before.

## 0.6.6

- The dashboard's "Track" link (and the `tracking_url` exposed on each
  Home Assistant sensor entity / the Lovelace card) now always points at
  Track123's own web tracker, instead of a per-carrier deep link built from
  our own carrier guess. Track123 reliably resolves the correct carrier
  straight from the number itself - including cross-border Cainiao/
  AliExpress numbers our own per-carrier links didn't handle well - so it
  works as a single, more reliable destination regardless of which carrier
  we think a number belongs to, and whether or not a Track123 API key is
  even configured.

## 0.6.5

- A tracking number that 17track/Track123 only matched to a carrier by the
  shape of the number itself - with no actual movement event behind that
  guess - is no longer treated as provider-confirmed. Both providers will
  occasionally guess a carrier from a number's format alone (e.g. a phone
  number, an eBay Item ID, or an order ID that happens to fit a carrier's
  number-length pattern), and since `provider_confirmed` is sticky, that
  false "confirmation" permanently exempted exactly this kind of false
  positive from the auto-dismiss feature meant to clean it up. A number now
  needs both a detected carrier *and* a real tracking event before it
  counts as confirmed. This only affects new confirmations going forward -
  a parcel already marked provider-confirmed under the old, looser check
  stays that way and can be removed by hand (Archive, then Delete) if it's
  actually bogus.

## 0.6.4

- A number Track123 explicitly rejects (e.g. quota exhausted, already
  imported under a different state) is no longer indistinguishable from one
  it simply has no data for yet - it previously just sat at "No status yet"
  forever with no clue why, even though the carrier's own tracking page (via
  the "Track" link) showed real updates. The rejection reason Track123
  returns is now shown as the status text, and also logged, including for
  registration-time rejections which have no status field to show it in.

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
