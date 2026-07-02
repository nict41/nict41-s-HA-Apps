import json
import urllib.request

import pytest

from providers import seventeentrack, track123


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(seventeentrack, "API_KEY", "test-key")
    monkeypatch.setattr(track123, "API_KEY", "test-key")
    yield


@pytest.fixture
def _clear_raw_exchanges():
    seventeentrack._raw_exchanges.clear()
    track123._raw_exchanges.clear()
    yield
    seventeentrack._raw_exchanges.clear()
    track123._raw_exchanges.clear()


class _FakeHTTPResponse:
    """Minimal stand-in for the context-managed object urlopen() returns -
    just enough for _post() to read a status code and a JSON body."""

    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _patch_urlopen(monkeypatch, responder):
    """responder(req) -> (status, body). Patches the real urlopen() (rather
    than _post() itself, like the tests above) so _post()'s capture side
    effect - populating _last_exchange - actually runs."""

    def fake_urlopen(req, timeout=15):
        status, body = responder(req)
        return _FakeHTTPResponse(status, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


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


def test_seventeentrack_redacts_api_key_in_captured_request(monkeypatch, _clear_raw_exchanges):
    monkeypatch.setattr(seventeentrack.time, "sleep", lambda _s: None)
    _patch_urlopen(monkeypatch, lambda req: (200, {"data": {}}))

    seventeentrack.register([("REDACT1", None)])

    exchange = seventeentrack.get_raw_exchange("REDACT1")
    assert exchange["register"]["request"]["headers"]["17token"] == "<redacted>"


def test_seventeentrack_attributes_raw_exchange_to_only_the_numbers_in_its_chunk(monkeypatch, _clear_raw_exchanges):
    monkeypatch.setattr(seventeentrack.time, "sleep", lambda _s: None)
    numbers = [f"N{i:03d}" for i in range(1, 42)]  # 41 forces 2 chunks (_MAX_NUMBERS_PER_REQUEST=40)
    _patch_urlopen(monkeypatch, lambda req: (200, {"data": {"accepted": []}}))

    seventeentrack.get_track_info(numbers)

    first_body = seventeentrack.get_raw_exchange(numbers[0])["get_track_info"]["request"]["body"]
    second_body = seventeentrack.get_raw_exchange(numbers[-1])["get_track_info"]["request"]["body"]
    first_numbers = {entry["number"] for entry in first_body}
    second_numbers = {entry["number"] for entry in second_body}

    assert numbers[0] in first_numbers
    assert numbers[-1] not in first_numbers
    assert numbers[-1] in second_numbers
    assert numbers[0] not in second_numbers


def test_seventeentrack_register_and_get_track_info_captured_independently(monkeypatch, _clear_raw_exchanges):
    monkeypatch.setattr(seventeentrack.time, "sleep", lambda _s: None)
    _patch_urlopen(monkeypatch, lambda req: (200, {"data": {}}))
    seventeentrack.register([("IND1", None)])

    exchange = seventeentrack.get_raw_exchange("IND1")
    assert "register" in exchange
    assert "get_track_info" not in exchange

    _patch_urlopen(monkeypatch, lambda req: (200, {"data": {"accepted": []}}))
    seventeentrack.get_track_info(["IND1"])

    exchange = seventeentrack.get_raw_exchange("IND1")
    assert "register" in exchange
    assert "get_track_info" in exchange


def test_seventeentrack_get_raw_exchange_returns_none_for_unknown_number():
    assert seventeentrack.get_raw_exchange("NEVER-SEEN-17TRACK") is None


def test_track123_redacts_api_secret_in_captured_request(monkeypatch, _clear_raw_exchanges):
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)
    _patch_urlopen(monkeypatch, lambda req: (200, {"data": {}}))

    track123.register([("REDACT2", None)])

    exchange = track123.get_raw_exchange("REDACT2")
    assert exchange["register"]["request"]["headers"]["Track123-Api-Secret"] == "<redacted>"


