# Changelog

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
  tracking provider, so their card shows the actual detected carrier and a
  live status preview (e.g. correcting cases where the generic carrier
  guess was wrong) instead of relying solely on the email-text guess.
  Confirming or dismissing is still required before a parcel is tracked
  for real - the preview doesn't change its pending status.

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
