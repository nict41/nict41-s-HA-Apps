"""Read-only IMAP polling, across one or more configured mailboxes.

Connects with `select(folder, readonly=True)` so a mailbox is never
modified (no read-flag changes, no deletions). Dedup is by the `Message-ID`
header rather than IMAP UID, since UIDs are only stable within a single
UIDVALIDITY epoch and that epoch can change (e.g. if the mail provider
rebuilds the folder), which would otherwise risk silently re-processing
(or skipping) messages after a reconnect.
"""

import email
import imaplib
import json
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message

import carriers
import db

MAILBOXES: list[dict] = json.loads(os.environ.get("MAILBOXES_JSON", "[]"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))

# Comma-separated extra sender domains to treat as trusted retailers (their
# tracking numbers get the high-confidence label parser instead of the
# generic regex fallback). Lets users add e.g. a niche retailer without a
# code change. Applies across every configured mailbox.
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


def _message_bytes(fetch_part) -> bytes | None:
    """A `FETCH ... (RFC822)` response part is a literal, so imaplib hands it
    back as a `(header_line, literal_bytes)` tuple rather than plain bytes -
    the actual message lives in the second element."""
    if isinstance(fetch_part, tuple):
        return fetch_part[1]
    if isinstance(fetch_part, bytes):
        return fetch_part
    return None


def _connect(account: dict) -> imaplib.IMAP4:
    host = account["host"]
    port = int(account.get("port") or 993)
    if account.get("use_ssl", True):
        conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port)
        conn.starttls()
    conn.login(account["username"], account["password"])
    # readonly=True: the mailbox's read/unread state and contents are never
    # touched by this app.
    conn.select(account.get("folder") or "INBOX", readonly=True)
    return conn


def _sync_account(account: dict) -> dict:
    label = account.get("username") or account.get("host") or "mailbox"

    try:
        conn = _connect(account)
    except (imaplib.IMAP4.error, OSError) as exc:
        return {"ok": False, "error": f"{label}: connection failed: {exc}", "new_candidates": 0, "scanned": 0}

    scanned = 0
    new_candidates = 0
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        status, data = conn.search(None, "SINCE", since)
        if status != "OK":
            return {"ok": False, "error": f"{label}: search failed: {status}", "new_candidates": 0, "scanned": 0}

        message_nums = data[0].split()
        for num in message_nums:
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw = _message_bytes(msg_data[0])
            if not raw:
                continue
            scanned += 1
            msg = email.message_from_bytes(raw)

            message_id = msg.get("Message-ID", "").strip()
            if not message_id:
                # Fall back to a synthetic id so messages without one (rare,
                # but seen from some legacy senders) still get deduped.
                message_id = f"{msg.get('From', '')}|{msg.get('Date', '')}|{msg.get('Subject', '')}"
            if db.is_processed(message_id):
                continue

            sender = _decode(msg.get("From", ""))
            if carriers.sender_domain(sender) in IGNORE_SENDERS:
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


def sync_mailbox() -> dict:
    """Poll every configured mailbox once. Never raises - a failure on one
    account is reported without stopping the others from being checked."""
    if not MAILBOXES:
        return {"ok": False, "error": "no mailboxes configured", "new_candidates": 0, "scanned": 0}

    scanned = 0
    new_candidates = 0
    errors = []
    for account in MAILBOXES:
        result = _sync_account(account)
        scanned += result["scanned"]
        new_candidates += result["new_candidates"]
        if not result["ok"]:
            errors.append(result["error"])

    return {
        "ok": not errors,
        "error": "; ".join(errors) if errors else None,
        "new_candidates": new_candidates,
        "scanned": scanned,
    }