def test_track123_attributes_raw_exchange_to_only_the_numbers_in_its_chunk(monkeypatch, _clear_raw_exchanges):
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)
    numbers = [f"T{i:03d}" for i in range(1, 42)]  # 41 forces 2 chunks (_MAX_NUMBERS_PER_REQUEST=40)
    _patch_urlopen(monkeypatch, lambda req: (200, {"data": {"accepted": {"content": []}}}))

    track123.get_track_info(numbers)

    first_body = track123.get_raw_exchange(numbers[0])["get_track_info"]["request"]["body"]
    second_body = track123.get_raw_exchange(numbers[-1])["get_track_info"]["request"]["body"]
    first_numbers = {entry["trackNo"] for entry in first_body["trackNoInfos"]}
    second_numbers = {entry["trackNo"] for entry in second_body["trackNoInfos"]}

    assert numbers[0] in first_numbers
    assert numbers[-1] not in first_numbers
    assert numbers[-1] in second_numbers
    assert numbers[0] not in second_numbers


def test_track123_register_and_get_track_info_captured_independently(monkeypatch, _clear_raw_exchanges):
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)
    _patch_urlopen(monkeypatch, lambda req: (200, {"data": {}}))
    track123.register([("IND2", None)])

    exchange = track123.get_raw_exchange("IND2")
    assert "register" in exchange
    assert "get_track_info" not in exchange

    _patch_urlopen(monkeypatch, lambda req: (200, {"data": {"accepted": {"content": []}}}))
    track123.get_track_info(["IND2"])

    exchange = track123.get_raw_exchange("IND2")
    assert "register" in exchange
    assert "get_track_info" in exchange


def test_track123_get_raw_exchange_returns_none_for_unknown_number():
    assert track123.get_raw_exchange("NEVER-SEEN-TRACK123") is None


def test_track123_captures_instant_tracking_exchange_separately_from_get_track_info(monkeypatch, _clear_raw_exchanges):
    # The instant-tracking fallback is a distinct HTTP call from the batch
    # get_track_info() call that triggered it - both need to be inspectable
    # on their own, not have one silently overwrite the other.
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)

    def responder(req):
        if req.full_url.endswith("track/query-realtime"):
            return 200, {
                "data": {
                    "accepted": {
                        "trackNo": "INSTANT1",
                        "localLogisticsInfo": {
                            "courierNameEN": "Cainiao",
                            "trackingDetails": [
                                {"eventDetail": "Departed facility", "eventTime": "2024-01-01T00:00:00Z"}
                            ],
                        },
                    }
                }
            }
        return 200, {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": "INSTANT1",
                            "transitStatus": "IN_TRANSIT",
                            "localLogisticsInfo": {"courierCode": "cainiao", "courierNameEN": "Cainiao"},
                        }
                    ]
                }
            }
        }

    _patch_urlopen(monkeypatch, responder)

    track123.get_track_info(["INSTANT1"])

    exchange = track123.get_raw_exchange("INSTANT1")
    assert "get_track_info" in exchange
    assert "get_track_info_instant" in exchange
    assert exchange["get_track_info"]["request"]["body"] != exchange["get_track_info_instant"]["request"]["body"]


@pytest.fixture
def _clear_quota_state():
    """Resets the per-process quota-efficiency state (instant-tracking
    backoff, known-registered sets) around tests that exercise it."""
    track123._instant_attempts.clear()
    track123._known_registered.clear()
    seventeentrack._known_registered.clear()
    yield
    track123._instant_attempts.clear()
    track123._known_registered.clear()
    seventeentrack._known_registered.clear()


