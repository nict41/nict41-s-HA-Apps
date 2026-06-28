from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import db
import ha_sync
import main
import settings
import sync_progress
from providers import track123


def _stub_provider(monkeypatch, track_info):
    """Make Track123 look configured and return a canned lookup, without any
    network or API key - exercises run_sync_cycle's provider-dispatch logic."""
    monkeypatch.setattr(track123, "configured", lambda: True)
    monkeypatch.setattr(track123, "register", lambda numbers: None)
    monkeypatch.setattr(track123, "get_track_info", lambda numbers: track_info)

# main.app's startup event (which starts the background scheduler) is only
# triggered if TestClient is used as a context manager; using it bare here
# keeps tests synchronous and avoids a real scheduler thread running during
# the suite.
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _fresh_db():
    db.DB_PATH.unlink(missing_ok=True)
    db.init_db()
    yield


def test_dashboard_loads_when_empty():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Parcel Tracker" in resp.text


def test_static_card_js_allows_cross_origin_requests():
    # The Lovelace card is loaded by HA's frontend via a cross-origin
    # `import()` (the add-on's direct port vs. HA's own frontend port) -
    # without this header the browser silently refuses to run it, so the
    # card never registers itself even though the URL loads fine on its own.
    resp = client.get("/static/parcel-tracker-card.js")
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


def test_dashboard_route_does_not_get_the_static_cors_header():
    resp = client.get("/")
    assert "access-control-allow-origin" not in resp.headers


def test_api_parcels_allows_cross_origin_requests_for_the_lovelace_card():
    # The card fetches this endpoint from its own origin (the add-on's
    # direct port) to load a parcel's full tracking history on demand -
    # without this header the browser would refuse to let the card's script
    # read the response, the same way it would for the static JS itself.
    resp = client.get("/api/parcels")
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


def test_dashboard_response_is_not_cached():
    # A browser tab left open across an add-on rebuild would otherwise keep
    # serving its cached copy of this page (and the sync-form JS in it).
    resp = client.get("/")
    assert resp.headers["cache-control"] == "no-store"


def test_dashboard_spinner_css_respects_hidden_attribute():
    # A `.spinner { display: ... }` rule alone always wins over the
    # browser's UA `[hidden] { display: none }` rule regardless of selector
    # specificity (an author-origin rule beats a UA-origin one even at equal
    # specificity) - without this override, the spinner attribute is
    # silently ignored and it spins on every page load, sync or no sync.
    html = client.get("/").text
    assert ".spinner[hidden]" in html


def test_dashboard_checks_for_an_already_running_sync_on_load():
    # run_sync_cycle() runs on a background thread once started, independent
    # of whatever request kicked it off - a page (re)load needs to ask
    # /sync/status what's actually happening rather than just assuming the
    # page's default "Check mail now" markup reflects reality, otherwise a
    # sync started from another tab/visit (or one outliving a closed tab)
    # looks done when it's still running.
    html = client.get("/").text
    assert 'fetch("sync/status")' in html
    assert "pollUntilFinished" in html


