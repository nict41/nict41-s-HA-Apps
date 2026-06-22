"""Tracking-number detection.

Two-tier approach:
1. Known retailer senders (AliExpress, eBay, Amazon) get a high-confidence,
   label-based parser, since their notification emails reliably print
   "Tracking Number: ..." style text.
2. Everything else falls back to generic carrier-shaped regexes, gated on
   some shipping-related context (a recognized sender or shipping keywords)
   to keep the false-positive rate down. Anything below CONFIRM_THRESHOLD
   lands in the dashboard's "needs confirmation" queue instead of being
   trusted outright.

Carrier names here are for display and for the no-API-key tracking-URL
fallback only. Status lookups always register with 17track using carrier
code 0 (auto-detect), since AliExpress/eBay cross-border shipments are
fragmented across dozens of regional carriers (Cainiao, 4PX, YunExpress,
Yanwen, ...) that 17track is specifically built to resolve from the number
itself.
"""

import re
from dataclasses import dataclass

RETAILER_DOMAINS = {
    "aliexpress.com": "AliExpress",
    "ebay.com": "eBay",
    "ebay.co.uk": "eBay",
    "ebay.de": "eBay",
    "ebay.com.au": "eBay",
    "amazon.com": "Amazon",
    "amazon.co.uk": "Amazon",
    "cainiao.com": "Cainiao",
}

CARRIER_SENDER_HINTS = {
    "usps.com": "USPS",
    "ups.com": "UPS",
    "fedex.com": "FedEx",
    "dhl.com": "DHL",
    "royalmail.com": "Royal Mail",
    "canadapost.ca": "Canada Post",
    "auspost.com.au": "Australia Post",
    "dpd.co.uk": "DPD",
    "dpd.com": "DPD",
    "evri.com": "Evri",
}

# UPU S10 format (2 letters + 9 digits + 2-letter country code, e.g.
# RR123456785CN) is the international postal standard. It covers China
# Post/ePacket, Hongkong Post, Singapore Post and most other national
# posts - i.e. the bulk of AliExpress shipments routed through a postal
# operator rather than a parcel carrier.
_UPU_S10_COUNTRY_NAMES = {
    "CN": "China Post / ePacket",
    "HK": "Hongkong Post",
    "SG": "Singapore Post",
    "GB": "Royal Mail",
    "US": "USPS",
    "FR": "La Poste",
    "DE": "Deutsche Post",
    "JP": "Japan Post",
    "KR": "Korea Post",
    "MY": "Pos Malaysia",
    "AU": "Australia Post",
    "CA": "Canada Post",
}

CONFIRM_THRESHOLD = 0.75

SHIPPING_KEYWORDS = (
    "tracking number",
    "track your package",
    "track your order",
    "has shipped",
    "is on its way",
    "out for delivery",
    "shipment",
    "courier",
    "parcel",
    "dispatched",
)

_TRACKING_URL_TEMPLATES = {
    "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels={number}",
    "UPS": "https://www.ups.com/track?tracknum={number}",
    "FedEx": "https://www.fedex.com/fedextrack/?trknbr={number}",
    "DHL": "https://www.dhl.com/global-en/home/tracking.html?tracking-id={number}",
    "Royal Mail": "https://www.royalmail.com/track-your-item#/tracking-results/{number}",
    "Canada Post": "https://www.canadapost-postescanada.ca/track-reperage/en#/search?searchFor={number}",
    "Australia Post": "https://auspost.com.au/mypost/track/#/details/{number}",
    "DPD": "https://track.dpd.co.uk/search?reference={number}",
    "Evri": "https://www.evri.com/track/parcel/{number}",
    "Cainiao / AliExpress Standard Shipping": "https://global.cainiao.com/detail.htm?mailNoList={number}",
    "Amazon Logistics": "https://www.amazon.com/progress-tracker/package/ref=ppx_yo2ov_dt_b_track_package?trackingId={number}",
}
# 17track's own public tracker auto-detects the carrier from the number, so
# it works as a universal fallback for carriers without a known URL
# template above - including most of AliExpress's long tail of regional
# logistics providers - whether or not an API key is configured.
_FALLBACK_TRACKING_URL = "https://t.17track.net/en#nums={number}"

