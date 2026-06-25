import pytest

import db


@pytest.fixture(autouse=True)
def _fresh_db():
    db.DB_PATH.unlink(missing_ok=True)
    db.init_db()
    yield


def test_upsert_parcel_creates_new_row():
    parcel_id = db.upsert_parcel("ABC123", "UPS", "Widget", 0.9, "msg-1", db.STATUS_ACTIVE)
    parcel = db.get_parcel(parcel_id)
    assert parcel["tracking_number"] == "ABC123"
    assert parcel["status"] == db.STATUS_ACTIVE


def test_upsert_parcel_is_idempotent_on_tracking_number():
    first_id = db.upsert_parcel("ABC123", "UPS", "Widget", 0.9, "msg-1", db.STATUS_ACTIVE)
    second_id = db.upsert_parcel("ABC123", "UPS", "Widget", 0.5, "msg-2", db.STATUS_PENDING)
    assert first_id == second_id
    assert len(db.list_parcels()) == 1


def test_upsert_parcel_keeps_higher_confidence_details():
    db.upsert_parcel("ABC123", "Unknown", "first guess", 0.3, "msg-1", db.STATUS_PENDING)
    db.upsert_parcel("ABC123", "UPS", "better guess", 0.9, "msg-2", db.STATUS_ACTIVE)
    parcel = db.get_parcel(1)
    assert parcel["carrier_name"] == "UPS"
    assert parcel["description"] == "better guess"
    assert parcel["confidence"] == 0.9


def test_upsert_parcel_does_not_downgrade_lower_confidence():
    db.upsert_parcel("ABC123", "UPS", "good guess", 0.9, "msg-1", db.STATUS_ACTIVE)
    db.upsert_parcel("ABC123", "Unknown", "worse guess", 0.3, "msg-2", db.STATUS_PENDING)
    parcel = db.get_parcel(1)
    assert parcel["carrier_name"] == "UPS"
    assert parcel["description"] == "good guess"


def test_upsert_parcel_stores_email_fields():
    parcel_id = db.upsert_parcel(
        "ABC123",
        "UPS",
        "Widget",
        0.9,
        "msg-1",
        db.STATUS_ACTIVE,
        email_sender="ups@ups.com",
        email_subject="Your package shipped",
        email_body="Tracking: ABC123",
    )
    parcel = db.get_parcel(parcel_id)
    assert parcel["email_sender"] == "ups@ups.com"
    assert parcel["email_subject"] == "Your package shipped"
    assert parcel["email_body"] == "Tracking: ABC123"


def test_upsert_parcel_updates_email_fields_only_on_higher_confidence():
    db.upsert_parcel(
        "ABC123", "Unknown", "first guess", 0.3, "msg-1", db.STATUS_PENDING,
        email_sender="a@example.com", email_subject="first subject", email_body="first body",
    )
    db.upsert_parcel(
        "ABC123", "UPS", "better guess", 0.9, "msg-2", db.STATUS_ACTIVE,
        email_sender="b@example.com", email_subject="second subject", email_body="second body",
    )
    parcel = db.get_parcel(1)
    assert parcel["email_sender"] == "b@example.com"
    assert parcel["email_body"] == "second body"

    db.upsert_parcel(
        "ABC123", "Unknown", "worse guess", 0.1, "msg-3", db.STATUS_PENDING,
        email_sender="c@example.com", email_subject="third subject", email_body="third body",
    )
    parcel = db.get_parcel(1)
    assert parcel["email_sender"] == "b@example.com"


def test_is_processed_and_mark_processed():
    assert not db.is_processed("msg-1")
    db.mark_processed("msg-1")
    assert db.is_processed("msg-1")


def test_list_parcels_filters_by_status():
    db.upsert_parcel("A", "UPS", "a", 0.9, None, db.STATUS_ACTIVE)
    db.upsert_parcel("B", "USPS", "b", 0.9, None, db.STATUS_PENDING)
    pending_only = db.list_parcels([db.STATUS_PENDING])
    assert len(pending_only) == 1
    assert pending_only[0]["tracking_number"] == "B"


def test_confirm_dismiss_archive_lifecycle():
    parcel_id = db.upsert_parcel("A", "UPS", "a", 0.5, None, db.STATUS_PENDING)
    db.confirm_parcel(parcel_id, carrier_name="UPS")
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_ACTIVE

    db.archive_parcel(parcel_id)
    parcel = db.get_parcel(parcel_id)
    assert parcel["status"] == db.STATUS_ARCHIVED
    assert parcel["archived_at"] is not None


def test_dismiss_sets_dismissed_status():
    parcel_id = db.upsert_parcel("A", "UPS", "a", 0.5, None, db.STATUS_PENDING)
    db.dismiss_parcel(parcel_id)
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_DISMISSED


