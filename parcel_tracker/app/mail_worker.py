"""Read-only IMAP polling, across one or more configured mailboxes, each of
which can scan one or several folders.

Connects with `select(folder, readonly=True)` so a mailbox is never
modified (no read-flag changes, no deletions). Dedup is by the `Message-ID`
header rather than IMAP UID, since UIDs are only stable within a single
UIDVALIDITY epoch and that epoch can change (e.g. if the mail provider
rebuilds the folder), which would otherwise risk silently re-processing
(or skipping) messages after a reconnect.

Every message returned by the date-bounded SEARCH is fetched twice at
most: a small headers-only FETCH first (Message-ID/From/Subject/Date),
cheap enough to do for the whole lookback window every cycle, followed by
a full-body FETCH only for messages that turn out to be new and not from
an ignored sender. Without this split, every message in the lookback
window - including ones already processed on a prior cycle - would have
its entire body (HTML, embedded images, attachments) pulled over the wire
again on every single poll, which is the main reason a mail check can be
slow on a busy or long-lookback inbox.
"""

import email
import imaplib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message

import carriers
import db


def _parse_mailboxes(raw_json: str) -> list[dict]:
    """bashio emits list-type options as one JSON value per array element
    rather than a single JSON array - for a single mailbox that's a bare
    mapping, for several it's multiple JSON objects with no separator
    between them. Decode however many JSON values are present and return
    them as a flat list, whatever shape they came in."""
    raw_json = raw_json.strip()
    if not raw_json:
        return []

    decoder = json.JSONDecoder()
    values = []
    idx = 0
    while idx < len(raw_json):
        value, idx = decoder.raw_decode(raw_json, idx)
        values.append(value)
        while idx < len(raw_json) and raw_json[idx].isspace():
            idx += 1

    if len(values) == 1 and isinstance(values[0], list):
        return values[0]
    return values


MAILBOXES: list[dict] = _parse_mailboxes(os.environ.get("MAILBOXES_JSON", "[]"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))

# Without an explicit timeout, a stalled/unresponsive IMAP server blocks the
# underlying socket forever - and since the manual "Check mail now" button
# runs this synchronously on the request thread, an indefinite hang here
# previously froze the whole dashboard, not just the sync.
_IMAP_TIMEOUT_SECONDS = 30

# Bounds how much of a source email gets stored per parcel, for the
# dashboard's "view full email" preview. Shipping emails are short; this is
# just a backstop against storing an unbounded amount of text per parcel.
MAX_EMAIL_BODY_CHARS = 8000

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

# Comma-separated sender domains to scan exclusively - everything else is
# excluded straight out of the IMAP SEARCH itself, before any FETCH happens,
# rather than fetched and then discarded. Left blank (the default), every
# sender is scanned as before. Most useful on a general-purpose inbox where
# shipping notifications only ever come from a known handful of senders.
ALLOWED_SENDERS = frozenset(
    d.strip().lower() for d in os.environ.get("ALLOWED_SENDERS", "").split(",") if d.strip()
)


def _from_search_terms(domains: list[str]) -> list[str]:
    """Builds an IMAP SEARCH term matching any of the given sender domains:
    `FROM d1` for one domain, or a right-nested `OR FROM d1 OR FROM d2 ...
    FROM dN` chain for several - IMAP's OR takes exactly two operands, so 3+
    alternatives need nesting, but the grammar parses this flat chain
    correctly without explicit parentheses since each operand is itself a
    complete search-key (and "OR ..." is one). FROM matches as a substring
    against the raw header, same as IMAP servers do natively, so a
    subdomain like notice.aliexpress.com still matches an aliexpress.com
    entry."""
    terms: list[str] = []
    for i, domain in enumerate(domains):
        if i < len(domains) - 1:
            terms.append("OR")
        terms.extend(["FROM", domain])
    return terms


def _search_criteria(since: str) -> list[str]:
    criteria = ["SINCE", since]
    if ALLOWED_SENDERS:
        criteria.extend(_from_search_terms(sorted(ALLOWED_SENDERS)))
    return criteria


# Lets the dashboard poll for a live "checked X of Y" count while a sync is
# running, instead of just showing a spinner for however long the mail check
# takes. Guarded by a lock since the sync itself runs on a worker thread
# (via run_in_threadpool) while the status poll runs on the main event loop.
_progress_lock = threading.Lock()
_progress = {"running": False, "checked": 0, "total": 0}


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _progress_start() -> None:
    with _progress_lock:
        _progress.update(running=True, checked=0, total=0)


def _progress_finish() -> None:
    with _progress_lock:
        _progress["running"] = False


def _progress_add_total(count: int) -> None:
    with _progress_lock:
        _progress["total"] += count


def _progress_increment_checked() -> None:
    with _progress_lock:
        _progress["checked"] += 1


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


