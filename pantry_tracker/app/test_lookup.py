import json
import urllib.error

import lookup


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
            "product_name": "Baked Beans",
            "brands": "Heinz,Other",
            "quantity": "415g",
            "categories": "Tinned foods,Beans",
            "image_front_small_url": "https://example.com/img.jpg",
        },
    }
    monkeypatch.setattr(lookup.urllib.request, "urlopen", lambda req, timeout: _FakeResponse(payload))

    result = lookup.lookup_barcode("5000157024671")
    assert result == {
        "name": "Baked Beans",
        "brand": "Heinz",
        "quantity": "415g",
        "category": "Tinned foods",
        "image_url": "https://example.com/img.jpg",
    }


def test_lookup_barcode_returns_none_when_not_found(monkeypatch):
    monkeypatch.setattr(lookup.urllib.request, "urlopen", lambda req, timeout: _FakeResponse({"status": 0}))
    assert lookup.lookup_barcode("0000000000000") is None


def test_lookup_barcode_returns_none_on_network_error(monkeypatch):
    def _raise(req, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(lookup.urllib.request, "urlopen", _raise)
    assert lookup.lookup_barcode("5000157024671") is None
