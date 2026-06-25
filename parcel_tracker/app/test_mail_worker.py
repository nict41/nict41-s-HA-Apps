import imaplib
import json

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

    def __init__(
        self,
        messages: dict[int, bytes] | None = None,
        fail_search: bool = False,
        fail_select: bool = False,
        messages_by_folder: dict[str, dict[int, bytes]] | None = None,
    ):
        # Either a flat {num: raw} dict (single-folder tests, folder ignored),
        # or a {folder: {num: raw}} dict keyed by whichever folder was last
        # selected (multi-folder tests).
        self._messages_by_folder = messages_by_folder
        self._messages = messages or {}
        self._fail_search = fail_search
        self._fail_select = fail_select
        self.closed = False
        self.logged_out = False
        self.selected_folders = []
        self._current_folder = None

    def select(self, folder, readonly=False):
        self.selected_folders.append(folder)
        self._current_folder = folder
        if self._fail_select:
            return "NO", [b""]
        return "OK", [b"1"]

    def _active_messages(self) -> dict[int, bytes]:
        if self._messages_by_folder is not None:
            return self._messages_by_folder.get(self._current_folder, {})
        return self._messages

    def search(self, charset, *criteria):
        if self._fail_search:
            return "NO", [b""]
        nums = b" ".join(str(n).encode() for n in self._active_messages())
        return "OK", [nums]

    def fetch(self, num, parts):
        raw = self._active_messages().get(int(num))
        if raw is None:
            return "NO", [None]
        header_line = f"{num} (RFC822 {{{len(raw)}}}".encode()
        return "OK", [(header_line, raw)]

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


def test_parse_mailboxes_handles_empty_string():
    assert mail_worker._parse_mailboxes("") == []
    assert mail_worker._parse_mailboxes("   ") == []


def test_parse_mailboxes_wraps_a_bare_mapping():
    # Regression test: with exactly one mailbox configured, bashio emits the
    # single array element as a bare JSON object rather than a one-item
    # array - iterating that mapping directly yields its keys (strings),
    # which crashed _sync_account with AttributeError.
    account = {"host": "imap.example.com", "username": "a@example.com"}
    raw_json = json.dumps(account)
    assert mail_worker._parse_mailboxes(raw_json) == [account]


def test_parse_mailboxes_handles_concatenated_objects():
    # Regression test: with two or more mailboxes configured, bashio emits
    # one JSON object per array element with no separator between them
    # (not wrapped in `[...]`), which made plain json.loads() raise
    # "Extra data" and crash the app on startup.
    account_a = {"host": "imap.one.com", "username": "a@example.com"}
    account_b = {"host": "imap.two.com", "username": "b@example.com"}
    raw_json = json.dumps(account_a) + "\n" + json.dumps(account_b)
    assert mail_worker._parse_mailboxes(raw_json) == [account_a, account_b]


def test_parse_mailboxes_passes_through_a_proper_array():
    accounts = [{"host": "imap.one.com"}, {"host": "imap.two.com"}]
    assert mail_worker._parse_mailboxes(json.dumps(accounts)) == accounts


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
    assert parcels[0]["email_sender"] == "noreply@aliexpress.com"
    assert parcels[0]["email_subject"] == "Your order has shipped!"
    assert "LP00123456789CN" in parcels[0]["email_body"]


def test_sync_account_truncates_long_email_body(monkeypatch):
    long_body = "Tracking Number: LP00123456789CN " + ("x" * 9000)
    raw = _raw_email("noreply@aliexpress.com", "Shipped!", long_body, "<msg-long@aliexpress.com>")
    fake_conn = _FakeConn({1: raw})
    monkeypatch.setattr(mail_worker, "_connect", lambda account: fake_conn)
    monkeypatch.setattr(mail_worker, "MAILBOXES", [{"host": "imap.example.com", "username": "a@example.com"}])

    mail_worker.sync_mailbox()

    parcel = db.list_parcels()[0]
    assert len(parcel["email_body"]) == mail_worker.MAX_EMAIL_BODY_CHARS


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