def test_add_parcel_creates_active_parcel_and_redirects():
    resp = client.post(
        "/add",
        data={"tracking_number": "1Z999AA10123456784", "carrier_name": "UPS", "description": "Cables"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    parcels = db.list_parcels()
    assert len(parcels) == 1
    assert parcels[0]["tracking_number"] == "1Z999AA10123456784"
    assert parcels[0]["status"] == db.STATUS_ACTIVE


def test_add_parcel_blank_tracking_number_is_ignored():
    client.post("/add", data={"tracking_number": "  "})
    assert db.list_parcels() == []


def test_confirm_moves_pending_to_active():
    parcel_id = db.upsert_parcel("ABC123", "Unknown", "test", 0.5, None, db.STATUS_PENDING)
    resp = client.post("/confirm", data={"parcel_id": parcel_id}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_ACTIVE


def test_dismiss_marks_dismissed():
    parcel_id = db.upsert_parcel("ABC123", "Unknown", "test", 0.5, None, db.STATUS_PENDING)
    client.post("/dismiss", data={"parcel_id": parcel_id})
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_DISMISSED


def test_archive_marks_archived():
    parcel_id = db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    client.post("/archive", data={"parcel_id": parcel_id})
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_ARCHIVED


def test_delete_removes_parcel():
    parcel_id = db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    client.post("/delete", data={"parcel_id": parcel_id})
    assert db.get_parcel(parcel_id) is None


def test_reset_puts_parcel_back_to_pending():
    parcel_id = db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    resp = client.post("/reset", data={"parcel_id": parcel_id}, follow_redirects=False)
    assert resp.status_code == 303
    parcel = db.get_parcel(parcel_id)
    assert parcel["status"] == db.STATUS_PENDING
    assert parcel["confidence"] == 0


def test_admin_reset_all_wipes_data_when_confirmed():
    db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    resp = client.post("/admin/reset-all", data={"confirm_text": "RESET"}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.list_parcels() == []


def test_admin_reset_all_does_nothing_without_exact_confirmation():
    db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    client.post("/admin/reset-all", data={"confirm_text": "reset"})
    client.post("/admin/reset-all", data={})
    assert len(db.list_parcels()) == 1


def test_mutating_routes_sync_to_ha_immediately(monkeypatch):
    # Dashboard actions used to only reach Home Assistant on the next
    # scheduled mail-sync cycle (up to poll_interval_minutes later), since
    # ha_sync.sync() was only ever called from run_sync_cycle(). Each
    # mutating route must now call it itself so HA sensors reflect the
    # change as soon as the request completes.
    calls = []
    monkeypatch.setattr(main.ha_sync, "sync", lambda parcels: calls.append(parcels))

    parcel_id = db.upsert_parcel("ABC123", "UPS", "test", 0.5, None, db.STATUS_PENDING)
    client.post("/confirm", data={"parcel_id": parcel_id})
    client.post("/dismiss", data={"parcel_id": parcel_id})
    client.post("/archive", data={"parcel_id": parcel_id})
    client.post("/reset", data={"parcel_id": parcel_id})
    client.post("/delete", data={"parcel_id": parcel_id})
    client.post(
        "/add",
        data={"tracking_number": "1Z999AA10123456784", "carrier_name": "UPS", "description": "Cables"},
    )
    client.post("/admin/reset-all", data={"confirm_text": "RESET"})
    assert len(calls) == 7


def test_add_parcel_blank_tracking_number_does_not_sync_to_ha(monkeypatch):
    calls = []
    monkeypatch.setattr(main.ha_sync, "sync", lambda parcels: calls.append(parcels))
    client.post("/add", data={"tracking_number": "  "})
    assert calls == []


def test_admin_reset_all_does_not_sync_to_ha_without_exact_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(main.ha_sync, "sync", lambda parcels: calls.append(parcels))
    client.post("/admin/reset-all", data={"confirm_text": "reset"})
    assert calls == []


def test_export_returns_json_attachment_of_all_parcels():
    db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    resp = client.get("/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.json()
    assert len(body["parcels"]) == 1
    assert body["parcels"][0]["tracking_number"] == "ABC123"


def test_dashboard_includes_reset_button_for_active_parcel():
    db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    html = client.get("/").text
    assert "action=\"reset\"" in html


def test_sync_without_imap_configured_does_not_crash():
    resp = client.post("/sync", follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_state("last_sync_at") is not None


def test_sync_status_reports_progress():
    # sync_progress's state is process-global and other test modules exercise
    # it too, so this only checks the endpoint's shape/wiring - the actual
    # checked/total bookkeeping is covered in test_mail_worker.py and below.
    resp = client.get("/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"running", "stage", "checked", "total"}
    assert body["running"] is False


def test_sync_cycle_reports_providers_stage_progress(monkeypatch):
    # Without this, the dashboard's progress count would freeze the moment
    # the mail scan finishes, even though run_sync_cycle keeps running the
    # (potentially much slower) provider-refresh phase afterward.
    db.upsert_parcel("PROV1", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    captured = {}

    def fake_get_track_info(numbers):
        captured["progress"] = sync_progress.get()
        return {"PROV1": dict(_UNCONFIRMED_INFO, confirmed=True)}

    monkeypatch.setattr(track123, "configured", lambda: True)
    monkeypatch.setattr(track123, "register", lambda numbers: None)
    monkeypatch.setattr(track123, "get_track_info", fake_get_track_info)

    main.run_sync_cycle()

    assert captured["progress"]["stage"] == "providers"
    assert captured["progress"]["total"] == 1
    assert sync_progress.get() == {"running": False, "stage": None, "checked": 1, "total": 1}


def test_api_parcels_returns_json():
    db.upsert_parcel("ABC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    resp = client.get("/api/parcels")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["parcels"]) == 1
    assert body["parcels"][0]["tracking_number"] == "ABC123"


def test_dashboard_lists_pending_and_active_parcels():
    db.upsert_parcel("PEND123", "Unknown", "maybe a parcel", 0.5, None, db.STATUS_PENDING)
    db.upsert_parcel("ACT123", "UPS", "definitely a parcel", 0.9, None, db.STATUS_ACTIVE)
    html = client.get("/").text
    assert "PEND123" in html
    assert "ACT123" in html


def test_dashboard_offers_view_email_for_parcel_with_source_email():
    parcel_id = db.upsert_parcel(
        "PEND123",
        "Unknown",
        "maybe a parcel",
        0.5,
        "msg-1",
        db.STATUS_PENDING,
        email_sender="newsletter@example.com",
        email_subject="Your order update",
        email_body="Hello, your tracking number is PEND123 and it ships soon.",
    )
    html = client.get("/").text
    assert "View email" in html
    # The "View email" control carries the metadata used to open the viewer.
    assert f'data-parcel-id="{parcel_id}"' in html
    assert "newsletter@example.com" in html
    assert "Your order update" in html


def test_dashboard_omits_view_email_when_no_source_email():
    db.upsert_parcel("PEND123", "Unknown", "manual add", 0.5, None, db.STATUS_PENDING)
    html = client.get("/").text
    assert "View email" not in html


def test_email_route_renders_sanitized_html_with_strict_csp():
    parcel_id = db.upsert_parcel(
        "ACT123", "UPS", "x", 1.0, None, db.STATUS_ACTIVE,
        email_body="text",
        email_html='<p>Hello <b>world</b></p><script>alert(1)</script>'
                   '<img src="https://cdn/x.png">',
    )
    resp = client.get(f"/email/{parcel_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "Hello" in body and "<b>world</b>" in body
    # Script stripped by the sanitiser.
    assert "alert(1)" not in body and "<script" not in body
    # Strict CSP, remote images blocked by default.
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src" not in csp
    assert "https:" not in csp
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_email_route_opts_into_remote_images():
    parcel_id = db.upsert_parcel(
        "ACT123", "UPS", "x", 1.0, None, db.STATUS_ACTIVE,
        email_html='<img src="https://cdn/x.png">',
    )
    resp = client.get(f"/email/{parcel_id}?images=1")
    assert "img-src data: https:" in resp.headers["content-security-policy"]


def test_email_route_falls_back_to_text_only_body():
    parcel_id = db.upsert_parcel(
        "ACT123", "UPS", "x", 1.0, None, db.STATUS_ACTIVE,
        email_body="Tracking number ACT123 <not html>",
    )
    resp = client.get(f"/email/{parcel_id}")
    assert resp.status_code == 200
    # Escaped and shown as preformatted text, not interpreted as markup.
    assert "&lt;not html&gt;" in resp.text


def test_email_route_handles_parcel_without_email():
    parcel_id = db.upsert_parcel("ACT123", "UPS", "manual", 1.0, None, db.STATUS_ACTIVE)
    resp = client.get(f"/email/{parcel_id}")
    assert resp.status_code == 200
    assert "No source email" in resp.text


def test_api_and_export_do_not_leak_raw_email_html():
    db.upsert_parcel(
        "ACT123", "UPS", "x", 1.0, None, db.STATUS_ACTIVE,
        email_html="<p>secret newsletter markup</p>",
    )
    api = client.get("/api/parcels").json()
    assert "email_html" not in api["parcels"][0]
    assert api["parcels"][0]["has_email"] is True
    assert "secret newsletter markup" not in client.get("/export").text


def test_dashboard_renders_tracking_history_timeline_for_parcel_with_events():
    parcel_id = db.upsert_parcel("ACT123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    db.update_tracking_status(
        parcel_id,
        status=db.STATUS_ACTIVE,
        status_detail="In transit",
        last_event_time="2024-01-02T00:00:00Z",
        estimated_delivery=None,
        carrier_name="UPS",
        tracking_provider="track123",
        confirmed=True,
        events=[
            {"time": "2024-01-02T00:00:00Z", "detail": "Departed facility", "location": "Hong Kong"},
            {"time": "2024-01-01T00:00:00Z", "detail": "Picked up", "location": None},
        ],
    )
    html = client.get("/").text
    assert "Tracking history" in html
    assert "Departed facility" in html
    assert "Hong Kong" in html
    assert "Picked up" in html
    assert "Tracked via track123" in html
    assert "Open carrier tracker" in html


def test_dashboard_shows_empty_history_message_when_parcel_has_no_events():
    db.upsert_parcel("ACT123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    html = client.get("/").text
    assert "No tracking history available yet." in html


def test_dashboard_includes_carrier_tracking_link_for_delivered_and_archived_parcels():
    # Only the in-transit section had a "Track" link before this feature -
    # delivered/archived parcels had no way at all to reopen the carrier's
    # own tracking page, even though the number is still right there.
    delivered_id = db.upsert_parcel("DLV123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    db.update_tracking_status(
        delivered_id,
        status=db.STATUS_DELIVERED,
        status_detail="Delivered",
        last_event_time=None,
        estimated_delivery=None,
        carrier_name="UPS",
        tracking_provider=None,
        confirmed=True,
    )
    archived_id = db.upsert_parcel("ARC123", "UPS", "test", 0.9, None, db.STATUS_ACTIVE)
    db.archive_parcel(archived_id)
    html = client.get("/").text
    assert html.count("Open carrier tracker") >= 2


def test_sync_auto_confirms_pending_when_provider_recognizes_number(monkeypatch):
    parcel_id = db.upsert_parcel("RECOG1", "FedEx", "ebay item", 0.4, None, db.STATUS_PENDING)
    _stub_provider(
        monkeypatch,
        {
            "RECOG1": {
                "status": db.STATUS_ACTIVE,
                "status_detail": "In transit",
                "last_event_time": "2024-01-01T00:00:00Z",
                "estimated_delivery": "2024-01-05",
                "carrier_name": "Cainiao",
                "confirmed": True,
            }
        },
    )
    main.run_sync_cycle()
    parcel = db.get_parcel(parcel_id)
    assert parcel["status"] == db.STATUS_ACTIVE
    # The provider's carrier replaces our wrong pattern guess on confirm.
    assert parcel["carrier_name"] == "Cainiao"
    assert parcel["tracking_provider"] == "track123"


def test_sync_cycle_stores_full_event_history_from_provider(monkeypatch):
    parcel_id = db.upsert_parcel("HIST1", "Cainiao", "test", 0.9, None, db.STATUS_ACTIVE)
    events = [
        {"time": "2024-01-02T00:00:00Z", "detail": "Departed facility", "location": "Shenzhen, CN"},
        {"time": "2024-01-01T00:00:00Z", "detail": "Order received", "location": None},
    ]
    _stub_provider(
        monkeypatch,
        {
            "HIST1": {
                "status": db.STATUS_ACTIVE,
                "status_detail": "Departed facility",
                "last_event_time": "2024-01-02T00:00:00Z",
                "estimated_delivery": None,
                "carrier_name": "Cainiao",
                "confirmed": True,
                "events": events,
            }
        },
    )
    main.run_sync_cycle()
    parcel = db.get_parcel(parcel_id)
    assert parcel["tracking_history"] == events


def test_sync_keeps_pending_as_preview_when_provider_does_not_recognize_number(monkeypatch):
    parcel_id = db.upsert_parcel("UNREC1", "FedEx", "maybe", 0.4, None, db.STATUS_PENDING)
    _stub_provider(
        monkeypatch,
        {
            "UNREC1": {
                "status": db.STATUS_ACTIVE,
                "status_detail": "Pending carrier pickup",
                "last_event_time": None,
                "estimated_delivery": None,
                "carrier_name": None,
                "confirmed": False,
            }
        },
    )
    main.run_sync_cycle()
    parcel = db.get_parcel(parcel_id)
    # Unrecognised numbers stay pending for manual review, but still get a
    # status preview written to their card.
    assert parcel["status"] == db.STATUS_PENDING
    assert parcel["status_detail"] == "Pending carrier pickup"
    # This was the first check, so even an old parcel gets a fresh grace
    # period from now rather than being dismissed immediately.
    assert parcel["first_checked_at"] is not None


_UNCONFIRMED_INFO = {
    "status": db.STATUS_ACTIVE,
    "status_detail": "No data yet",
    "last_event_time": None,
    "estimated_delivery": None,
    "carrier_name": None,
    "confirmed": False,
}


def _backdate_first_checked_at(parcel_id: int, days: int) -> None:
    # Matches db.now_iso()'s tz-aware format - the real column is always
    # written that way, so backdating has to use the same shape.
    backdated = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db._connect() as conn:
        conn.execute("UPDATE parcels SET first_checked_at = ? WHERE id = ?", (backdated, parcel_id))


def test_sync_dismisses_number_never_confirmed_past_grace_period(monkeypatch):
    # Reproduces the "FedEx"/"Canada Post" false-positive reports: a number
    # our own pattern-matching guessed at, that the tracking provider has
    # never once recognised, gets auto-dismissed once it's had a fair chance.
    parcel_id = db.upsert_parcel("BOGUS1", "FedEx", "maybe", 0.4, None, db.STATUS_PENDING)
    _backdate_first_checked_at(parcel_id, days=10)
    _stub_provider(monkeypatch, {"BOGUS1": _UNCONFIRMED_INFO})

    main.run_sync_cycle()

    assert db.get_parcel(parcel_id)["status"] == db.STATUS_DISMISSED


def test_sync_does_not_dismiss_within_grace_period(monkeypatch):
    parcel_id = db.upsert_parcel("BOGUS2", "FedEx", "maybe", 0.4, None, db.STATUS_PENDING)
    _backdate_first_checked_at(parcel_id, days=1)
    _stub_provider(monkeypatch, {"BOGUS2": _UNCONFIRMED_INFO})

    main.run_sync_cycle()

    assert db.get_parcel(parcel_id)["status"] == db.STATUS_PENDING


def test_sync_does_not_dismiss_a_previously_confirmed_parcel(monkeypatch):
    # provider_confirmed is sticky - a parcel genuinely confirmed in the past
    # can't be dismissed later just because one check came back inconclusive
    # (e.g. a transient provider hiccup).
    parcel_id = db.upsert_parcel("REAL1", "Cainiao", "real parcel", 0.9, None, db.STATUS_ACTIVE)
    _backdate_first_checked_at(parcel_id, days=10)
    with db._connect() as conn:
        conn.execute("UPDATE parcels SET provider_confirmed = 1 WHERE id = ?", (parcel_id,))
    _stub_provider(monkeypatch, {"REAL1": _UNCONFIRMED_INFO})

    main.run_sync_cycle()

    assert db.get_parcel(parcel_id)["status"] != db.STATUS_DISMISSED


def test_sync_dismiss_disabled_when_days_is_zero(monkeypatch):
    parcel_id = db.upsert_parcel("BOGUS3", "FedEx", "maybe", 0.4, None, db.STATUS_PENDING)
    _backdate_first_checked_at(parcel_id, days=999)
    _stub_provider(monkeypatch, {"BOGUS3": _UNCONFIRMED_INFO})
    settings.set_many({"dismiss_unconfirmed_after_days": 0})

    main.run_sync_cycle()

    assert db.get_parcel(parcel_id)["status"] == db.STATUS_PENDING


def _recording_provider(monkeypatch, track_info):
    """Like _stub_provider, but records which tracking numbers the provider
    was actually asked about - so a test can assert a parcel was (or wasn't)
    re-queried."""
    queried = []

    def get_track_info(numbers):
        queried.extend(numbers)
        return track_info

    monkeypatch.setattr(track123, "configured", lambda: True)
    monkeypatch.setattr(track123, "register", lambda parcels: None)
    monkeypatch.setattr(track123, "get_track_info", get_track_info)
    return queried


def test_scheduled_refresh_skips_recently_checked_parcel_when_throttled(monkeypatch):
    parcel_id = db.upsert_parcel("ACT1", "UPS", "x", 1.0, None, db.STATUS_ACTIVE)
    # update_tracking_status stamps last_checked_at = now, marking it freshly checked.
    db.update_tracking_status(parcel_id, status=db.STATUS_ACTIVE, status_detail="old status",
                              last_event_time=None, estimated_delivery=None, confirmed=True)
    settings.set_many({"provider_refresh_minutes": 60})
    queried = _recording_provider(monkeypatch, {"ACT1": dict(_UNCONFIRMED_INFO, confirmed=True, status_detail="new status")})

    main.run_sync_cycle()  # scheduled run honours the throttle

    assert "ACT1" not in queried
    assert db.get_parcel(parcel_id)["status_detail"] == "old status"


def test_manual_sync_forces_refresh_even_when_throttled(monkeypatch):
    parcel_id = db.upsert_parcel("ACT1", "UPS", "x", 1.0, None, db.STATUS_ACTIVE)
    db.update_tracking_status(parcel_id, status=db.STATUS_ACTIVE, status_detail="old status",
                              last_event_time=None, estimated_delivery=None, confirmed=True)
    settings.set_many({"provider_refresh_minutes": 60})
    queried = _recording_provider(monkeypatch, {"ACT1": dict(_UNCONFIRMED_INFO, confirmed=True, status_detail="new status")})

    main.run_sync_cycle(force_refresh=True)

    assert "ACT1" in queried
    assert db.get_parcel(parcel_id)["status_detail"] == "new status"


def test_zero_throttle_refreshes_every_cycle(monkeypatch):
    parcel_id = db.upsert_parcel("ACT1", "UPS", "x", 1.0, None, db.STATUS_ACTIVE)
    db.update_tracking_status(parcel_id, status=db.STATUS_ACTIVE, status_detail="old status",
                              last_event_time=None, estimated_delivery=None, confirmed=True)
    settings.set_many({"provider_refresh_minutes": 0})  # default: no throttle
    queried = _recording_provider(monkeypatch, {"ACT1": dict(_UNCONFIRMED_INFO, confirmed=True, status_detail="new status")})

    main.run_sync_cycle()

    assert "ACT1" in queried
    assert db.get_parcel(parcel_id)["status_detail"] == "new status"


def test_refresh_route_queries_only_the_requested_parcel(monkeypatch):
    parcel_id = db.upsert_parcel("ACT1", "UPS", "x", 1.0, None, db.STATUS_ACTIVE)
    db.upsert_parcel("ACT2", "UPS", "y", 1.0, None, db.STATUS_ACTIVE)
    queried = _recording_provider(monkeypatch, {"ACT1": dict(_UNCONFIRMED_INFO, confirmed=True)})

    client.post("/refresh", data={"parcel_id": parcel_id})

    assert queried == ["ACT1"]


def test_refresh_route_updates_parcel_status_detail(monkeypatch):
    parcel_id = db.upsert_parcel("ACT1", "UPS", "x", 1.0, None, db.STATUS_ACTIVE)
    _stub_provider(monkeypatch, {"ACT1": dict(_UNCONFIRMED_INFO, confirmed=True, status_detail="new status")})

    client.post("/refresh", data={"parcel_id": parcel_id})

    assert db.get_parcel(parcel_id)["status_detail"] == "new status"


def test_refresh_route_bypasses_throttle(monkeypatch):
    parcel_id = db.upsert_parcel("ACT1", "UPS", "x", 1.0, None, db.STATUS_ACTIVE)
    # update_tracking_status stamps last_checked_at = now, marking it freshly checked.
    db.update_tracking_status(parcel_id, status=db.STATUS_ACTIVE, status_detail="old status",
                              last_event_time=None, estimated_delivery=None, confirmed=True)
    settings.set_many({"provider_refresh_minutes": 60})
    queried = _recording_provider(monkeypatch, {"ACT1": dict(_UNCONFIRMED_INFO, confirmed=True, status_detail="new status")})

    client.post("/refresh", data={"parcel_id": parcel_id})  # bypasses the throttle, unlike a scheduled cycle

    assert "ACT1" in queried
    assert db.get_parcel(parcel_id)["status_detail"] == "new status"


def test_refresh_route_redirects():
    parcel_id = db.upsert_parcel("ACT1", "UPS", "x", 1.0, None, db.STATUS_ACTIVE)
    resp = client.post("/refresh", data={"parcel_id": parcel_id}, follow_redirects=False)
    assert resp.status_code == 303


def test_refresh_route_is_noop_for_unknown_parcel_id():
    resp = client.post("/refresh", data={"parcel_id": 999999}, follow_redirects=False)
    assert resp.status_code == 303


def test_refresh_route_syncs_to_ha_immediately(monkeypatch):
    parcel_id = db.upsert_parcel("ACT1", "UPS", "x", 1.0, None, db.STATUS_ACTIVE)
    _stub_provider(monkeypatch, {"ACT1": dict(_UNCONFIRMED_INFO, confirmed=True)})
    calls = []
    monkeypatch.setattr(main.ha_sync, "sync", lambda parcels: calls.append(parcels))

    client.post("/refresh", data={"parcel_id": parcel_id})

    assert len(calls) == 1


def test_refresh_route_can_auto_dismiss_via_apply_track_info(monkeypatch):
    parcel_id = db.upsert_parcel("BOGUS1", "FedEx", "maybe", 0.4, None, db.STATUS_PENDING)
    _backdate_first_checked_at(parcel_id, days=10)
    _stub_provider(monkeypatch, {"BOGUS1": _UNCONFIRMED_INFO})

    client.post("/refresh", data={"parcel_id": parcel_id})

    assert db.get_parcel(parcel_id)["status"] == db.STATUS_DISMISSED


def test_settings_page_renders_current_values():
    settings.set_many({"poll_interval_minutes": 45, "ignore_senders": "spam.example"})
    html = client.get("/settings").text
    assert 'name="poll_interval_minutes"' in html
    assert 'value="45"' in html
    assert "spam.example" in html


def test_post_settings_saves_reschedules_and_redirects():
    resp = client.post(
        "/settings",
        data={
            "poll_interval_minutes": 20,
            "provider_refresh_minutes": 120,
            "lookback_days": 7,
            "auto_archive_after_days": 30,
            "dismiss_unconfirmed_after_days": 5,
            "trusted_senders": "shop.example",
            "ignore_senders": "",
            "allowed_senders": "amazon.com",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert settings.get_int("poll_interval_minutes") == 20
    assert settings.get_int("provider_refresh_minutes") == 120
    assert settings.get_domains("allowed_senders") == frozenset({"amazon.com"})


def test_post_settings_clamps_out_of_range_values():
    client.post(
        "/settings",
        data={
            "poll_interval_minutes": 99999,
            "provider_refresh_minutes": 0,
            "lookback_days": 14,
            "auto_archive_after_days": 14,
            "dismiss_unconfirmed_after_days": 3,
            "trusted_senders": "",
            "ignore_senders": "",
            "allowed_senders": "",
        },
        follow_redirects=False,
    )
    assert settings.get_int("poll_interval_minutes") == 1440
