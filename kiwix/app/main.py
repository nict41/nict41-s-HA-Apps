"""Kiwix add-on manager: library UI, catalog browser, downloader, and the
reverse proxy that puts kiwix-serve itself inside the same ingress panel.

Everything the user sees is served from here on port 8099, which is the
add-on's ingress port. kiwix-serve stays on loopback and is reached only
through `/kiwix`, so reading an article goes through Home Assistant's
authentication exactly like the rest of the panel does.
"""

import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import catalog
import downloads
import library
import server
import settings

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# Hop-by-hop headers, which belong to a single connection and must not be
# copied between the two sides of the proxy.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}

# Language/category lists change about as often as the catalog gains a new
# project, so one fetch per hour is plenty and keeps the filters instant.
_FILTER_TTL = 3600.0
_filters_cache: dict = {"fetched_at": 0.0, "value": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    library.prune_served()
    downloads.adopt_partials()
    server.start_monitor()
    if server.enabled() and library.read_state()["served"]:
        await run_in_threadpool(server.start)
    yield
    server.stop()
    await _kiwix_client.aclose()


app = FastAPI(title="Kiwix", lifespan=lifespan)

_kiwix_client = httpx.AsyncClient(
    base_url=f"http://127.0.0.1:{settings.KIWIX_PORT}",
    timeout=httpx.Timeout(300.0, connect=10.0),
    follow_redirects=False,
)


def _ingress_prefix(request: Request) -> str:
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/state")
async def api_state():
    def collect() -> dict:
        storage = settings.storage_status()
        return {
            "storage": storage,
            "options": settings.as_dict(),
            "server": server.status(),
            "library": library.list_zims(),
            "downloads": downloads.snapshot(),
        }

    return await run_in_threadpool(collect)


@app.get("/api/catalog")
async def api_catalog(
    q: str = "",
    lang: str = "",
    category: str = "",
    start: int = 0,
    count: int = 30,
):
    def collect() -> dict:
        result = catalog.search(
            query=q, language=lang, category=category, start=start, count=min(count, 100)
        )
        return _annotate(result)

    try:
        return await run_in_threadpool(collect)
    except catalog.CatalogError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/catalog/wikipedia")
async def api_catalog_wikipedia(lang: str = ""):
    def collect() -> dict:
        return _annotate(catalog.wikipedia_variants(lang))

    try:
        return await run_in_threadpool(collect)
    except catalog.CatalogError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/catalog/filters")
async def api_catalog_filters():
    now = time.monotonic()
    if _filters_cache["value"] is not None and now - _filters_cache["fetched_at"] < _FILTER_TTL:
        return _filters_cache["value"]

    def collect() -> dict:
        return {"languages": catalog.languages(), "categories": catalog.categories()}

    try:
        value = await run_in_threadpool(collect)
    except catalog.CatalogError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    _filters_cache.update({"fetched_at": now, "value": value})
    return value


def _annotate(result: dict) -> dict:
    """Mark catalog entries the library already has (or is downloading)."""
    present = library.downloaded_filenames()
    in_flight = {
        job["filename"] for job in downloads.snapshot() if job["status"] in downloads.ACTIVE_STATES
    }
    for entry in result["entries"]:
        entry["downloaded"] = entry["filename"] in present
        entry["downloading"] = entry["filename"] in in_flight
    return result


@app.post("/api/downloads")
async def api_download_start(payload: dict):
    job, error = await run_in_threadpool(
        downloads.start,
        str(payload.get("url", "")),
        str(payload.get("filename", "")),
        str(payload.get("title", "")),
        payload.get("size") if isinstance(payload.get("size"), int) else None,
    )
    if error:
        return JSONResponse({"error": error}, status_code=400)
    return {"download": job}


@app.post("/api/downloads/{job_id}/{action}")
async def api_download_action(job_id: str, action: str):
    handlers = {"cancel": downloads.cancel, "resume": downloads.resume, "forget": downloads.forget}
    handler = handlers.get(action)
    if handler is None:
        return JSONResponse({"error": f"Unknown action '{action}'."}, status_code=404)
    ok, error = await run_in_threadpool(handler, job_id)
    if not ok:
        return JSONResponse({"error": error}, status_code=400)
    return {"ok": True}


@app.post("/api/library/{filename}/serve")
async def api_library_serve(filename: str, payload: dict, request: Request):
    path = library.zim_path_for(filename)
    served = bool(payload.get("served", True))
    # Unserving is always allowed (it also clears a stale selection); serving
    # something that isn't on the share would just make kiwix-serve reject it.
    if path is None or (served and not path.is_file()):
        return JSONResponse(
            {"error": "Unknown archive, or the storage share is unavailable."}, status_code=400
        )
    await run_in_threadpool(library.set_served, filename, served)

    # Selecting the first archive is also what starts the server, since an
    # empty library has nothing to serve.
    if served and server.enabled() and not server.is_running():
        await run_in_threadpool(server.start, _ingress_prefix(request))
    return {"ok": True, "server": server.status()}


@app.delete("/api/library/{filename}")
async def api_library_delete(filename: str):
    ok, error = await run_in_threadpool(library.delete_zim, filename)
    if not ok:
        return JSONResponse({"error": error}, status_code=400)
    return {"ok": True}


@app.post("/api/server/{action}")
async def api_server(action: str, request: Request):
    prefix = _ingress_prefix(request)
    if action == "start":
        ok, error = await run_in_threadpool(server.set_enabled, True, prefix)
    elif action == "stop":
        ok, error = await run_in_threadpool(server.set_enabled, False, prefix)
    elif action == "restart":
        ok, error = await run_in_threadpool(server.restart, prefix)
    else:
        return JSONResponse({"error": f"Unknown action '{action}'."}, status_code=404)

    if not ok:
        return JSONResponse({"error": error, "server": server.status()}, status_code=400)
    return {"ok": True, "server": server.status()}


@app.api_route(
    "/kiwix{path:path}", methods=["GET", "HEAD", "POST", "OPTIONS"], include_in_schema=False
)
async def proxy_kiwix(path: str, request: Request):
    """Pass a request through to kiwix-serve, under the public URL prefix.

    kiwix-serve is configured with the same prefix the browser used, so the
    path it is asked for here is simply that prefix plus this request's path
    - no URL rewriting of its pages is needed anywhere.
    """
    prefix = _ingress_prefix(request)
    if prefix:
        await run_in_threadpool(server.ensure_root, prefix)
    elif settings.INGRESS_ENTRY:
        # Reached on the add-on's direct port while kiwix-serve is generating
        # ingress links: every link on the page would 404. Say so plainly
        # instead of serving a broken copy of the reader.
        return HTMLResponse(_direct_access_page(), status_code=421)

    if not server.is_running():
        started, error = await run_in_threadpool(server.start, prefix)
        if not started:
            return HTMLResponse(_server_down_page(error), status_code=503)

    upstream_path = f"{prefix}/kiwix{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}

    url = httpx.URL(path=upstream_path)
    if request.url.query:
        url = url.copy_with(query=request.url.query.encode())

    upstream = _kiwix_client.build_request(
        request.method,
        url,
        headers=headers,
        content=await request.body() if request.method == "POST" else None,
    )

    try:
        response = await _kiwix_client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        return HTMLResponse(_server_down_page(str(exc)), status_code=502)

    async def body():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        body(),
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() not in _HOP_BY_HOP},
    )


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; padding: 2.5rem 1.5rem; font: 15px/1.5 "Helvetica Neue", Helvetica, Arial, sans-serif;
         background: #f0efe9; color: #191b14; }}
  main {{ max-width: 34rem; margin: 0 auto; border: 1px solid #d7d5c9; border-top: 2px solid #191b14;
          background: #f7f6f1; padding: 1.5rem; box-shadow: 3px 3px 0 rgba(25,27,20,0.16); }}
  h1 {{ font-size: 0.8rem; letter-spacing: 0.16em; text-transform: uppercase; margin: 0 0 0.75rem; }}
  a {{ color: inherit; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0c0e09; color: #e7ead9; }}
    main {{ background: #12140d; border-color: #262b1a; border-top-color: #c6f53c;
            box-shadow: 3px 3px 0 rgba(0,0,0,0.55); }}
  }}
</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""


def _server_down_page(error: str) -> str:
    return _page(
        "Kiwix reader unavailable",
        f"<p>kiwix-serve isn't running, so there is nothing to read yet.</p>"
        f"<p>{error or 'Select at least one archive to serve in the library.'}</p>"
        f'<p><a href="../">Back to the library</a></p>',
    )


def _direct_access_page() -> str:
    return _page(
        "Open Kiwix from the sidebar",
        "<p>The reader's links are built for Home Assistant's ingress path, so "
        "reading articles only works when Kiwix is opened from the Home "
        "Assistant sidebar - not over this add-on's direct port.</p>"
        '<p><a href="../">Back to the library</a></p>',
    )