def _account_folders(account: dict) -> list[str]:
    """A mailbox scans `folders` - e.g. INBOX plus a "Shipping" label or an
    "Archive" folder shipping notifications get filtered into. Defaults to
    just INBOX when left blank."""
    folders = account.get("folders") or []
    if isinstance(folders, str):
        folders = [folders]
    folders = [f.strip() for f in folders if f and str(f).strip()]
    return folders or ["INBOX"]


def _connect(account: dict) -> imaplib.IMAP4:
    host = account["host"]
    port = int(account.get("port") or 993)
    if account.get("use_ssl", True):
        conn = imaplib.IMAP4_SSL(host, port, timeout=_IMAP_TIMEOUT_SECONDS)
    else:
        conn = imaplib.IMAP4(host, port, timeout=_IMAP_TIMEOUT_SECONDS)
        conn.starttls()
    conn.login(account["username"], account["password"])
    return conn


_HEADER_FETCH_PARTS = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])"
_BODY_FETCH_PARTS = "(RFC822)"


def _fetch_part(conn: imaplib.IMAP4, num: bytes, parts: str) -> bytes | None:
    status, msg_data = conn.fetch(num, parts)
    if status != "OK" or not msg_data:
        return None
    return _message_bytes(msg_data[0])


def _sync_folder(conn: imaplib.IMAP4, label: str, folder: str, since: str) -> dict:
    scanned = 0
    new_candidates = 0

    # readonly=True: the mailbox's read/unread state and contents are never
    # touched by this app.
    status, _data = conn.select(folder, readonly=True)
    if status != "OK":
        return {"ok": False, "error": f"{label}/{folder}: select failed: {status}", "new_candidates": 0, "scanned": 0}

    status, data = conn.search(None, *_search_criteria(since))
    if status != "OK":
        return {"ok": False, "error": f"{label}/{folder}: search failed: {status}", "new_candidates": 0, "scanned": 0}

    message_nums = data[0].split()
    _progress_add_total(len(message_nums))

    for num in message_nums:
        try:
            # Headers only first - cheap enough to do for every message in
            # the lookback window every cycle. Only a message that turns out
            # to be new and not ignored is worth the much costlier full-body
            # fetch below.
            header_raw = _fetch_part(conn, num, _HEADER_FETCH_PARTS)
            if not header_raw:
                continue
            scanned += 1
            header_msg = email.message_from_bytes(header_raw)

            message_id = header_msg.get("Message-ID", "").strip()
            if not message_id:
                # Fall back to a synthetic id so messages without one (rare,
                # but seen from some legacy senders) still get deduped.
                message_id = (
                    f"{header_msg.get('From', '')}|{header_msg.get('Date', '')}|"
                    f"{header_msg.get('Subject', '')}"
                )
            if db.is_processed(message_id):
                continue

            sender = _decode(header_msg.get("From", ""))
            if carriers.sender_domain(sender) in IGNORE_SENDERS:
                db.mark_processed(message_id)
                continue

            raw = _fetch_part(conn, num, _BODY_FETCH_PARTS)
            if not raw:
                continue
            msg = email.message_from_bytes(raw)

            subject = _decode(header_msg.get("Subject", ""))
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
                    email_sender=sender,
                    email_subject=subject,
                    email_body=body_text[:MAX_EMAIL_BODY_CHARS],
                )
                new_candidates += 1

            db.mark_processed(message_id)
        finally:
            _progress_increment_checked()

    return {"ok": True, "error": None, "new_candidates": new_candidates, "scanned": scanned}


def _sync_account(account: dict) -> dict:
    label = account.get("username") or account.get("host") or "mailbox"

    try:
        conn = _connect(account)
    except (imaplib.IMAP4.error, OSError) as exc:
        return {"ok": False, "error": f"{label}: connection failed: {exc}", "new_candidates": 0, "scanned": 0}

    scanned = 0
    new_candidates = 0
    errors = []
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        for folder in _account_folders(account):
            try:
                result = _sync_folder(conn, label, folder, since)
            except (imaplib.IMAP4.error, OSError) as exc:
                # One folder erroring out (timeout, transient server hiccup,
                # ...) shouldn't stop the rest of the account's folders - or
                # any other configured mailbox - from being scanned.
                errors.append(f"{label}/{folder}: {exc}")
                continue
            scanned += result["scanned"]
            new_candidates += result["new_candidates"]
            if not result["ok"]:
                errors.append(result["error"])
    finally:
        try:
            conn.close()
        except (imaplib.IMAP4.error, OSError):
            pass
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    return {
        "ok": not errors,
        "error": "; ".join(errors) if errors else None,
        "new_candidates": new_candidates,
        "scanned": scanned,
    }


def sync_mailbox() -> dict:
    """Poll every configured mailbox once. Never raises - a failure on one
    account is reported without stopping the others from being checked."""
    if not MAILBOXES:
        return {"ok": False, "error": "no mailboxes configured", "new_candidates": 0, "scanned": 0}

    _progress_start()
    try:
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
    finally:
        _progress_finish()
