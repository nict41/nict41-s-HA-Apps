"""Client for a Kiwix library's OPDS catalog (library.kiwix.org by default).

Only the read side of the catalog is here: searching entries, sorting and
describing them, listing the languages and categories used to filter them,
and finding the entry a given `.zim` filename came from. Every call is
best-effort - a catalog that is unreachable (which, for an offline-archive
add-on, is an entirely normal state to be in) surfaces as an error string,
not an exception.
"""

import re
import threading
import time
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
        "detail": "Full text, no images. Roughly a fifth of the full size.",
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

# The catalog has no sort parameter of its own, so sorting means fetching a
# wide slice and ordering it here. This caps how wide.
SORT_POOL = 400
_PAGE = 100

SORTS = {
    "relevance": None,
    "title": lambda e: (e["title"].lower(), e["size"] or 0),
    "size_desc": lambda e: -(e["size"] or 0),
    "size_asc": lambda e: e["size"] or 0,
    "date": lambda e: (e["issued"] or "", e["title"].lower()),
    "articles": lambda e: -(e["article_count"] or 0),
}
# Sorts where the interesting end is the top of the list, not the bottom.
_DESCENDING = {"date"}

_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 120.0


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


def _readable_tags(raw: str) -> list[str]:
    """The catalog's tag string as things worth showing.

    Tags come as `wikipedia;_category:wikipedia;_pictures:no;_ftindex:yes`.
    The bare ones repeat the category and the `_`-prefixed ones are machine
    flags, of which only a few say anything a reader cares about.
    """
    interesting = {"_pictures": "pictures", "_videos": "videos", "_details": "full articles",
                   "_ftindex": "full-text search"}
    tags = []
    for item in raw.split(";"):
        key, _, value = item.partition(":")
        if key in interesting and value in ("yes", "no"):
            tags.append(("" if value == "yes" else "no ") + interesting[key])
    return tags


def parse_entry(entry: ET.Element) -> dict | None:
    """One OPDS entry as the flat dict the UI works with.

    Entries without a downloadable ZIM (the catalog does carry a few) are
    dropped by returning None.
    """
    download_url = ""
    size = None
    thumbnail = ""
    preview_url = ""

    for link in entry.findall(f"{_ATOM}link"):
        rel = link.get("rel", "")
        href = link.get("href", "")
        if rel == _ACQUISITION_REL and href:
            # The catalog links a metalink; the ZIM itself sits at the same
            # URL without the .meta4 suffix and supports range requests,
            # which is what makes downloads resumable and splittable.
            download_url = href[: -len(".meta4")] if href.endswith(".meta4") else href
            try:
                size = int(link.get("length", ""))
            except ValueError:
                size = None
        elif rel == _THUMBNAIL_REL and href:
            thumbnail = _absolute(href)
        elif not rel and link.get("type") == "text/html" and href:
            preview_url = href

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
        "author": _text(entry, f"{_ATOM}author/{_ATOM}name"),
        "issued": _text(entry, f"{_DC}issued")[:10],
        "updated": _text(entry, f"{_ATOM}updated")[:10],
        "article_count": _int(entry, f"{_ATOM}articleCount"),
        "media_count": _int(entry, f"{_ATOM}mediaCount"),
        "tags": _readable_tags(_text(entry, f"{_ATOM}tags")),
        "size": size,
        "url": download_url,
        "filename": filename,
        "thumbnail": thumbnail,
        "preview_url": preview_url,
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


def _page(params: dict) -> tuple[list[dict], int]:
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

    return entries, total


def _pool(filters: dict) -> list[dict]:
    """Up to SORT_POOL entries matching the filters, cached briefly.

    Sorting and its paging both work over this pool, so flipping through
    pages of a sorted result doesn't re-fetch the whole thing each time.
    """
    key = tuple(sorted(filters.items()))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]

    entries: list[dict] = []
    while len(entries) < SORT_POOL:
        page, total = _page({**filters, "count": _PAGE, "start": len(entries)})
        entries.extend(page)
        if not page or len(entries) >= total:
            break

    with _cache_lock:
        _cache[key] = (now, entries)
        if len(_cache) > 24:
            _cache.pop(next(iter(_cache)))
    return entries


def search(
    query: str = "",
    language: str = "",
    category: str = "",
    count: int = 30,
    start: int = 0,
    sort: str = "relevance",
) -> dict:
    """Search catalog entries. Raises CatalogError if the library is down."""
    filters: dict[str, str] = {}
    if query:
        filters["q"] = query
    if language:
        filters["lang"] = language
    if category:
        filters["category"] = category

    if sort not in SORTS:
        sort = "relevance"

    if sort == "relevance":
        entries, total = _page({**filters, "count": count, "start": start})
        return {"entries": entries, "total": total, "start": start, "count": count,
                "sort": sort, "sorted_over": None}

    pool = list(_pool(filters))
    pool.sort(key=SORTS[sort], reverse=sort in _DESCENDING)
    return {
        "entries": pool[start:start + count],
        "total": len(pool),
        "start": start,
        "count": count,
        "sort": sort,
        # How many entries the ordering could see: a sorted result over a
        # capped pool is honest about being capped rather than pretending to
        # have ranked the whole catalog.
        "sorted_over": len(pool),
    }


def wikipedia_variants(language: str = "", sort: str = "size_desc") -> dict:
    """The Wikipedia archives for a language, largest edition first.

    This is the curated view: same catalog, narrowed to `category=wikipedia`
    and ordered so the choice between full / no-pictures / mini for a given
    edition is a side-by-side one.
    """
    result = search(
        language=language or settings.get("catalog_language"),
        category="wikipedia",
        count=100,
        sort=sort if sort != "relevance" else "size_desc",
    )
    if sort in ("relevance", "size_desc"):
        result["entries"].sort(key=lambda e: (e["name"], -(e["size"] or 0)))
    return result


def find_by_filename(filename: str) -> dict | None:
    """The catalog entry a `.zim` filename came from, or None.

    Used to resume a partial download whose source URL isn't known - an
    archive interrupted before the job list was persisted, or copied onto
    the share by hand. The catalog can be filtered by ZIM name, which is the
    filename minus its flavour and date, so this walks the filename back one
    underscore at a time until a query matches and then insists on the exact
    filename: a newer edition is a different file and must not be silently
    written into the old one.
    """
    stem = filename[: -len(".zim")] if filename.endswith(".zim") else filename
    candidate = re.sub(r"_\d{4}-\d{2}$", "", stem)

    while candidate:
        feed = _get("/catalog/v2/entries", {"name": candidate, "count": 50})
        for element in feed.findall(f"{_ATOM}entry"):
            entry = parse_entry(element)
            if entry and entry["filename"] == filename:
                return entry
        if "_" not in candidate:
            return None
        candidate = candidate.rsplit("_", 1)[0]
    return None


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
