import pytest

from providers import seventeentrack, track123


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(seventeentrack, "API_KEY", "test-key")
    monkeypatch.setattr(track123, "API_KEY", "test-key")
    yield


def test_seventeentrack_confirms_a_number_the_provider_recognizes(monkeypatch):
    monkeypatch.setattr(
        seventeentrack,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": [
                    {
                        "number": "REAL1",
                        "track_info": {
                            "latest_status": {"status": "InTransit"},
                            "latest_event": {"description": "Departed facility", "time_iso": "2024-01-01T00:00:00Z"},
                            "tracking": {"providers": [{"provider": {"name": "Cainiao"}}]},
                        },
                    }
                ]
            }
        },
    )

    results = seventeentrack.get_track_info(["REAL1"])

    assert results["REAL1"]["confirmed"] is True
    assert results["REAL1"]["carrier_name"] == "Cainiao"


def test_seventeentrack_does_not_confirm_a_carrier_guess_with_no_actual_event(monkeypatch):
    # 17track can match a number to a carrier from its shape alone (e.g. a
    # phone number that happens to fit a carrier's number-length pattern)
    # with no real tracking data behind it - that bare guess isn't enough to
    # call the number confirmed, since that's exactly the kind of false
    # positive auto-dismiss exists to clean up.
    monkeypatch.setattr(
        seventeentrack,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": [
                    {
                        "number": "FAKE1",
                        "track_info": {
                            "latest_status": {"status": "InTransit"},
                            "tracking": {"providers": [{"provider": {"name": "Canada Post"}}]},
                        },
                    }
                ]
            }
        },
    )

    results = seventeentrack.get_track_info(["FAKE1"])

    assert results["FAKE1"]["confirmed"] is False


def test_seventeentrack_marks_unrecognized_number_as_unconfirmed_rather_than_omitting_it(monkeypatch):
    # A successful request that just doesn't recognize this number must be
    # distinguishable from a failed request - this is what lets the app tell
    # "never a real tracking number" apart from "couldn't check right now".
    monkeypatch.setattr(seventeentrack, "_post", lambda path, payload: {"data": {"accepted": []}})

    results = seventeentrack.get_track_info(["BOGUS1"])

    assert "BOGUS1" in results
    assert results["BOGUS1"]["confirmed"] is False
    assert results["BOGUS1"]["carrier_name"] is None


def test_seventeentrack_omits_numbers_entirely_when_request_fails(monkeypatch):
    monkeypatch.setattr(seventeentrack, "_post", lambda path, payload: None)

    results = seventeentrack.get_track_info(["ANY1"])

    assert results == {}


def test_track123_confirms_a_number_the_provider_recognizes(monkeypatch):
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": "REAL2",
                            "transitStatus": "IN_TRANSIT",
                            "localLogisticsInfo": {
                                "courierNameEN": "Cainiao",
                                "trackingDetails": [
                                    {"eventDetail": "Departed facility", "eventTime": "2024-01-01T00:00:00Z"}
                                ],
                            },
                        }
                    ]
                }
            }
        },
    )

    results = track123.get_track_info(["REAL2"])

    assert results["REAL2"]["confirmed"] is True
    assert results["REAL2"]["carrier_name"] == "Cainiao"


def test_track123_does_not_confirm_a_carrier_guess_with_no_actual_event(monkeypatch):
    # Track123 can guess a courier from a number's shape alone (e.g. a phone
    # number that happens to fit a carrier's number-length pattern) with no
    # real tracking data behind it - that bare guess isn't enough to call
    # the number confirmed, since that's exactly the kind of false positive
    # auto-dismiss exists to clean up.
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": "FAKE2",
                            "transitStatus": "IN_TRANSIT",
                            "localLogisticsInfo": {"courierNameEN": "DHL"},
                        }
                    ]
                }
            }
        },
    )

    results = track123.get_track_info(["FAKE2"])

    assert results["FAKE2"]["confirmed"] is False