def _stuck_track123_responder(number, realtime_paths):
    """A fake _post where track/query always accepts `number` with a detected
    courier but no events (the stuck state that triggers the instant
    fallback), and track/query-realtime is also always empty - recording each
    realtime call into `realtime_paths`."""

    def fake_post(path, payload):
        if path == "tk/v2.1/track/query-realtime":
            realtime_paths.append(path)
            return {"data": {"accepted": {"trackNo": number, "localLogisticsInfo": {"courierNameEN": "Cainiao"}}}}
        return {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": number,
                            "transitStatus": "IN_TRANSIT",
                            "localLogisticsInfo": {"courierCode": "cainiao", "courierNameEN": "Cainiao"},
                        }
                    ]
                }
            }
        }

    return fake_post


def test_track123_instant_fallback_not_retried_within_cooldown(monkeypatch, _clear_quota_state):
    # Each instant call costs one quota credit, so a number stuck
    # accepted-but-empty must not re-query the carrier live on every sync
    # cycle - only the first cycle of the day gets to try.
    realtime_calls = []
    monkeypatch.setattr(track123, "_post", _stuck_track123_responder("STUCK1", realtime_calls))
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)

    track123.get_track_info(["STUCK1"])
    track123.get_track_info(["STUCK1"])

    assert len(realtime_calls) == 1
    # The stuck number still gets its normal (unconfirmed) batch-query entry.
    assert track123.get_track_info(["STUCK1"])["STUCK1"]["confirmed"] is False


def test_track123_instant_fallback_retries_after_cooldown(monkeypatch, _clear_quota_state):
    realtime_calls = []
    monkeypatch.setattr(track123, "_post", _stuck_track123_responder("STUCK2", realtime_calls))
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)

    track123.get_track_info(["STUCK2"])
    # Age the attempt past the cooldown window, as if a day had passed.
    track123._instant_attempts["STUCK2"]["last"] -= track123._INSTANT_RETRY_SECONDS + 1
    track123.get_track_info(["STUCK2"])

    assert len(realtime_calls) == 2


def test_track123_instant_fallback_gives_up_after_repeated_misses(monkeypatch, _clear_quota_state):
    # After _INSTANT_MAX_MISSES consecutive empty answers, the number stops
    # getting instant lookups even once the cooldown has expired - Track123's
    # own background polling (served by the free batch query) is the only
    # thing likely to ever have news for it.
    realtime_calls = []
    monkeypatch.setattr(track123, "_post", _stuck_track123_responder("STUCK3", realtime_calls))
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)

    for _ in range(track123._INSTANT_MAX_MISSES + 2):
        track123.get_track_info(["STUCK3"])
        track123._instant_attempts["STUCK3"]["last"] -= track123._INSTANT_RETRY_SECONDS + 1

    assert len(realtime_calls) == track123._INSTANT_MAX_MISSES


def test_track123_allow_instant_retry_resets_backoff(monkeypatch, _clear_quota_state):
    # The dashboard's per-parcel "Refresh from API" is a deliberate request
    # for a fresh look - it clears the backoff so its check may spend a
    # quota credit on the instant endpoint again.
    realtime_calls = []
    monkeypatch.setattr(track123, "_post", _stuck_track123_responder("STUCK4", realtime_calls))
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)

    track123.get_track_info(["STUCK4"])
    track123.get_track_info(["STUCK4"])
    assert len(realtime_calls) == 1

    track123.allow_instant_retry("STUCK4")
    track123.get_track_info(["STUCK4"])
    assert len(realtime_calls) == 2


