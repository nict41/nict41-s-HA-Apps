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


def test_seventeentrack_returns_full_event_history_not_just_latest(monkeypatch):
    # latest_event only ever surfaces one entry - the full journey lives in
    # providers[].events, newest first, and needs to be exposed separately
    # for a card's expanded history view to show more than just that one.
    monkeypatch.setattr(
        seventeentrack,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": [
                    {
                        "number": "REAL3",
                        "track_info": {
                            "latest_status": {"status": "InTransit"},
                            "latest_event": {"description": "Departed facility", "time_iso": "2024-01-02T00:00:00Z"},
                            "tracking": {
                                "providers": [
                                    {
                                        "provider": {"name": "Cainiao"},
                                        "events": [
                                            {
                                                "time_iso": "2024-01-02T00:00:00Z",
                                                "description": "Departed facility",
                                                "location": "Shenzhen, CN",
                                            },
                                            {
                                                "time_iso": "2024-01-01T00:00:00Z",
                                                "description": "Order received",
                                                "location": None,
                                            },
                                        ],
                                    }
                                ]
                            },
                        },
                    }
                ]
            }
        },
    )

    results = seventeentrack.get_track_info(["REAL3"])

    assert results["REAL3"]["events"] == [
        {"time": "2024-01-02T00:00:00Z", "detail": "Departed facility", "location": "Shenzhen, CN"},
        {"time": "2024-01-01T00:00:00Z", "detail": "Order received", "location": None},
    ]


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


def test_track123_returns_full_event_history_not_just_latest(monkeypatch):
    # status_detail/last_event_time only ever surface the single latest
    # entry - the full journey (with per-event location) needs to be
    # exposed separately for a card's expanded history view.
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": "REAL4",
                            "transitStatus": "IN_TRANSIT",
                            "localLogisticsInfo": {
                                "courierNameEN": "Cainiao",
                                "trackingDetails": [
                                    {
                                        "eventDetail": "Departed facility",
                                        "eventTime": "2024-01-02T00:00:00Z",
                                        "address": "Shenzhen, CN",
                                    },
                                    {
                                        "eventDetail": "Order received",
                                        "eventTime": "2024-01-01T00:00:00Z",
                                    },
                                ],
                            },
                        }
                    ]
                }
            }
        },
    )

    results = track123.get_track_info(["REAL4"])

    assert results["REAL4"]["events"] == [
        {"time": "2024-01-02T00:00:00Z", "detail": "Departed facility", "location": "Shenzhen, CN"},
        {"time": "2024-01-01T00:00:00Z", "detail": "Order received", "location": None},
    ]


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


def test_track123_falls_back_to_instant_tracking_when_accepted_but_no_events(monkeypatch):
    # track/query can accept a number (courier detected) well before its own
    # background polling of the carrier has caught up with real movement
    # events - track/query-realtime queries the carrier live instead, the
    # same way Track123's own web tracker does, so a number that tracks fine
    # there shouldn't be stuck showing "no status yet" in our app too.
    def fake_post(path, payload):
        if path == "tk/v2.1/track/query":
            return {
                "data": {
                    "accepted": {
                        "content": [
                            {
                                "trackNo": "JJD9",
                                "transitStatus": "IN_TRANSIT",
                                "localLogisticsInfo": {"courierCode": "cainiao", "courierNameEN": "Cainiao"},
                            }
                        ]
                    }
                }
            }
        if path == "tk/v2.1/track/query-realtime":
            assert payload == {"trackNo": "JJD9", "courierCode": "cainiao"}
            return {
                "data": {
                    "accepted": {
                        "trackNo": "JJD9",
                        "localLogisticsInfo": {
                            "courierNameEN": "Cainiao",
                            "trackingDetails": [
                                {"eventDetail": "Departed facility", "eventTime": "2024-01-01T00:00:00Z"}
                            ],
                        },
                    }
                }
            }
        raise AssertionError(f"unexpected path {path!r}")

    monkeypatch.setattr(track123, "_post", fake_post)
    monkeypatch.setattr(track123.time, "sleep", lambda _seconds: None)

    results = track123.get_track_info(["JJD9"])

    assert results["JJD9"]["confirmed"] is True
    assert results["JJD9"]["status_detail"] == "Departed facility"
    assert results["JJD9"]["carrier_name"] == "Cainiao"


def test_track123_instant_tracking_fallback_still_empty_stays_unconfirmed(monkeypatch):
    def fake_post(path, payload):
        if path == "tk/v2.1/track/query":
            return {
                "data": {
                    "accepted": {
                        "content": [
                            {
                                "trackNo": "JJD10",
                                "transitStatus": "IN_TRANSIT",
                                "localLogisticsInfo": {"courierCode": "cainiao", "courierNameEN": "Cainiao"},
                            }
                        ]
                    }
                }
            }
        if path == "tk/v2.1/track/query-realtime":
            return {"data": {"accepted": {"trackNo": "JJD10", "localLogisticsInfo": {"courierNameEN": "Cainiao"}}}}
        raise AssertionError(f"unexpected path {path!r}")

    monkeypatch.setattr(track123, "_post", fake_post)
    monkeypatch.setattr(track123.time, "sleep", lambda _seconds: None)

    results = track123.get_track_info(["JJD10"])

    assert results["JJD10"]["confirmed"] is False
    assert results["JJD10"]["status_detail"] is None


def test_track123_does_not_call_instant_tracking_when_already_has_events(monkeypatch):
    # The instant endpoint is documented as quota-consuming and unsuited to
    # routine/bulk use - it must only be tried for numbers actually stuck
    # without events, not for every lookup.
    paths_called = []

    def fake_post(path, payload):
        paths_called.append(path)
        return {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": "JJD11",
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
        }

    monkeypatch.setattr(track123, "_post", fake_post)

    track123.get_track_info(["JJD11"])

    assert paths_called == ["tk/v2.1/track/query"]


def test_track123_register_includes_courier_code_for_known_carrier(monkeypatch):
    # Auto-detect can reject a Cainiao/AliExpress number outright at
    # registration, even though Track123's own web tracker resolves it fine
    # once a courier is known - so its registration payload should include
    # the courier code, unlike a carrier we have no mapping for.
    captured_payload = []
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: captured_payload.append(payload) or None,
    )

    track123.register([("JJD3", "Cainiao / AliExpress Standard Shipping"), ("ANY3", "UPS")])

    assert captured_payload == [[{"trackNo": "JJD3", "courierCode": "cainiao"}, {"trackNo": "ANY3"}]]


def test_track123_register_includes_courier_code_for_evri(monkeypatch):
    # Evri numbers can be rejected outright at registration (A0400: trackNo
    # not registered) without an explicit courier code, even though
    # Track123's own web tracker resolves the same number fine.
    captured_payload = []
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: captured_payload.append(payload) or None,
    )

    track123.register([("H06R4A0176637302", "Evri")])

    assert captured_payload == [[{"trackNo": "H06R4A0176637302", "courierCode": "evri"}]]


def test_track123_register_courier_code_lookup_is_case_insensitive(monkeypatch):
    # carrier_name can come from the dashboard's freeform manual-add field,
    # so the lookup can't depend on exact casing matching our own detectors.
    captured_payload = []
    monkeypatch.setattr(
        track123,
        "_post",
        lambda path, payload: captured_payload.append(payload) or None,
    )

    track123.register([("H06R4A0176637302", "evri")])

    assert captured_payload == [[{"trackNo": "H06R4A0176637302", "courierCode": "evri"}]]
