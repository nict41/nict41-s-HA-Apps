import xml.etree.ElementTree as ET

import httpx
import pytest

import catalog
import settings

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:dc="http://purl.org/dc/terms/"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <totalResults>67</totalResults>
  <entry>
    <id>urn:uuid:0ce0eda1</id>
    <title>Wikipedia 0.8</title>
    <summary>Wikipedia 45,000 best articles</summary>
    <language>eng</language>
    <name>wikipedia_en_wp1-0.8</name>
    <flavour>nopic</flavour>
    <category>wikipedia</category>
    <articleCount>855632</articleCount>
    <mediaCount>47737</mediaCount>
    <link rel="http://opds-spec.org/image/thumbnail" href="/catalog/v2/illustration/0ce0eda1/?size=48"/>
    <publisher><name>openZIM</name></publisher>
    <dc:issued>2026-07-10T00:00:00Z</dc:issued>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_wp1-0.8_nopic_2026-07.zim.meta4"
          length="2347802624"/>
  </entry>
  <entry>
    <id>urn:uuid:deadbeef</id>
    <title>Not downloadable</title>
    <language>eng</language>
  </entry>
</feed>
"""


def _canned_feed(url, **kwargs):
    """A stand-in for httpx.get that serves the fixture feed."""
    return httpx.Response(200, content=FEED.encode(), request=httpx.Request("GET", url))


def _entries():
    return ET.fromstring(FEED).findall("{http://www.w3.org/2005/Atom}entry")


def test_entry_parses_into_the_fields_the_ui_needs():
    entry = catalog.parse_entry(_entries()[0])

    assert entry["title"] == "Wikipedia 0.8"
    assert entry["language"] == "eng"
    assert entry["flavour"] == "nopic"
    assert entry["flavour_label"] == "No pictures"
    assert entry["size"] == 2347802624
    assert entry["article_count"] == 855632
    assert entry["issued"] == "2026-07-10"
    assert entry["publisher"] == "openZIM"


def test_the_metalink_is_turned_into_the_direct_resumable_zim_url():
    entry = catalog.parse_entry(_entries()[0])
    assert entry["url"].endswith("wikipedia_en_wp1-0.8_nopic_2026-07.zim")
    assert entry["filename"] == "wikipedia_en_wp1-0.8_nopic_2026-07.zim"


def test_relative_thumbnails_are_made_absolute():
    entry = catalog.parse_entry(_entries()[0])
    assert entry["thumbnail"] == f"{settings.LIBRARY_SOURCE}/catalog/v2/illustration/0ce0eda1/?size=48"


def test_entries_without_a_download_are_dropped():
    assert catalog.parse_entry(_entries()[1]) is None


def test_search_reads_the_total_and_skips_undownloadable_entries(monkeypatch):
    monkeypatch.setattr(catalog.httpx, "get", _canned_feed)

    result = catalog.search(query="wikipedia")

    assert result["total"] == 67
    assert len(result["entries"]) == 1


def test_an_unreachable_library_raises_a_catalog_error(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(catalog.httpx, "get", boom)

    with pytest.raises(catalog.CatalogError) as excinfo:
        catalog.search()

    assert settings.LIBRARY_SOURCE in str(excinfo.value)


def test_wikipedia_variants_group_editions_largest_first(monkeypatch):
    captured = {}

    def fake_get(url, params=None, **kwargs):
        captured.update(params or {})
        return _canned_feed(url)

    monkeypatch.setattr(catalog.httpx, "get", fake_get)

    result = catalog.wikipedia_variants("fra")

    assert captured["category"] == "wikipedia"
    assert captured["lang"] == "fra"
    assert [entry["flavour"] for entry in result["entries"]] == ["nopic"]
