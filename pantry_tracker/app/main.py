"""Pantry Tracker - scan a grocery barcode, look it up, read its use-by date
from a photo, and get notified before it expires. See README.md for the
full workflow.
"""

import json
import mimetypes
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, Form, UploadFile, File, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import barcode
import db
import ha_sync
import lookup
import photos
import settings
import vision
import tidy
import ha_events

_REPUBLISH_JOB_ID = "republish_after_restart"
_NOTIFY_JOB_ID = "daily_notification"
_SYNC_JOB_ID = "midnight_sync"
_RETENTION_JOB_ID = "photo_retention"
_PRODUCT_IMAGE_BACKFILL_STATE_KEY = "product_image_backfill_done"
_IMAGE_BACKFILL_STATE_KEY = "image_backfill_done"
# Open Food Facts asks clients to keep product lookups well under 100/minute.
_BACKFILL_LOOKUP_DELAY_SECONDS = 1

APP_DIR = Path(__file__).resolve().parent
# The browser won't compile WebAssembly served as anything else, and this
# varies by what the system's mime database happens to know.
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")

_MAX_OCR_RAW_LENGTH = 2000
_VISION_READ_LOCK = threading.Lock()
_CLIENT_TIMING_FIELDS = {
    "open_ms", "stream_ms", "first_sample_ms", "steady_ms", "capture_ms",
    "ocr_ms", "read_ms", "soft", "read_slow", "tick_ms",
}
_CLIENT_TIMING_PATHS = {"steady", "live", "shutter"}

db.init_db()

scheduler = BackgroundScheduler(timezone=db.local_zone())


def run_check_cycle() -> str:
    """Sends due-item summary and queues a current-state sync."""
    items = db.list_items([db.STATUS_ACTIVE])
    result = ha_sync.notify_summary(items)
    db.set_state("last_check_at", db.now_iso())
    summaries = {
        "sent": "Summary sent",
        "nothing_due": "Nothing is due right now",
        "failed": "Couldn't send the summary",
        "not_configured": "Notifications aren't set up",
    }
    db.set_state("last_check_summary", summaries[result])
    ha_sync.request_sync()
    return result


def run_full_sync() -> None:
    ha_sync.sync(db.list_items([db.STATUS_ACTIVE]))


def run_republish_check() -> None:
    """Republishes entities when Home Assistant has lost transient states."""
    if ha_sync.summary_state_exists() is False:
        ha_sync.sync(db.list_items([db.STATUS_ACTIVE]))



def _extract_and_update(item_id: int, *, only_if_blank: bool = False) -> None:
    """Runs in the background after a use-by date photo is saved, so the
    request that uploaded it (adding a new item, or rescanning an existing
    one) returns immediately rather than making the client wait on the
    vision model's round trip - the person is free to go on scanning the
    next item straight away. Reads the photo back off disk rather than
    carrying the bytes through the task closure, since that's already
    where it needs to end up for the "view the photo" feature, and it
    means a restart mid-extraction can't lose the photo itself.
    """
    if not db.get_item(item_id):
        return  # deleted before extraction got a chance to run
    path = db.date_photo_path(item_id)
    if not path.exists():
        return
    try:
        image_bytes = path.read_bytes()
    except FileNotFoundError:
        return
    with _VISION_READ_LOCK:
        result = vision.extract_use_by_date(image_bytes)
        if result.get("retryable"):
            result = vision.extract_use_by_date(image_bytes)
    if not db.get_item(item_id):
        return  # deleted while extraction was running
    candidates = result.get("candidates") or ([{"date": result["date"], "repaired": False}] if result.get("date") else [])
    db.finish_date_extraction(item_id, candidates, result.get("raw") or "", only_if_blank=only_if_blank)
    ha_sync.request_sync()


def run_image_backfill() -> None:
    """Fills in product image URLs for items added before 0.6.0.

    Lookup failures leave the flag unset so the backfill retries on restart.
    """
    if db.get_state(_IMAGE_BACKFILL_STATE_KEY):
        return
    complete = True
    for barcode in db.barcodes_missing_image():
        cached = db.get_cached_barcode(barcode)
        image_url = cached["image_url"] if cached else ""
        if not image_url:
            try:
                product = lookup.lookup_barcode(barcode)
            except lookup.LookupUnavailable:
                product = None
            time.sleep(_BACKFILL_LOOKUP_DELAY_SECONDS)
            if product is None:
                complete = False
                continue
            image_url = product["image_url"]
            if image_url:
                photos.download_product_image(image_url, barcode)
        if image_url:
            db.apply_barcode_image(barcode, image_url)
    if complete:
        db.set_state(_IMAGE_BACKFILL_STATE_KEY, db.now_iso())


