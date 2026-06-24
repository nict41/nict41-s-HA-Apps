import imaplib

import pytest

import db
import mail_worker


@pytest.fixture(autouse=True)
def _fresh_db():
    db.DB_PATH.unlink(missing_ok=True)
    db.init_db()
    yield


def _raw_email(sender: str, subject: str, body: str, message_id: str) -> bytes:
    return (
        f"From: {sender}\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


class _FakeConn:
    """Stands in for imaplib's IMAP4 connection. `fetch` mimics imaplib's
    real quirk of returning literal-bearing FETCH responses as
    `(header_line, literal_bytes)` tuples rather than plain bytes."""

    def __init__(self, messages: dict[int, bytes], fail_search: bool = False):
        self._messages = messages
        self._fail_search = fail_search
        self.closed = False
        self.logged_out = False

    def search(self, charset, *criteria):
        if self._fail_search:
            return "NO", [b""]
        nums = b" ".join(str(n).encode() for n in self._messages)
        return "OK", [nums]

    def fetch(self, num, parts):
        raw = self._messages.get(int(num))
        if raw is None:
            return "NO", [None]
        header_line = f"{num} (RFC822 {{{len(raw)}}}".encode()
        return "OK", [(header_line, raw)]

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


def test_normalize_mailboxes_wraps_a_bare_mapping():
    # Regression test: with exactly one mailbox configured, the Supervisor's
    # repeating-group option can come through as a single mapping instead of
    # a one-item list - iterating that mapping directly yields its keys
    # (strings), which crashes _sync_account with AttributeError.
    account = {"host": "imap.example.com", "username": "a@example.com"}
    assert mail_worker._normalize_mailboxes(account) == [account]


def test_normalize_mailboxes_passes_through_a_list():
    accounts = [{"host": "imap.one.com"}, {"host": "imap.two.com"}]
    assert mail_worker._normalize_mailboxes(accounts) == accounts


def test_normalize_mailboxes_treats_other_values_as_empty():
    assert mail_worker._normalize_mailboxes(None) == []
    assert mail_worker._normalize_mailboxes("") == []


def test_sync_mailbox_with_no_accounts_configured():
    result = mail_worker.sync_mailbox()
    assert result == {"ok": False, "error": "no mailboxes configured", "new_candidates": 0, "scanned": 0}


def test_sync_account_parses_literal_tuple_fetch_response(monkeypatch):
    # Regression test: imaplib's FETCH response for RFC822 is a literal, so
    # the response part is a (header, bytes) tuple, not plain bytes - using
    # it directly as message bytes raises AttributeError on .decode().
    raw = _raw_email(
        "noreply@aliexpress.com",
        "Your order has shipped!",
        "Tracking Number: LP00123456789CN Carrier: Cainiao Standard",
        "<msg-1@aliexpress.com>",
    )
    fake_conn = _FakeConn({1: raw})
    monkeypatch.setattr(mail_worker, "_connect", lambda account: fake_conn)
    monkeypatch.setattr(mail_worker, "MAILBOXES", [{"host": "imap.example.com", "username": "a@example.com"}])

    result = mail_worker.sync_mailbox()

    assert result["ok"] is True
    assert result["scanned"] == 1
    assert result["new_candidates"] == 1
    assert fake_conn.closed and fake_conn.logged_out
    parcels = db.list_parcels()
    assert len(parcels) == 1
    assert parcels[0]["tracking_number"] == "LP00123456789CN"


def test_sync_mailbox_scans_every_configured_account(monkeypatch):
    raw_a = _raw_email(
        "noreply@aliexpress.com", "Shipped!", "Tracking Number: LP00123456789CN", "<msg-a@aliexpress.com>"
    )
    raw_b = _raw_email(
        "ebay@ebay.com", "Your item has shipped", "Tracking number: 1Z999AA10123456784 Carrier: UPS", "<msg-b@ebay.com>"
    )
    conn_a = _FakeConn({1: raw_a})
    conn_b = _FakeConn({1: raw_b})
    conns = {"account-a": conn_a, "account-b": conn_b}
    monkeypatch.setattr(mail_worker, "_connect", lambda account: conns[account["username"]])
    monkeypatch.setattr(
        mail_worker,
        "MAILBOXES",
        [
            {"host": "imap.one.com", "username": "account-a"},
            {"host": "imap.two.com", "username": "account-b"},
        ],
    )

    result = mail_worker.sync_mailbox()

    assert result["ok"] is True
    assert result["scanned"] == 2
    assert result["new_candidates"] == 2
    tracking_numbers = {p["tracking_number"] for p in db.list_parcels()}
    assert tracking_numbers == {"LP00123456789CN", "1Z999AA10123456784"}


def test_sync_mailbox_continues_past_one_failing_account(monkeypatch):
    raw = _raw_email(
        "noreply@aliexpress.com", "Shipped!", "Tracking Number: LP00123456789CN", "<msg-ok@aliexpress.com>"
    )
    good_conn = _FakeConn({1: raw})

    def fake_connect(account):
        if account["username"] == "broken":
            raise imaplib.IMAP4.error("login failed")
        return good_conn

    monkeypatch.setattr(mail_worker, "_connect", fake_connect)
    monkeypatch.setattr(
        mail_worker,
        "MAILBOXES",
        [
            {"host": "imap.broken.com", "username": "broken"},
            {"host": "imap.good.com", "username": "good"},
        ],
    )

    result = mail_worker.sync_mailbox()

    assert result["ok"] is False
    assert "broken" in result["error"]
    assert result["scanned"] == 1
    assert result["new_candidates"] == 1


def test_sync_account_skips_already_processed_messages(monkeypatch):
    raw = _raw_email(
        "noreply@aliexpress.com", "Shipped!", "Tracking Number: LP00123456789CN", "<msg-dup@aliexpress.com>"
    )
    fake_conn = _FakeConn({1: raw})
    monkeypatch.setattr(mail_worker, "_connect", lambda account: fake_conn)
    monkeypatch.setattr(mail_worker, "MAILBOXES", [{"host": "imap.example.com", "username": "a@example.com"}])

    mail_worker.sync_mailbox()
    result = mail_worker.sync_mailbox()

    assert result["scanned"] == 1
    assert result["new_candidates"] == 0


def test_sync_account_respects_ignore_senders(monkeypatch):
    raw = _raw_email(
        "newsletter@noisyretailer.com",
        "Your parcel is on its way",
        "Tracking Number: LP00123456789CN",
        "<msg-ignored@noisyretailer.com>",
    )
    fake_conn = _FakeConn({1: raw})
    monkeypatch.setattr(mail_worker, "_connect", lambda account: fake_conn)
    monkeypatch.setattr(mail_worker, "MAILBOXES", [{"host": "imap.example.com", "username": "a@example.com"}])
    monkeypatch.setattr(mail_worker, "IGNORE_SENDERS", frozenset({"noisyretailer.com"}))

    result = mail_worker.sync_mailbox()

    assert result["scanned"] == 1
    assert result["new_candidates"] == 0
    assert db.list_parcels() == []