def test_delete_parcel_removes_row():
    parcel_id = db.upsert_parcel("A", "UPS", "a", 0.5, None, db.STATUS_PENDING)
    db.delete_parcel(parcel_id)
    assert db.get_parcel(parcel_id) is None


def test_parcels_needing_refresh_returns_pending_active_and_exception_only():
    db.upsert_parcel("A", "UPS", "a", 0.9, None, db.STATUS_ACTIVE)
    db.upsert_parcel("B", "USPS", "b", 0.9, None, db.STATUS_EXCEPTION)
    db.upsert_parcel("C", "DHL", "c", 0.9, None, db.STATUS_DELIVERED)
    db.upsert_parcel("D", "Unknown", "d", 0.4, None, db.STATUS_PENDING)
    refresh = {p["tracking_number"] for p in db.parcels_needing_refresh()}
    assert refresh == {"A", "B", "D"}


def test_update_tracking_status_sets_delivered_at_once():
    parcel_id = db.upsert_parcel("A", "UPS", "a", 0.9, None, db.STATUS_ACTIVE)
    db.update_tracking_status(parcel_id, db.STATUS_DELIVERED, "Delivered", "2024-01-01T00:00:00Z", None)
    first = db.get_parcel(parcel_id)
    assert first["delivered_at"] is not None

    db.update_tracking_status(parcel_id, db.STATUS_DELIVERED, "Delivered again", "2024-01-02T00:00:00Z", None)
    second = db.get_parcel(parcel_id)
    assert second["delivered_at"] == first["delivered_at"]


def test_update_tracking_status_with_none_status_leaves_lifecycle_status_untouched():
    parcel_id = db.upsert_parcel("A", "Unknown", "a", 0.4, None, db.STATUS_PENDING)
    db.update_tracking_status(
        parcel_id,
        status=None,
        status_detail="In transit",
        last_event_time="2024-01-01T00:00:00Z",
        estimated_delivery="2024-01-05",
    )
    parcel = db.get_parcel(parcel_id)
    assert parcel["status"] == db.STATUS_PENDING
    assert parcel["status_detail"] == "In transit"


def test_update_tracking_status_sets_carrier_name_and_tracking_provider():
    parcel_id = db.upsert_parcel("A", "FedEx", "a", 0.4, None, db.STATUS_PENDING)
    db.update_tracking_status(
        parcel_id,
        status=None,
        status_detail="In transit",
        last_event_time=None,
        estimated_delivery=None,
        carrier_name="Cainiao",
        tracking_provider="track123",
    )
    parcel = db.get_parcel(parcel_id)
    assert parcel["carrier_name"] == "Cainiao"
    assert parcel["tracking_provider"] == "track123"


def test_update_tracking_status_stamps_first_checked_at_once():
    parcel_id = db.upsert_parcel("A", "Unknown", "a", 0.4, None, db.STATUS_PENDING)
    assert db.get_parcel(parcel_id)["first_checked_at"] is None

    db.update_tracking_status(parcel_id, status=None, status_detail=None, last_event_time=None, estimated_delivery=None)
    first = db.get_parcel(parcel_id)["first_checked_at"]
    assert first is not None

    db.update_tracking_status(parcel_id, status=None, status_detail=None, last_event_time=None, estimated_delivery=None)
    assert db.get_parcel(parcel_id)["first_checked_at"] == first


def test_update_tracking_status_confirmed_is_sticky():
    parcel_id = db.upsert_parcel("A", "Unknown", "a", 0.4, None, db.STATUS_PENDING)
    assert db.get_parcel(parcel_id)["provider_confirmed"] == 0

    db.update_tracking_status(
        parcel_id, status=None, status_detail=None, last_event_time=None, estimated_delivery=None, confirmed=True
    )
    assert db.get_parcel(parcel_id)["provider_confirmed"] == 1

    # A later inconclusive check shouldn't undo a confirmation already earned.
    db.update_tracking_status(
        parcel_id, status=None, status_detail=None, last_event_time=None, estimated_delivery=None, confirmed=False
    )
    assert db.get_parcel(parcel_id)["provider_confirmed"] == 1


def test_update_tracking_status_does_not_blank_known_good_fields_on_empty_response():
    parcel_id = db.upsert_parcel("A", "Cainiao", "a", 0.9, None, db.STATUS_ACTIVE)
    db.update_tracking_status(
        parcel_id, status=None, status_detail="In transit", last_event_time="2024-01-01T00:00:00Z",
        estimated_delivery="2024-01-05",
    )
    # A later check that comes back with no fresh data (e.g. a momentary gap
    # in the provider's response) shouldn't erase the status text we already
    # had - it should leave the parcel showing its last-known-good status
    # rather than reverting to "no status yet".
    db.update_tracking_status(parcel_id, status=None, status_detail=None, last_event_time=None, estimated_delivery=None)
    parcel = db.get_parcel(parcel_id)
    assert parcel["status_detail"] == "In transit"
    assert parcel["last_event_time"] == "2024-01-01T00:00:00Z"
    assert parcel["estimated_delivery"] == "2024-01-05"


