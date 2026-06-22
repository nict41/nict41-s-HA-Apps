"""Read-only IMAP polling.

Connects with `select(folder, readonly=True)` so the mailbox is never
modified (no read-flag changes, no deletions). Dedup is by the `Message-ID`
header rather than IMAP UID, since UIDs are only stable within a single
UIDVALIDITY epoch and that epoch can change (e.g. if the mail provider
rebuilds the folder), which would otherwise risk silently re-processing
(or skipping) messages after a reconnect.
"""

import email
import imaplib
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message

import carriers
import db

IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USE_SSL = os.environ.get("IMAP_USE_SSL", "true").lower() == "true"
IMAP_USERNAME = os.environ.get("IMAP_USERNAME", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))

# Comma-separated extra sender domains to treat as trusted retailers (their
# tracking numbers get the high-confidence label parser instead of the
# generic regex fallback). Lets users add e.g. a niche retailer without a
# code change.
TRUSTED_SENDERS = frozenset(
    d.strip().lower() for d in os.environ.get("TRUSTED_SENDERS", "").split(",") if d.strip()
)
IGNORE_SENDERS = frozenset(
    d.strip().lower() for d in os.environ.get("IGNORE_SENDERS", "").split(",") if d.strip()
)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def _extract_body(msg: Message) -> str:
    """Prefer a plain-text part; fall back to stripping tags from HTML."""
    html_fallback = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif content_type == "text/html" and not html_fallback:
                payload = part.get_payload(decode=True)
                if payload:
                    html_fallback = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_fallback = text
            else:
                return text

    return carriers.strip_html(html_fallback) if html_fallback else ""


def _connect() -> imaplib.IMAP4:
    if IMAP_USE_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    else:
        conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        conn.starttls()
    conn.login(IMAP_USERNAME, IMAP_PASSWORD)
    # readonly=True: the inbox's read/unread state and contents are never
    # touched by this app.
    conn.select(IMAP_FOLDER, readonly=True)
    return conn


def sync_mailbox() -> dict:
    """Poll the mailbox once. Never raises - failures are reported in the
    returned dict so the caller can surface them without crashing the
    scheduler loop."""
    if not IMAP_HOST or not IMAP_USERNAME or not IMAP_PASSWORD:
        return {"ok": False, "error": "IMAP is not configured", "new_candidates": 0, "scanned": 0}

    try:
        conn = _connect()
    except (imaplib.IMAP4.error, OSError) as exc:
        return {"ok": False, "error": f"connection failed: {exc}", "new_candidates": 0, "scanned": 0}

    scanned = 0
    new_candidates = 0
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        status, data = conn.search(None, "SINCE", since)
        if status != "OK":
            return {"ok": False, "error": f"search failed: {status}", "new_candidates": 0, "scanned": 0}

        message_nums = data[0].split()
        for num in message_nums:
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            scanned += 1
            msg = email.message_from_bytes(msg_data[0])

            message_id = msg.get("Message-ID", "").strip()
            if not message_id:
                # Fall back to a synthetic id so messages without one (rare,
                # but seen from some legacy senders) still get deduped.
                message_id = f"{msg.get('From', '')}|{msg.get('Date', '')}|{msg.get('Subject', '')}"
            if db.is_processed(message_id):
                continue

            sender = _decode(msg.get("From", ""))
            sender_domain = sender.split("@")[-1].rstrip(">").lower() if "@" in sender else ""
            if sender_domain in IGNORE_SENDERS:
                db.mark_processed(message_id)
                continue

            subject = _decode(msg.get("Subject", ""))
            body_text = _extract_body(msg)

            candidates = carriers.detect_candidates(
                sender, subject, body_text, extra_trusted_domains=TRUSTED_SENDERS
            )
            for candidate in candidates:
                initial_status = (
                    db.STATUS_ACTIVE
                    if candidate.confidence >= carriers.CONFIRM_THRESHOLD
                    else db.STATUS_PENDING
                )
                db.upsert_parcel(
                    tracking_number=candidate.tracking_number,
                    carrier_name=candidate.carrier_name,
                    description=candidate.description,
                    confidence=candidate.confidence,
                    source_message_id=message_id,
                    initial_status=initial_status,
                )
                new_candidates += 1

            db.mark_processed(message_id)
    finally:
        try:
            conn.close()
        except imaplib.IMAP4.error:
            pass
        conn.logout()

    return {"ok": True, "error": None, "new_candidates": new_candidates, "scanned": scanned}
