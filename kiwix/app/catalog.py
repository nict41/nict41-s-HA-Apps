"""Client for a Kiwix library's OPDS catalog (library.kiwix.org by default).

Only the read side of the catalog is here: searching entries, listing the
languages and categories used to filter them, and turning an entry into the
handful of fields the UI and the downloader need. Every call is best-effort -
a catalog that is unreachable (which, for an offline-archive add-on, is an
entirely normal state to be in) surfaces as an error string, not an
exception.
"""

import xml.etree.ElementTree as ET

import httpx

import settings

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/terms/}"
_THR = "{http://purl.org/syndication/thread/1.0}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

_ACQUISITION_REL = "http://opds-spec.org/acquisition/open-access"
_THUMBNAIL_REL = "http://opds-spec.org/image/thumbnail"

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# How each ZIM flavour is described in the UI. The catalog's own flavour
# values are terse and the difference between them is the single most
# important thing to understand before starting a 100 GB download.
FLAVOURS = {
    "maxi": {
        "label": "Full",
        "detail": "Everything, images included. By far the largest.",
    },
    "nopic": {
        "label": "No pictures",
        "detail": "Full text, no images. Roughly a tenth of the full size.",
    },
    "mini": {
        "label": "Mini",
        "detail": "Introductions and infoboxes only. Smallest by a wide margin.",
    },
    "": {
        "label": "Standard",
        "detail": "The publisher's single edition of this archive.",
    },
}


class CatalogError(RuntimeError):
    """The catalog could not be reached or understood."""


def _text(entry: ET.Element, tag: str, default: str = "") -> str:
    found = entry.find(tag)
    return (found.text or default).strip() if found is not None and found.text else default


def _int(entry: ET.Element, tag: str) -> int | None:
    raw = _text(entry, tag)
    try:
        return int(raw)
    except ValueError:
        return None


def _absolute(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    return f"{settings.LIBRARY_SOURCE}/{href.lstrip('/')}"


def parse_entry(entry: ET.Element) -> dict | None:
    """One OPDS entry as the flat dict the UI works with.

    Entries without a downloadable ZIM (the catalog does carry a few) are
    dropped by returning None.
    """
    download_url = ""
    size = None
    thumbnail = ""

    for link in entry.findall(f"{_ATOM}link"):
        rel = link.get("rel", "")
        href = link.get("href", "")
        if rel == _ACQUISITION_REL and href:
            # The catalog links a metalink; the ZIM itself sits at the same
            # URL without the .meta4 suffix and supports range requests,
            # which is what makes downloads resumable.
            download_url = href[: -len(".meta4")] if href.endswith(".meta4") else href
            try:
                size = int(link.get("length", ""))
            except ValueError:
                size = None
        elif rel == _THUMBNAIL_REL and href:
            thumbnail = _absolute(href)

    if not download_url:
        return None

    flavour = _text(entry, f"{_ATOM}flavour")
    filename = download_url.rsplit("/", 1)[-1]

    return {
        "id": _text(entry, f"{_ATOM}id"),
        "title": _text(entry, f"{_ATOM}title"),
        "summary": _text(entry, f"{_ATOM}summary"),
        "language": _text(entry, f"{_ATOM}language"),
        "name": _text(entry, f"{_ATOM}name"),
        "flavour": flavour,
        "flavour_label": FLAVOURS.get(flavour, {"label": flavour or "Standard"})["label"],
        "flavour_detail": FLAVOURS.get(flavour, {}).get("detail", ""),
        "category": _text(entry, f"{_ATOM}category"),
        "publisher": _text(entry, f"{_ATOM}publisher/{_ATOM}name"),
        "issued": _text(entry, f"{_DC}issued")[:10],
        "article_count": _int(entry, f"{_ATOM}articleCount"),
        "media_count": _int(entry, f"{_ATOM}mediaCount"),
        "size": size,
        "url": download_url,
        "filename": filename,
        "thumbnail": thumbnail,
    }


def _get(path: str, params: dict | None = None) -> ET.Element:
    url = f"{settings.LIBRARY_SOURCE}{path}"
    try:
        response = httpx.get(url, params=params, timeout=_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return ET.fromstring(response.content)
    except httpx.HTTPError as exc:
        raise CatalogError(
            f"Could not reach the library at {settings.LIBRARY_SOURCE}: {exc}"
        ) from exc
    except ET.ParseError as exc:
        raise CatalogError(f"The library at {url} returned something unreadable: {exc}") from exc


def search(
    query: str = "",
    language: str = "",
    category: str = "",
    count: int = 30,
    start: int = 0,
) -> dict:
    """Search catalog entries. Raises CatalogError if the library is down."""
    params: dict[str, str | int] = {"count": count, "start": start}
    if query:
        params["q"] = query
    if language:
        params["lang"] = language
    if category:
        params["category"] = category

    feed = _get("/catalog/v2/entries", params)
    entries = [parsed for entry in feed.findall(f"{_ATOM}entry") if (parsed := parse_entry(entry))]

    # library.kiwix.org declares the OpenSearch namespace but writes
    # totalResults unprefixed, which puts it in the *default* (Atom)
    # namespace; check both so paging works against either spelling.
    total_el = feed.find(f"{_ATOM}totalResults")
    if total_el is None:
        total_el = feed.find(f"{_OPENSEARCH}totalResults")
    try:
        total = int(total_el.text) if total_el is not None and total_el.text else len(entries)
    except ValueError:
        total = len(entries)

    return {"entries": entries, "total": total, "start": start, "count": count}


def wikipedia_variants(language: str = "") -> dict:
    """The Wikipedia archives for a language, largest edition first.

    This is the curated view: same catalog, narrowed to `category=wikipedia`
    and ordered so the choice between full / no-pictures / mini for a given
    edition is a side-by-side one.
    """
    result = search(query="", language=language or settings.CATALOG_LANGUAGE, category="wikipedia", count=100)
    result["entries"].sort(key=lambda e: (e["name"], -(e["size"] or 0)))
    return result


def languages() -> list[dict]:
    feed = _get("/catalog/v2/languages")
    items = []
    for entry in feed.findall(f"{_ATOM}entry"):
        code = _text(entry, f"{_DC}language")
        if not code:
            continue
        items.append(
            {
                "code": code,
                "title": _text(entry, f"{_ATOM}title", code),
                "count": _int(entry, f"{_THR}count") or 0,
            }
        )
    items.sort(key=lambda item: -item["count"])
    return items


def categories() -> list[str]:
    feed = _get("/catalog/v2/categories")
    names = [_text(entry, f"{_ATOM}title") for entry in feed.findall(f"{_ATOM}entry")]
    return sorted(name for name in names if name)