def test_update_tracking_status_overwrites_with_fresh_non_empty_data():
    parcel_id = db.upsert_parcel("A", "Cainiao", "a", 0.9, None, db.STATUS_ACTIVE)
    db.update_tracking_status(
        parcel_id, status=None, status_detail="In transit", last_event_time="2024-01-01T00:00:00Z",
        estimated_delivery="2024-01-05",
    )
    db.update_tracking_status(
        parcel_id, status=None, status_detail="Out for delivery", last_event_time="2024-01-06T00:00:00Z",
        estimated_delivery="2024-01-06",
    )
    parcel = db.get_parcel(parcel_id)
    assert parcel["status_detail"] == "Out for delivery"
    assert parcel["last_event_time"] == "2024-01-06T00:00:00Z"
    assert parcel["estimated_delivery"] == "2024-01-06"


def test_auto_archive_delivered_respects_days_and_zero_disables():
    parcel_id = db.upsert_parcel("A", "UPS", "a", 0.9, None, db.STATUS_DELIVERED)
    with db._connect() as conn:
        conn.execute(
            "UPDATE parcels SET delivered_at = datetime('now', '-30 days') WHERE id = ?",
            (parcel_id,),
        )

    assert db.auto_archive_delivered(0) == 0
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_DELIVERED

    assert db.auto_archive_delivered(14) == 1
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_ARCHIVED


def test_reset_parcel_clears_processed_message_and_state():
    parcel_id = db.upsert_parcel(
        "A", "UPS", "a", 0.9, "msg-1", db.STATUS_ACTIVE,
        email_sender="ups@ups.com", email_subject="Shipped", email_body="body",
    )
    db.mark_processed("msg-1")
    db.update_tracking_status(
        parcel_id, status=db.STATUS_EXCEPTION, status_detail="Delayed",
        last_event_time="2024-01-01T00:00:00Z", estimated_delivery="2024-01-05", confirmed=True,
    )

    db.reset_parcel(parcel_id)

    assert not db.is_processed("msg-1")
    parcel = db.get_parcel(parcel_id)
    assert parcel["status"] == db.STATUS_PENDING
    assert parcel["status_detail"] is None
    assert parcel["last_event_time"] is None
    assert parcel["estimated_delivery"] is None
    assert parcel["confidence"] == 0
    assert parcel["provider_confirmed"] == 0
    assert parcel["first_checked_at"] is None
    assert parcel["tracking_provider"] is None
    # The email itself stays - only the lifecycle/tracking state resets.
    assert parcel["email_body"] == "body"


def test_reset_parcel_without_source_message_does_not_error():
    parcel_id = db.upsert_parcel("A", "UPS", "a", 1.0, None, db.STATUS_ACTIVE)
    db.reset_parcel(parcel_id)
    assert db.get_parcel(parcel_id)["status"] == db.STATUS_PENDING


def test_reset_parcel_clears_delivered_and_archived_at():
    parcel_id = db.upsert_parcel("A", "UPS", "a", 0.9, None, db.STATUS_DELIVERED)
    db.archive_parcel(parcel_id)
    db.reset_parcel(parcel_id)
    parcel = db.get_parcel(parcel_id)
    assert parcel["delivered_at"] is None
    assert parcel["archived_at"] is None


def test_reset_parcel_missing_id_is_noop():
    db.reset_parcel(999)  # should not raise


def test_reset_all_data_clears_parcels_processed_messages_and_state():
    db.upsert_parcel("A", "UPS", "a", 0.9, "msg-1", db.STATUS_ACTIVE)
    db.mark_processed("msg-1")
    db.set_state("last_sync_at", "2024-01-01T00:00:00Z")

    db.reset_all_data()

    assert db.list_parcels() == []
    assert not db.is_processed("msg-1")
    assert db.get_state("last_sync_at") is None


def test_get_state_and_set_state_roundtrip():
    assert db.get_state("missing", "default") == "default"
    db.set_state("last_sync_at", "2024-01-01T00:00:00Z")
    assert db.get_state("last_sync_at") == "2024-01-01T00:00:00Z"
    db.set_state("last_sync_at", "2024-01-02T00:00:00Z")
    assert db.get_state("last_sync_at") == "2024-01-02T00:00:00Z"