def test_account_folders_defaults_to_inbox():
    assert mail_worker._account_folders({}) == ["INBOX"]


def test_account_folders_uses_folders_list():
    assert mail_worker._account_folders({"folders": ["INBOX", "Shipping"]}) == [
        "INBOX",
        "Shipping",
    ]


def test_account_folders_strips_blank_entries():
    assert mail_worker._account_folders({"folders": ["INBOX", "  ", ""]}) == ["INBOX"]


def test_sync_account_scans_every_configured_folder(monkeypatch):
    raw_inbox = _raw_email(
        "noreply@aliexpress.com", "Shipped!", "Tracking Number: LP00123456789CN", "<msg-inbox@aliexpress.com>"
    )
    raw_shipping = _raw_email(
        "ebay@ebay.com", "Your item has shipped", "Tracking number: 1Z999AA10123456784 Carrier: UPS", "<msg-shipping@ebay.com>"
    )
    fake_conn = _FakeConn(messages_by_folder={"INBOX": {1: raw_inbox}, "Shipping": {1: raw_shipping}})
    monkeypatch.setattr(mail_worker, "_connect", lambda account: fake_conn)
    monkeypatch.setattr(
        mail_worker,
        "MAILBOXES",
        [{"host": "imap.example.com", "username": "a@example.com", "folders": ["INBOX", "Shipping"]}],
    )

    result = mail_worker.sync_mailbox()

    assert result["ok"] is True
    assert result["scanned"] == 2
    assert result["new_candidates"] == 2
    assert fake_conn.selected_folders == ["INBOX", "Shipping"]
    assert fake_conn.closed and fake_conn.logged_out
    tracking_numbers = {p["tracking_number"] for p in db.list_parcels()}
    assert tracking_numbers == {"LP00123456789CN", "1Z999AA10123456784"}


def test_sync_account_continues_past_one_failing_folder(monkeypatch):
    raw_shipping = _raw_email(
        "ebay@ebay.com", "Your item has shipped", "Tracking number: 1Z999AA10123456784 Carrier: UPS", "<msg-shipping@ebay.com>"
    )

    class _PartialFailConn(_FakeConn):
        def select(self, folder, readonly=False):
            self.selected_folders.append(folder)
            self._current_folder = folder
            if folder == "Broken":
                return "NO", [b""]
            return "OK", [b"1"]

    fake_conn = _PartialFailConn(messages_by_folder={"Shipping": {1: raw_shipping}})
    monkeypatch.setattr(mail_worker, "_connect", lambda account: fake_conn)
    monkeypatch.setattr(
        mail_worker,
        "MAILBOXES",
        [{"host": "imap.example.com", "username": "a@example.com", "folders": ["Broken", "Shipping"]}],
    )

    result = mail_worker.sync_mailbox()

    assert result["ok"] is False
    assert "Broken" in result["error"]
    assert result["scanned"] == 1
    assert result["new_candidates"] == 1
    tracking_numbers = {p["tracking_number"] for p in db.list_parcels()}
    assert tracking_numbers == {"1Z999AA10123456784"}


def test_connect_passes_explicit_timeout(monkeypatch):
    # Regression test: without an explicit timeout, a stalled IMAP server
    # blocks the connection's socket forever, freezing the whole sync (and,
    # via the synchronous "Check mail now" button, the dashboard itself).
    captured = {}

    class _FakeIMAP4SSL:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

        def login(self, username, password):
            pass

    monkeypatch.setattr(mail_worker.imaplib, "IMAP4_SSL", _FakeIMAP4SSL)

    mail_worker._connect({"host": "imap.example.com", "username": "a", "password": "b"})

    assert captured["timeout"] == mail_worker._IMAP_TIMEOUT_SECONDS


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