def test_track123_instant_miss_counter_resets_when_events_found(monkeypatch, _clear_quota_state):
    # A hit proves the number is real and just slow to sync - it starts a
    # fresh backoff budget rather than inheriting the misses of its earlier
    # stuck spell.
    def fake_post(path, payload):
        if path == "tk/v2.1/track/query-realtime":
            return {
                "data": {
                    "accepted": {
                        "trackNo": "STUCK5",
                        "localLogisticsInfo": {
                            "courierNameEN": "Cainiao",
                            "trackingDetails": [{"eventDetail": "Departed", "eventTime": "2024-01-01T00:00:00Z"}],
                        },
                    }
                }
            }
        return {
            "data": {
                "accepted": {
                    "content": [
                        {
                            "trackNo": "STUCK5",
                            "transitStatus": "IN_TRANSIT",
                            "localLogisticsInfo": {"courierCode": "cainiao", "courierNameEN": "Cainiao"},
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(track123, "_post", fake_post)
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)
    track123._instant_attempts["STUCK5"] = {
        "last": track123.time.monotonic() - track123._INSTANT_RETRY_SECONDS - 1,
        "misses": track123._INSTANT_MAX_MISSES - 1,
    }

    results = track123.get_track_info(["STUCK5"])

    assert results["STUCK5"]["confirmed"] is True
    assert track123._instant_attempts["STUCK5"]["misses"] == 0


def test_track123_register_skips_numbers_already_accepted_by_a_query(monkeypatch, _clear_quota_state):
    # Registration is what consumes a quota credit per new shipment, so a
    # number a batch query has already accepted must never be re-imported.
    posts = []

    def fake_post(path, payload):
        posts.append(path)
        if path == "tk/v2.1/track/query":
            return {
                "data": {
                    "accepted": {
                        "content": [
                            {
                                "trackNo": "REG1",
                                "transitStatus": "IN_TRANSIT",
                                "localLogisticsInfo": {
                                    "courierNameEN": "Cainiao",
                                    "trackingDetails": [
                                        {"eventDetail": "Departed", "eventTime": "2024-01-01T00:00:00Z"}
                                    ],
                                },
                            }
                        ]
                    }
                }
            }
        return {"data": {}}

    monkeypatch.setattr(track123, "_post", fake_post)
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)

    track123.get_track_info(["REG1"])
    track123.register([("REG1", None)])

    assert posts == ["tk/v2.1/track/query"]


def test_track123_register_skips_reimport_of_numbers_it_imported(monkeypatch, _clear_quota_state):
    posts = []

    def fake_post(path, payload):
        posts.append(path)
        return {"data": {"accepted": [{"trackNo": "REG2"}]}}

    monkeypatch.setattr(track123, "_post", fake_post)
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)

    track123.register([("REG2", None)])
    track123.register([("REG2", None)])

    assert posts == ["tk/v2.1/track/import"]


def test_track123_register_still_retries_numbers_never_accepted(monkeypatch, _clear_quota_state):
    # A number Track123 rejected (or never answered for) isn't marked
    # registered - it should be retried on the next cycle, e.g. after a
    # courier-code fix ships.
    posts = []

    def fake_post(path, payload):
        posts.append(path)
        return {"data": {"rejected": [{"trackNo": "REG3", "error": {"code": "A0400", "msg": "not registered"}}]}}

    monkeypatch.setattr(track123, "_post", fake_post)
    monkeypatch.setattr(track123.time, "sleep", lambda _s: None)

    track123.register([("REG3", None)])
    track123.register([("REG3", None)])

    assert posts == ["tk/v2.1/track/import", "tk/v2.1/track/import"]


def test_seventeentrack_register_skips_numbers_already_accepted(monkeypatch, _clear_quota_state):
    # 17track's quota is a one-time pool consumed per registered number -
    # numbers it has already accepted (at registration or in a query
    # response) must not be re-registered every sync cycle.
    posts = []

    def fake_post(path, payload):
        posts.append(path)
        if path == "register":
            return {"data": {"accepted": [{"number": "SREG1"}]}}
        return {"data": {"accepted": [{"number": "SREG2", "track_info": {}}]}}

    monkeypatch.setattr(seventeentrack, "_post", fake_post)
    monkeypatch.setattr(seventeentrack.time, "sleep", lambda _s: None)

    seventeentrack.register([("SREG1", None)])
    seventeentrack.register([("SREG1", None)])  # skipped: accepted at registration
    seventeentrack.get_track_info(["SREG2"])
    seventeentrack.register([("SREG2", None)])  # skipped: accepted by a query

    assert posts == ["register", "gettrackinfo"]
