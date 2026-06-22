# Changelog

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
