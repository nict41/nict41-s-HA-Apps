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
                            "localLogisticsInfo": {"courierNameEN": "Cainiao"},
                        }
                    ]
                }
            }
        },
    )

    results = track123.get_track_info(["REAL2"])

    assert results["REAL2"]["confirmed"] is True
    assert results["REAL2"]["carrier_name"] == "Cainiao"


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
