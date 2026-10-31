import json
import urllib.error

import lookup
import pytest


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_lookup_barcode_returns_product_fields(monkeypatch):
    payload = {
        "status": 1,
        "product": {
            "product_name_en": "Baked Beans English",
            "product_name": "Baked Beans",
            "brands": "Heinz,Other",
            "quantity": "415g",
            "categories": "Tinned foods,Beans",
            "categories_tags": ["en:canned-foods"],
            "image_front_small_url": "https://example.com/img.jpg",
        },
    }
    monkeypatch.setattr(lookup.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(payload))

    result = lookup.lookup_barcode("5000157024671")
    assert result == {
        "name": "Baked Beans English",
        "brand": "Heinz",
        "quantity": "415g",
        "category": "Tinned foods",
        "categories_tags": ["en:canned-foods"],
        "image_url": "https://example.com/img.jpg",
    }


def test_lookup_barcode_returns_none_when_not_found(monkeypatch):
    monkeypatch.setattr(lookup.urllib.request, "urlopen", lambda req, timeout: _FakeResponse({"status": 0}))
    assert lookup.lookup_barcode("0000000000000") is None


def test_lookup_barcode_raises_named_error_on_network_error(monkeypatch):
    def _raise(req, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(lookup.urllib.request, "urlopen", _raise)
    with pytest.raises(lookup.LookupUnavailable):
        lookup.lookup_barcode("5000157024671")



def test_lookup_barcode_treats_http_404_as_not_found(monkeypatch):
    def _raise(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(lookup.urllib.request, "urlopen", _raise)
    assert lookup.lookup_barcode("0000000000000") is None


def test_lookup_barcode_treats_status_zero_as_not_found(monkeypatch):
    monkeypatch.setattr(lookup.urllib.request, "urlopen", lambda req, timeout: _FakeResponse({"status": 0}))
    assert lookup.lookup_barcode("0000000000000") is None


def test_lookup_barcode_non_404_http_error_is_unavailable(monkeypatch):
    def _raise(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, None)
    monkeypatch.setattr(lookup.urllib.request, "urlopen", _raise)
    with pytest.raises(lookup.LookupUnavailable):
        lookup.lookup_barcode("5000157024671")