_GENERIC_PATTERNS = [
    # (carrier name, regex, base confidence)
    ("UPS", re.compile(r"\b1Z[0-9A-Z]{16}\b"), 0.95),
    ("Amazon Logistics", re.compile(r"\bTBA\d{10,12}\b"), 0.9),
    ("Cainiao / AliExpress Standard Shipping", re.compile(r"\bLP\d{9,16}\b"), 0.85),
    ("YunExpress", re.compile(r"\bYT\d{10,20}\b"), 0.8),
    ("UPU S10 (international post)", re.compile(r"\b([A-Z]{2}\d{9}[A-Z]{2})\b"), 0.85),
    ("DPD", re.compile(r"\b\d{14}\b"), 0.5),
    ("FedEx", re.compile(r"\b\d{12}\b|\b\d{15}\b"), 0.4),
    ("USPS", re.compile(r"\b\d{20,22}\b"), 0.5),
    ("Generic numeric", re.compile(r"\b\d{10,22}\b"), 0.25),
]

_LABEL_RE = re.compile(
    r"track(?:ing)?\s*(?:number|no\.?|id|#)?\s*[:\-]?\s*([A-Za-z0-9]{8,30})",
    re.IGNORECASE,
)
_CARRIER_LABEL_RE = re.compile(
    r"(?:carrier|shipping\s*carrier|courier|shipped\s*(?:via|by|with))\s*[:\-]?\s*"
    r"([A-Za-z][A-Za-z0-9 .&'-]{1,30})",
    re.IGNORECASE,
)
_BOILERPLATE_SUBJECT_RE = re.compile(
    r"^(?:re|fwd?)\s*:\s*|"
    r"^(?:your\s+)?(?:order|item|package|parcel)\s*(?:has\s+)?(?:been\s+)?"
    r"(?:shipped|dispatched|on\s+its\s+way)\s*[:\-]?\s*",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    tracking_number: str
    carrier_name: str
    confidence: float
    description: str


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def sender_domain(sender: str) -> str:
    match = re.search(r"@([\w.-]+)", sender or "")
    return match.group(1).lower() if match else ""


def _upu_carrier_name(code: str) -> str:
    country = code[-2:].upper()
    return _UPU_S10_COUNTRY_NAMES.get(country, f"International post ({country})")


def _clean_subject(subject: str) -> str:
    cleaned = _BOILERPLATE_SUBJECT_RE.sub("", subject or "").strip(" -:")
    return cleaned if 3 <= len(cleaned) <= 80 else ""


def detect_candidates(
    sender: str, subject: str, body_text: str, extra_trusted_domains=frozenset()
) -> list[Candidate]:
    domain = sender_domain(sender)
    text = f"{subject}\n{body_text}"
    description = _clean_subject(subject)

    retailer = RETAILER_DOMAINS.get(domain)
    if not retailer and domain in extra_trusted_domains:
        retailer = domain

    found: dict[str, Candidate] = {}

    if retailer:
        for match in _LABEL_RE.finditer(text):
            number = match.group(1)
            window = text[match.end() : match.end() + 200]
            carrier_match = _CARRIER_LABEL_RE.search(window)
            carrier_name = carrier_match.group(1).strip() if carrier_match else retailer
            found[number] = Candidate(number, carrier_name, 0.92, description or retailer)

    if found:
        return list(found.values())

    sender_hint = CARRIER_SENDER_HINTS.get(domain)
    has_context = bool(retailer) or bool(sender_hint) or any(
        kw in text.lower() for kw in SHIPPING_KEYWORDS
    )
    if not has_context:
        return []

    for name, pattern, base_confidence in _GENERIC_PATTERNS:
        for match in pattern.finditer(text):
            number = match.group(0)
            if number in found:
                continue
            confidence = base_confidence
            carrier_name = _upu_carrier_name(number) if name == "UPU S10 (international post)" else name
            if sender_hint and sender_hint.lower() in name.lower():
                confidence = min(confidence + 0.15, 0.95)
            found[number] = Candidate(
                number, carrier_name, confidence, description or retailer or sender_hint or "Unknown sender"
            )

    return list(found.values())


def get_tracking_url(carrier_name: str, tracking_number: str) -> str:
    template = _TRACKING_URL_TEMPLATES.get(carrier_name, _FALLBACK_TRACKING_URL)
    return template.format(number=tracking_number)