def run_product_image_backfill() -> None:
    """Caches product images for active barcodes once per installation."""
    if db.get_state(_PRODUCT_IMAGE_BACKFILL_STATE_KEY):
        return
    complete = True
    barcodes = sorted({
        item["barcode"]
        for item in db.list_items([db.STATUS_ACTIVE])
        if db.valid_barcode(item.get("barcode"))
    })
    for code in barcodes:
        if db.product_image_path(code).exists():
            continue
        cached = db.get_cached_barcode(code)
        image_url = cached.get("image_url") if cached else ""
        if not image_url:
            try:
                product = lookup.lookup_barcode(code)
            except lookup.LookupUnavailable:
                complete = False
                time.sleep(_BACKFILL_LOOKUP_DELAY_SECONDS)
                continue
            time.sleep(_BACKFILL_LOOKUP_DELAY_SECONDS)
            if product is None:
                continue
            image_url = product.get("image_url", "")
        if image_url:
            photos.download_product_image(image_url, code)
        time.sleep(_BACKFILL_LOOKUP_DELAY_SECONDS)
    if complete:
        db.set_state(_PRODUCT_IMAGE_BACKFILL_STATE_KEY, db.now_iso())


def run_photo_retention() -> None:
    db.expire_old_date_photos()


def run_pending_date_extractions() -> None:
    """Restarts pending photo reads left behind by a process restart."""
    # only_if_blank: a date typed in before the restart must survive it.
    for item_id in db.pending_date_extractions():
        _extract_and_update(item_id, only_if_blank=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tidy_existing_data()
    scheduler.add_job(run_full_sync, next_run_time=datetime.now())
    scheduler.add_job(
        run_full_sync,
        "cron",
        hour=0,
        minute=5,
        id=_SYNC_JOB_ID,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.add_job(run_republish_check, "interval", minutes=5, next_run_time=datetime.now(), id=_REPUBLISH_JOB_ID)
    scheduler.add_job(run_check_cycle, "cron", hour=settings.get_int("notify_hour"), id=_NOTIFY_JOB_ID,
                      misfire_grace_time=3600, coalesce=True)
    print(
        f"[pantry_tracker] Daily summary at {settings.get_int('notify_hour'):02d}:00 {db.local_zone()}",
        flush=True,
    )
    scheduler.add_job(run_image_backfill, next_run_time=datetime.now())
    scheduler.add_job(run_product_image_backfill, next_run_time=datetime.now())
    scheduler.add_job(run_photo_retention, "cron", hour=0, minute=15, id=_RETENTION_JOB_ID, coalesce=True)
    scheduler.add_job(run_pending_date_extractions, next_run_time=datetime.now())
    scheduler.start()
    stop_events = threading.Event()
    event_thread = None
    if ha_sync.configured():
        ha_sync.start_sync_worker()
        event_thread = threading.Thread(target=ha_events.run, args=(stop_events,), daemon=True)
        event_thread.start()
    yield
    stop_events.set()
    if event_thread is not None:
        event_thread.join(timeout=2)
    ha_sync.stop_sync_worker()
    scheduler.shutdown(wait=False)



def tidy_existing_data() -> None:
    key = "tidy_names_013_done"
    with db._connect() as conn:
        if conn.execute("SELECT 1 FROM app_state WHERE key=?", (key,)).fetchone():
            return
        for item in conn.execute("SELECT id,name,brand FROM items").fetchall():
            conn.execute(
                "UPDATE items SET name=?,brand=? WHERE id=?",
                (tidy.tidy_name(item["name"]), tidy.tidy_name(item["brand"]) if item["brand"] is not None else None, item["id"]),
            )
        for cached in conn.execute("SELECT barcode,name,brand FROM barcode_cache").fetchall():
            conn.execute(
                "UPDATE barcode_cache SET name=?,brand=? WHERE barcode=?",
                (tidy.tidy_name(cached["name"] or ""), tidy.tidy_name(cached["brand"]) if cached["brand"] is not None else None, cached["barcode"]),
            )
        conn.execute("INSERT INTO app_state (key,value) VALUES (?,?)", (key, "1"))


def _reschedule_notify_job() -> None:
    if not scheduler.running:
        return
    try:
        scheduler.reschedule_job(_NOTIFY_JOB_ID, trigger="cron", hour=settings.get_int("notify_hour"))
        print(
            f"[pantry_tracker] Daily summary at {settings.get_int('notify_hour'):02d}:00 {db.local_zone()}",
            flush=True,
        )
    except JobLookupError:
        return


class _StaticFiles(StaticFiles):
    """The OCR runtime and models (fetched into static/ocr by the Dockerfile,
    so absent under the test client) are around 30 MB and pinned, and
    ocr-worker.js names their version when requesting them - a phone should
    only ever download them once."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if Path(full_path).parent.name == "ocr":
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # ocr-worker.js changes with the app, unlike the pinned files.
            response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="Pantry Tracker", lifespan=lifespan)
_ALLOWED_PEERS = {"172.30.32.2", "127.0.0.1", "::1", "testclient"}


@app.middleware("http")
async def ingress_only(request: Request, call_next):
    peer = request.client.host if request.client else ""
    if peer not in _ALLOWED_PEERS:
        return Response(status_code=403)
    return await call_next(request)

app.mount("/static", _StaticFiles(directory=APP_DIR / "static", check_dir=False), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _display_image_url(barcode: str | None, image_url: str | None) -> str:
    """Prefers a user photo, then a local OFF copy, then its remote URL."""
    if db.valid_barcode(barcode):
        user_photo = db.product_photo_path(barcode)
        if user_photo.exists():
            return f"api/barcodes/{barcode}/photo?v={int(user_photo.stat().st_mtime)}"
        local_image = db.product_image_path(barcode)
        if local_image.exists():
            return f"api/barcodes/{barcode}/photo?v={int(local_image.stat().st_mtime)}"
    return image_url or ""


def _item_view(item: dict, notify_days_before: int | None = None,
               barcode_cache: dict | None = None) -> dict:
    if notify_days_before is None:
        notify_days_before = settings.get_int("notify_days_before")
    bucket = db.bucket_for(item, notify_days_before)
    cached = None
    if item.get("barcode"):
        cached = (barcode_cache or {}).get(item.get("barcode"))
        if cached is None and barcode_cache is None:
            cached = db.get_cached_barcode(item.get("barcode"))
    return {
        **item,
        "bucket": bucket,
        "days_left": db.days_left_for(item),
        "effective_date": db.effective_date(item),
        "group": tidy.food_group(item.get("category", ""), item.get("name", "")),
        "opened_days": item.get("use_within_days") or (cached.get("opened_days") if cached else None),
        "display_image_url": _display_image_url(item.get("barcode"), item.get("image_url")),
        "has_date_photo": bool(item.get("has_date_photo")),
        "date_extraction_pending": bool(item.get("date_extraction_pending")),
        "date_candidates": json.loads(item.get("date_candidates") or "[]"),
    }


def _cache_item_preference(item: dict, **fields) -> None:
    """Stores a location or opened-days choice even when no lookup cache exists yet."""
    barcode = item.get("barcode")
    if not barcode:
        return
    if not db.get_cached_barcode(barcode):
        db.cache_barcode(barcode, {
            "name": item.get("name", ""),
            "brand": item.get("brand", ""),
            "quantity": item.get("quantity", ""),
            "category": item.get("category", ""),
            "image_url": item.get("image_url", ""),
        })
    db.update_cached_barcode(barcode, **fields)


def _valid_ocr_date(value: str) -> str | None:
    """Comes from the browser, so isn't trusted to be a real date."""
    value = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _valid_ocr_candidates(value: str) -> list[dict]:
    try:
        candidates = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(candidates, list) or len(candidates) > 10:
        return []
    valid = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) - {"date", "repaired"}:
            return []
        parsed = _valid_ocr_date(candidate.get("date", ""))
        if not parsed or not isinstance(candidate.get("repaired", False), bool):
            return []
        valid.append({"date": parsed, "repaired": candidate.get("repaired", False)})
    return valid


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Without this, a phone browser tab left open across an add-on rebuild
    # can keep serving its cached copy of the page indefinitely.
    response = templates.TemplateResponse(request, "index.html", {})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/scan")
async def scan(photo: UploadFile = File(...)):
    image_bytes = await photo.read()
    code = await run_in_threadpool(barcode.decode_barcode, image_bytes)
    return {"barcode": code}


@app.get("/api/lookup/{code}")
async def lookup_product(code: str, background_tasks: BackgroundTasks):
    cached = db.get_cached_barcode(code)
    if cached:
        background_tasks.add_task(photos.download_product_image, cached.get("image_url", ""), code)
        return {
            "found": True,
            "cached": True,
            "name": cached["name"],
            "brand": cached["brand"],
            "quantity": cached["quantity"],
            "category": cached["category"],
            "image_url": cached["image_url"],
            "display_image_url": _display_image_url(code, cached["image_url"]),
            **({"location": cached.get("location")} if cached.get("location") else {}),
            **({"opened_days": cached.get("opened_days")} if cached.get("opened_days") else {}),
        }

    try:
        product = await run_in_threadpool(lookup.lookup_barcode, code)
    except lookup.LookupUnavailable:
        return {"found": False, "unavailable": True, "display_image_url": _display_image_url(code, "")}
    if not product:
        return {"found": False, "display_image_url": _display_image_url(code, "")}
    product["name"] = tidy.tidy_name(product.get("name", ""))
    product["brand"] = tidy.tidy_name(product.get("brand", ""))
    db.cache_barcode(code, product)
    background_tasks.add_task(photos.download_product_image, product["image_url"], code)
    return {
        "found": True,
        "cached": False,
        **product,
        "display_image_url": _display_image_url(code, product["image_url"]),
    }


@app.post("/api/barcodes/{code}/photo")
async def upload_product_photo(code: str, photo: UploadFile = File(...)):
    if not db.valid_barcode(code):
        return JSONResponse({"error": "Not a valid barcode."}, status_code=400)
    normalised = await run_in_threadpool(photos.normalise_product_photo, await photo.read())
    if normalised is None:
        return JSONResponse({"error": "That file isn't a readable image."}, status_code=400)
    db.save_product_photo(code, normalised)
    return {"display_image_url": _display_image_url(code, "")}


@app.get("/api/barcodes/{code}/photo")
async def get_product_photo(code: str):
    if not db.valid_barcode(code):
        return JSONResponse({"error": "No photo stored for this barcode."}, status_code=404)
    path = db.product_photo_path(code)
    if not path.exists():
        path = db.product_image_path(code)
    if not path.exists():
        return JSONResponse({"error": "No photo stored for this barcode."}, status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/api/items")
async def list_items():
    items = db.list_items([db.STATUS_ACTIVE])
    notify_days_before = settings.get_int("notify_days_before")
    barcodes = {item.get("barcode") for item in items if item.get("barcode")}
    cache = db.get_cached_barcodes(list(barcodes))
    return {"items": [_item_view(i, notify_days_before, cache) for i in items]}


@app.get("/api/barcodes/{code}/active")
async def active_barcode_match(code: str):
    matches = [item for item in db.list_items([db.STATUS_ACTIVE]) if item.get("barcode") == code]
    if not matches:
        return {"item": None}
    item = max(matches, key=lambda value: (value["created_at"], value["id"]))
    return {"item": _item_view(item)}


@app.get("/api/items/{item_id}")
async def get_single_item(item_id: int):
    """A single item's current state - used by the edit panel to poll for
    a rescan's background extraction finishing, without re-fetching (and
    the dashboard re-rendering) the whole list just to check one item."""
    item = db.get_item(item_id)
    if not item:
        return JSONResponse({"error": "Item not found."}, status_code=404)
    return _item_view(item)


@app.post("/api/parse-date")
async def parse_date(text: str = Form(""), lenient: str = Form("0")):
    """Turns what the browser's on-device OCR read into a date, so there's
    only one date parser to keep right."""
    candidates = vision.date_candidates(text, lenient=lenient == "1")
    return {
        "date": candidates[0]["date"] if candidates else None,
        "confident": bool(candidates) and vision.is_confident(candidates[0]),
        "candidates": [{"date": c["date"], "repaired": c["repaired"]} for c in candidates],
    }


@app.post("/api/client-timing")
async def client_timing(request: Request):
    """Accepts only bounded numeric measurements and a known reader path."""
    form = await request.form()
    values = {}
    for name in _CLIENT_TIMING_FIELDS:
        value = form.get(name)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0 and number < 3600000:
            values[name] = number
    path = form.get("path")
    if not isinstance(path, str) or path not in _CLIENT_TIMING_PATHS:
        return JSONResponse({"error": "Invalid path."}, status_code=400)
    print(
        "Capture timing: "
        f"path={path} open={values.get('open_ms', 0):g} "
        f"stream={values.get('stream_ms', 0):g} "
        f"first={values.get('first_sample_ms', 0):g} "
        f"steady={values.get('steady_ms', 0):g} "
        f"capture={values.get('capture_ms', 0):g} "
        f"ocr_done={values.get('ocr_ms', 0):g} "
        f"read={values.get('read_ms', 0):g} soft={values.get('soft', 0):g} "
        f"read_slow={values.get('read_slow', 0):g} tick_ms={values.get('tick_ms', 0):g}",
        flush=True,
    )
    return {"ok": True}


@app.post("/api/items")
async def add_item(
    background_tasks: BackgroundTasks,
    barcode: str = Form(""),
    name: str = Form(...),
    brand: str = Form(""),
    quantity: str = Form(""),
    category: str = Form(""),
    expiry_date: str = Form(""),
    notes: str = Form(""),
    image_url: str = Form(""),
    source: str = Form("manual"),
    count: int = Form(1),
    location: str = Form(""),
    opened_at: str = Form(""),
    use_within_days: str = Form(""),
    date_photo: UploadFile | None = File(None),
    ocr_date: str = Form(""),
    ocr_raw: str = Form(""),
    ocr_candidates: str = Form("[]"),
):
    name = name.strip()
    if not name:
        return JSONResponse({"error": "A product name is required."}, status_code=400)
    try:
        opened_days = int(use_within_days) if use_within_days else None
    except ValueError:
        opened_days = 0
    if (not 1 <= count <= 99 or location not in {"", "fridge", "cupboard", "freezer"}
            or (opened_days is not None and not 1 <= opened_days <= 365)
            or bool(opened_at) != (opened_days is not None)):
        return JSONResponse({"error": "Invalid count or location."}, status_code=400)
    barcode = barcode.strip()
    brand = brand.strip()
    quantity = quantity.strip()
    category = category.strip()
    image_url = image_url.strip()
    item_id = db.add_item(
        barcode=barcode or None,
        name=name,
        brand=brand,
        quantity=quantity,
        category=category,
        expiry_date=expiry_date.strip() or None,
        notes=notes.strip(),
        image_url=image_url,
        source=source.strip() or "manual",
        count=count,
        location=None,
        opened_at=None,
        use_within_days=None,
    )
    if location or opened_at or opened_days is not None:
        db.transition_item(item_id, location=location, opened=bool(opened_at),
                          opened_at=opened_at or None, use_within_days=opened_days)
    # Whatever was confirmed here - corrected or not - becomes the new
    # cached product for this barcode, so a garbled or wrong Open Food
    # Facts entry (crowd-sourced data is sometimes just bad) only has to
    # be fixed once: the next scan of the same barcode serves this
    # corrected version straight from the cache instead of the original.
    if barcode:
        db.cache_barcode(barcode, {"name": name, "brand": brand, "quantity": quantity, "category": category, "image_url": image_url})
        if location:
            db.update_cached_barcode(barcode, location=location)
        if opened_days is not None:
            db.update_cached_barcode(barcode, opened_days=opened_days)
    # A use-by date photo captured before the item existed (the normal case
    # when adding something new) is attached here instead - reading it is
    # queued in the background rather than making "Add to pantry" wait on
    # the vision model, so scanning the next item can start immediately.
    # only_if_blank=True: an expiry_date already typed into the form before
    # submitting wins over whatever the background read finds later.
    if date_photo is not None:
        image_bytes = await date_photo.read()
        db.save_date_photo(item_id, image_bytes)
        db.start_date_extraction(item_id, _valid_ocr_date(ocr_date), ocr_raw[:_MAX_OCR_RAW_LENGTH] or None, _valid_ocr_candidates(ocr_candidates))
        background_tasks.add_task(_extract_and_update, item_id, only_if_blank=True)
    ha_sync.request_sync()
    return _item_view(db.get_item(item_id))


@app.post("/api/items/{item_id}/date-photo")
async def upload_date_photo(
    item_id: int,
    background_tasks: BackgroundTasks,
    photo: UploadFile = File(...),
    ocr_date: str = Form(""),
    ocr_raw: str = Form(""),
    ocr_candidates: str = Form("[]"),
):
    """Attaches (or replaces) a use-by date photo on an existing item - used
    by the Pantry tab's "Rescan use-by date". Saves the photo and returns
    immediately; extraction itself runs in the background exactly like a
    newly added item's does, so closing the edit panel right away doesn't
    lose it."""
    if not db.get_item(item_id):
        return JSONResponse({"error": "Item not found."}, status_code=404)
    image_bytes = await photo.read()
    db.save_date_photo(item_id, image_bytes)
    db.start_date_extraction(item_id, _valid_ocr_date(ocr_date), ocr_raw[:_MAX_OCR_RAW_LENGTH] or None, _valid_ocr_candidates(ocr_candidates))
    background_tasks.add_task(_extract_and_update, item_id)
    return _item_view(db.get_item(item_id))


@app.get("/api/items/{item_id}/date-photo")
async def get_date_photo(item_id: int):
    """Serves the photo a use-by date was (or is being) read from, so it
    can be compared against the extracted date - best-effort OCR on
    packaging print is always worth a sanity check."""
    path = db.date_photo_path(item_id)
    if not path.exists():
        return JSONResponse({"error": "No photo stored for this item."}, status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/items/{item_id}")
async def edit_item(
    item_id: int,
    name: str = Form(...),
    brand: str = Form(""),
    quantity: str = Form(""),
    category: str = Form(""),
    expiry_date: str = Form(""),
    notes: str = Form(""),
    date_chosen: str = Form(""),
    count: int | None = Form(None),
    location: str | None = Form(None),
    opened_at: str | None = Form(None),
    use_within_days: str | None = Form(None),
):
    name = name.strip()
    if not name:
        return JSONResponse({"error": "A product name is required."}, status_code=400)
    existing = db.get_item(item_id)
    if not existing:
        return JSONResponse({"error": "Item not found."}, status_code=404)
    if ((count is not None and not 1 <= count <= 99)
            or (location is not None and location not in {"", "fridge", "cupboard", "freezer"})):
        return JSONResponse({"error": "Invalid count or location."}, status_code=400)
    try:
        days = int(use_within_days) if use_within_days else None
    except ValueError:
        days = 0
    if use_within_days is not None and days is not None and not 1 <= days <= 365:
        return JSONResponse({"error": "Use within days must be between 1 and 365."}, status_code=400)
    if opened_at is not None and use_within_days is not None and bool(opened_at) != (days is not None):
        return JSONResponse({"error": "Opened date and use-within days must be set together."}, status_code=400)
    brand = brand.strip()
    quantity = quantity.strip()
    category = category.strip()
    expiry_date = expiry_date.strip() or None
    # Saving a different date, or tapping one, is the person settling any
    # doubt about it.
    confidence = {}
    if expiry_date != existing["expiry_date"] or date_chosen == "1":
        confidence = {"date_confidence": None, "date_candidates": None}
    db.update_item(
        item_id,
        name=name,
        brand=brand,
        quantity=quantity,
        category=category,
        expiry_date=expiry_date,
        notes=notes.strip(),
        **({"count": count} if count is not None else {}),
        **confidence,
    )
    if location is not None or opened_at is not None or use_within_days is not None:
        prior_location = existing.get("location") or ""
        new_location = location if location is not None else prior_location
        if opened_at is not None or use_within_days is not None:
            is_opened = bool(opened_at) and (days is not None)
            opened_value = opened_at or None
        else:
            is_opened = None
            opened_value = None
        db.transition_item(item_id, opened=is_opened, location=new_location,
                           use_within_days=days, opened_at=opened_value)
    # Same reasoning as add_item: a correction made here (e.g. fixing a
    # garbled Open Food Facts name after the fact, from the Pantry tab
    # instead of at add-time) should also fix this barcode's cached
    # product, not just this one item.
    if existing["barcode"]:
        db.cache_barcode(
            existing["barcode"],
            {"name": name, "brand": brand, "quantity": quantity, "category": category, "image_url": existing["image_url"] or ""},
        )
        if location:
            db.update_cached_barcode(existing["barcode"], location=location)
        if days is not None:
            db.update_cached_barcode(existing["barcode"], opened_days=days)
    ha_sync.request_sync()
    return _item_view(db.get_item(item_id))


@app.post("/api/items/{item_id}/finish")
async def finish_item(item_id: int, outcome: str = Form(...)):
    if outcome not in {"eaten", "binned"}:
        return JSONResponse({"error": "Invalid outcome."}, status_code=400)
    event_id = db.finish_item(item_id, outcome)
    if event_id is None:
        return JSONResponse({"error": "Item not found or no longer active."}, status_code=404)
    ha_sync.request_sync()
    return {"ok": True, "event_id": event_id}


@app.post("/api/items/{item_id}/opened")
async def set_opened(item_id: int, days: int | None = Form(None), opened: bool = Form(True)):
    if opened and (days is None or not 1 <= days <= 365):
        return JSONResponse({"error": "Use within days must be between 1 and 365."}, status_code=400)
    item = db.transition_item(item_id, opened=opened, use_within_days=days)
    if not item:
        return JSONResponse({"error": "Item not found."}, status_code=404)
    if opened:
        _cache_item_preference(item, opened_days=days)
    ha_sync.request_sync()
    return _item_view(item)


@app.post("/api/items/{item_id}/add-one")
async def add_one(item_id: int):
    item = db.get_item(item_id)
    if not item or item["status"] != db.STATUS_ACTIVE:
        return JSONResponse({"error": "Item not found."}, status_code=404)
    updated = db.add_one(item_id)
    if not updated:
        return JSONResponse({"error": "Item count cannot exceed 99."}, status_code=400)
    ha_sync.request_sync()
    return _item_view(updated)


@app.post("/api/events/{event_id}/undo")
async def undo_event(event_id: int):
    if not db.undo_event(event_id):
        return JSONResponse({"error": "Event not found."}, status_code=404)
    ha_sync.request_sync()
    return {"ok": True}


@app.post("/api/items/{item_id}/delete")
async def delete_item(item_id: int):
    db.delete_item(item_id)
    ha_sync.request_sync()
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    values = settings.all_settings()
    values["vision_configured"] = vision.configured()
    values["ha_sync_configured"] = ha_sync.configured()
    values["last_check_at"] = db.get_state("last_check_at")
    values["last_check_summary"] = db.get_state("last_check_summary")
    return values


@app.get("/api/stats")
async def get_stats():
    return db.stats()


@app.post("/api/settings")
async def save_settings(
    notify_days_before: int = Form(...),
    notify_repeat_hours: int = Form(...),
    notify_hour: int | None = Form(None),
):
    # notify_service isn't settable here - see settings.py's module
    # docstring for why it's add-on-configuration-only.
    setting_values = {
        "notify_days_before": notify_days_before,
        "notify_repeat_hours": notify_repeat_hours,
    }
    if notify_hour is not None:
        setting_values["notify_hour"] = notify_hour
    settings.set_many(setting_values)
    if notify_hour is not None:
        _reschedule_notify_job()
    return await get_settings()


@app.post("/api/check-now")
async def check_now():
    result = await run_in_threadpool(run_check_cycle)
    return {"result": result}


@app.get("/export")
async def export_data():
    all_items = db.list_items()
    notify_days_before = settings.get_int("notify_days_before")
    barcodes = {item.get("barcode") for item in all_items if item.get("barcode")}
    cache = db.get_cached_barcodes(list(barcodes))
    items = [_item_view(i, notify_days_before, cache) for i in all_items]
    with db._connect() as conn:
        events = [dict(row) for row in conn.execute("SELECT * FROM item_events ORDER BY id").fetchall()]
    payload = json.dumps({"items": items, "item_events": events}, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=pantry-tracker-export.json"},
    )
