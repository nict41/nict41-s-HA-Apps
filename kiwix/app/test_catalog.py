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

    def fake_pool(filters):
        captured.update(filters)
        return [
            {"title": "Wikipedia", "name": "wikipedia_fr_all", "flavour": "mini",
             "size": 1_000, "article_count": 1, "issued": "2026-01-01", "filename": "a.zim"},
            {"title": "Wikipedia", "name": "wikipedia_fr_all", "flavour": "maxi",
             "size": 9_000, "article_count": 1, "issued": "2026-01-01", "filename": "b.zim"},
        ]

    monkeypatch.setattr(catalog, "_pool", fake_pool)

    result = catalog.wikipedia_variants("fra")

    assert captured["category"] == "wikipedia"
    assert captured["lang"] == "fra"
    # Flavours of one edition stay together, biggest first, so the choice
    # between them is a side-by-side one.
    assert [entry["flavour"] for entry in result["entries"]] == ["maxi", "mini"]


# ── Details ─────────────────────────────────────────────────────────────────

def test_an_entry_carries_the_details_the_ui_shows():
    entry = catalog.parse_entry(_entries()[0])

    assert entry["media_count"] == 47737
    assert entry["publisher"] == "openZIM"
    assert entry["summary"] == "Wikipedia 45,000 best articles"
    assert entry["name"] == "wikipedia_en_wp1-0.8"


def test_machine_tags_become_readable_ones():
    tags = catalog._readable_tags("wikipedia;_category:wikipedia;_pictures:no;_videos:no;_ftindex:yes")

    assert tags == ["no pictures", "no videos", "full-text search"]


# ── Sorting ─────────────────────────────────────────────────────────────────

def _pool_of(monkeypatch, entries):
    monkeypatch.setattr(catalog, "_pool", lambda filters: list(entries))


def _entry(title, size=0, articles=0, issued=""):
    return {"title": title, "size": size, "article_count": articles, "issued": issued,
            "filename": title + ".zim"}


def test_results_can_be_sorted_by_size(monkeypatch):
    _pool_of(monkeypatch, [_entry("small", 10), _entry("huge", 900), _entry("medium", 500)])

    biggest = catalog.search(sort="size_desc")["entries"]
    smallest = catalog.search(sort="size_asc")["entries"]

    assert [e["title"] for e in biggest] == ["huge", "medium", "small"]
    assert [e["title"] for e in smallest] == ["small", "medium", "huge"]


def test_results_can_be_sorted_by_articles_title_and_date(monkeypatch):
    _pool_of(monkeypatch, [
        _entry("beta", articles=10, issued="2026-01-01"),
        _entry("alpha", articles=90, issued="2024-05-05"),
        _entry("gamma", articles=50, issued="2026-07-07"),
    ])

    assert [e["title"] for e in catalog.search(sort="articles")["entries"]] == ["alpha", "gamma", "beta"]
    assert [e["title"] for e in catalog.search(sort="title")["entries"]] == ["alpha", "beta", "gamma"]
    assert [e["title"] for e in catalog.search(sort="date")["entries"]] == ["gamma", "beta", "alpha"]


def test_sorting_pages_through_the_sorted_set(monkeypatch):
    _pool_of(monkeypatch, [_entry(f"e{index}", size=index) for index in range(10)])

    page = catalog.search(sort="size_asc", start=4, count=3)

    assert [e["title"] for e in page["entries"]] == ["e4", "e5", "e6"]
    assert page["total"] == 10
    assert page["sorted_over"] == 10


def test_an_unknown_sort_falls_back_to_the_catalog_order(monkeypatch):
    monkeypatch.setattr(catalog, "_page", lambda params: ([_entry("only")], 1))

    result = catalog.search(sort="by-vibes")

    assert result["sort"] == "relevance"
    assert [e["title"] for e in result["entries"]] == ["only"]


# ── Finding the entry behind a filename ─────────────────────────────────────

def test_a_partial_download_is_traced_back_to_its_catalog_entry(monkeypatch):
    asked = []

    def fake_get(path, params=None):
        asked.append(params["name"])
        return ET.fromstring(FEED)

    monkeypatch.setattr(catalog, "_get", fake_get)

    entry = catalog.find_by_filename("wikipedia_en_wp1-0.8_nopic_2026-07.zim")

    assert entry is not None
    assert entry["url"].endswith("wikipedia_en_wp1-0.8_nopic_2026-07.zim")
    # The flavour and date are walked off the filename to get the ZIM name.
    assert asked[0] == "wikipedia_en_wp1-0.8_nopic"


def test_a_filename_the_catalog_no_longer_carries_returns_nothing(monkeypatch):
    monkeypatch.setattr(catalog, "_get", lambda path, params=None: ET.fromstring(FEED))

    # Same archive, an edition that has since been replaced: resuming into it
    # would write new bytes into a file of the old edition.
    assert catalog.find_by_filename("wikipedia_en_wp1-0.8_nopic_2019-01.zim") is None