def test_track123_prefers_last_mile_events_once_handed_off(monkeypatch):
    # A cross-border parcel (e.g. Cainiao) hands off to a local last-mile
    # courier once it reaches the destination country - lastMileInfo's
    # events are the freshest ones at that point, even though
    # localLogisticsInfo still has its (now-stale) international-leg event.
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": "JJD1",
                            "transitStatus": "IN_TRANSIT",
                            "localLogisticsInfo": {
                                "courierNameEN": "Cainiao",
                                "trackingDetails": [
                                    {"eventDetail": "Departed facility", "eventTime": "2024-01-01T00:00:00Z"}
                                ],
                            },
                            "lastMileInfo": {
                                "openApiWayBillInfo": {
                                    "courierNameEN": "Royal Mail",
                                    "trackingDetails": [
                                        {"eventDetail": "Out for delivery", "eventTime": "2024-01-05T00:00:00Z"}
                                    ],
                                }
                            },
                        }
                    ]
                }
            }
        },
    )

    results = track123.get_track_info(["JJD1"])

    assert results["JJD1"]["status_detail"] == "Out for delivery"
    assert results["JJD1"]["carrier_name"] == "Royal Mail"


def test_track123_falls_back_to_international_leg_before_last_mile_handoff(monkeypatch):
    # lastMileInfo appears in the response shape even before the handoff
    # happens, but with no trackingDetails yet - localLogisticsInfo is
    # still the freshest source until that handoff actually occurs.
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": "JJD2",
                            "transitStatus": "IN_TRANSIT",
                            "localLogisticsInfo": {
                                "courierNameEN": "Cainiao",
                                "trackingDetails": [
                                    {"eventDetail": "Departed facility", "eventTime": "2024-01-01T00:00:00Z"}
                                ],
                            },
                            "lastMileInfo": {"openApiWayBillInfo": {"trackingDetails": []}},
                        }
                    ]
                }
            }
        },
    )

    results = track123.get_track_info(["JJD2"])

    assert results["JJD2"]["status_detail"] == "Departed facility"
    assert results["JJD2"]["carrier_name"] == "Cainiao"


def test_track123_marks_unrecognized_number_as_unconfirmed_rather_than_omitting_it(monkeypatch):
    monkeypatch.setattr(track123, "_post", lambda path, payload: {"data": {"accepted": {"content": []}}})

    results = track123.get_track_info(["BOGUS2"])

    assert "BOGUS2" in results
    assert results["BOGUS2"]["confirmed"] is False
    assert results["BOGUS2"]["carrier_name"] is None


def test_track123_omits_numbers_entirely_when_request_fails(monkeypatch):
    monkeypatch.setattr(track123, "_post", lambda path, payload: None)

    results = track123.get_track_info(["ANY2"])

    assert results == {}


def test_track123_surfaces_rejection_reason_as_status_detail(monkeypatch):
    # A number Track123 explicitly rejects (rather than just having no data
    # for) gets that reason shown instead of silently staying "No status
    # yet" with no indication of why it's stuck.
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": {"content": []},
                "rejected": [
                    {"trackNo": "REJ1", "error": {"code": "A0400", "msg": "The order number has been imported"}}
                ],
            }
        },
    )

    results = track123.get_track_info(["REJ1"])

    assert results["REJ1"]["confirmed"] is False
    assert results["REJ1"]["status_detail"] == "Track123: A0400: The order number has been imported"


def test_track123_register_logs_rejection_reason(monkeypatch, capsys):
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: {
            "data": {"rejected": [{"trackNo": "REJ2", "error": {"code": "A0400", "msg": "quota exceeded"}}]}
        },
    )

    track123.register([("REJ2", None)])

    out = capsys.readouterr().out
    assert "REJ2" in out
    assert "quota exceeded" in out


def test_track123_register_never_includes_courier_code(monkeypatch):
    # carrier_name is accepted for call-site symmetry with 17track but never
    # turned into a courierCode override - including for Cainiao/AliExpress
    # Standard Shipping numbers (LP/JJD-prefixed), where guessing cainiao vs.
    # aliexpress ourselves is a worse bet than Track123's own auto-detect.
    captured_payload = []
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: captured_payload.append(payload) or None,
    )

    track123.register([("JJD3", "Cainiao / AliExpress Standard Shipping"), ("ANY3", "UPS")])

    assert captured_payload == [[{"trackNo": "JJD3"}, {"trackNo": "ANY3"}]]
