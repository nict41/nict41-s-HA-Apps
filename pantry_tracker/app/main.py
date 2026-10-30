"""Pantry Tracker - scan a grocery barcode, look it up, read its use-by date
from a photo, and get notified before it expires. See README.md for the
full workflow.
"""

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, Form, UploadFile, File, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

import barcode
import db
import ha_sync
import lookup
import photos
import settings
import vision

_CHECK_JOB_ID = "check_cycle"
_IMAGE_BACKFILL_STATE_KEY = "image_backfill_done"
# Open Food Facts asks clients to keep product lookups well under 100/minute.
_BACKFILL_LOOKUP_DELAY_SECONDS = 1

APP_DIR = Path(__file__).resolve().parent

db.init_db()

scheduler = BackgroundScheduler()


def run_check_cycle() -> None:
    """Re-evaluates every active item's urgency, sends any notifications
    that are due, and pushes current state to Home Assistant. Runs on a
    schedule (`check_interval_hours`) and can also be triggered on demand
    from the dashboard's "Check now" action."""
    items = db.list_items([db.STATUS_ACTIVE])
    notify_days_before = settings.get_int("notify_days_before")

    notified = 0
    for item in items:
        bucket = db.bucket_for(item["expiry_date"], notify_days_before)
        if ha_sync.notify_if_due(item, bucket):
            notified += 1

    # Re-fetch: notify_if_due() may have updated last_notified_* on some
    # items, and the summary/entity sync doesn't need that field, but
    # keeping the list fresh avoids syncing stale in-memory copies.
    items = db.list_items([db.STATUS_ACTIVE])
    ha_sync.sync(items)

    db.set_state("last_check_at", db.now_iso())
    db.set_state("last_check_summary", f"checked {len(items)} item(s), sent {notified} notification(s)")


def _sync_now() -> None:
    """A lighter sync for immediate feedback after a dashboard mutation -
    pushes current Home Assistant sensor state without touching
    notifications, which stay governed by the scheduled/manual check cycle."""
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
    result = vision.extract_use_by_date(path.read_bytes())
    if not db.get_item(item_id):
        return  # deleted while extraction was running
    db.finish_date_extraction(item_id, result.get("date"), result.get("raw") or "", only_if_blank=only_if_blank)
    _sync_now()


def run_image_backfill() -> None:
    """Fills in product images for items added before 0.6.0, when the add
    form never submitted one. Runs once: a lookup that fails or finds
    nothing leaves the flag unset so it's retried on the next start, since
    "not found" and "Open Food Facts was unreachable" look the same here."""
    if db.get_state(_IMAGE_BACKFILL_STATE_KEY):
        return
    complete = True
    for barcode in db.barcodes_missing_image():
        cached = db.get_cached_barcode(barcode)
        image_url = cached["image_url"] if cached else ""
        if not image_url:
            product = lookup.lookup_barcode(barcode)
            time.sleep(_BACKFILL_LOOKUP_DELAY_SECONDS)
            if product is None:
                complete = False
                continue
            image_url = product["image_url"]
        if image_url:
            db.apply_barcode_image(barcode, image_url)
    if complete:
        db.set_state(_IMAGE_BACKFILL_STATE_KEY, db.now_iso())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(
        run_check_cycle,
        "interval",
        hours=settings.get_int("check_interval_hours"),
        next_run_time=datetime.now(),
        id=_CHECK_JOB_ID,
    )
    scheduler.add_job(run_image_backfill, next_run_time=datetime.now())
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


def _reschedule_check_job() -> None:
    """Applies a changed check interval to the running scheduler immediately,
    without an add-on restart. A no-op under the test client, which doesn't
    run the lifespan."""
    if not scheduler.running:
        return
    try:
        scheduler.reschedule_job(_CHECK_JOB_ID, trigger="interval", hours=settings.get_int("check_interval_hours"))
    except JobLookupError:
        pass


app = FastAPI(title="Pantry Tracker", lifespan=lifespan)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _display_image_url(barcode: str | None, image_url: str | None) -> str:
    """What to actually show for a product: a photo taken in the app wins
    over the Open Food Facts one, which often doesn't exist at all for
    own-brand items. Relative, so it works under ingress and direct access."""
    if db.valid_barcode(barcode):
        path = db.product_photo_path(barcode)
        if path.exists():
            return f"api/barcodes/{barcode}/photo?v={int(path.stat().st_mtime)}"
    return image_url or ""


def _item_view(item: dict) -> dict:
    notify_days_before = settings.get_int("notify_days_before")
    bucket = db.bucket_for(item["expiry_date"], notify_days_before)
    return {
        **item,
        "bucket": bucket,
        "days_left": db.days_left_for(item["expiry_date"]),
        "display_image_url": _display_image_url(item.get("barcode"), item.get("image_url")),
        "has_date_photo": bool(item.get("has_date_photo")),
        "date_extraction_pending": bool(item.get("date_extraction_pending")),
    }


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
async def lookup_product(code: str):
    cached = db.get_cached_barcode(code)
    if cached:
        return {
            "found": True,
            "cached": True,
            "name": cached["name"],
            "brand": cached["brand"],
            "quantity": cached["quantity"],
            "category": cached["category"],
            "image_url": cached["image_url"],
            "display_image_url": _display_image_url(code, cached["image_url"]),
        }

    product = await run_in_threadpool(lookup.lookup_barcode, code)
    if not product:
        return {"found": False, "display_image_url": _display_image_url(code, "")}
    db.cache_barcode(code, product)
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
    if not db.valid_barcode(code) or not db.product_photo_path(code).exists():
        return JSONResponse({"error": "No photo stored for this barcode."}, status_code=404)
    return FileResponse(db.product_photo_path(code), media_type="image/jpeg")


@app.get("/api/vision-configured")
async def vision_configured():
    return {"configured": vision.configured()}


@app.get("/api/items")
async def list_items():
    items = db.list_items([db.STATUS_ACTIVE])
    return {"items": [_item_view(i) for i in items]}


@app.get("/api/items/{item_id}")
async def get_single_item(item_id: int):
    """A single item's current state - used by the edit panel to poll for
    a rescan's background extraction finishing, without re-fetching (and
    the dashboard re-rendering) the whole list just to check one item."""
    item = db.get_item(item_id)
    if not item:
        return JSONResponse({"error": "Item not found."}, status_code=404)
    return _item_view(item)


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
    date_photo: UploadFile | None = File(None),
):
    name = name.strip()
    if not name:
        return JSONResponse({"error": "A product name is required."}, status_code=400)
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
    )
    # Whatever was confirmed here - corrected or not - becomes the new
    # cached product for this barcode, so a garbled or wrong Open Food
    # Facts entry (crowd-sourced data is sometimes just bad) only has to
    # be fixed once: the next scan of the same barcode serves this
    # corrected version straight from the cache instead of the original.
    if barcode:
        db.cache_barcode(barcode, {"name": name, "brand": brand, "quantity": quantity, "category": category, "image_url": image_url})
    # A use-by date photo captured before the item existed (the normal case
    # when adding something new) is attached here instead - reading it is
    # queued in the background rather than making "Add to pantry" wait on
    # the vision model, so scanning the next item can start immediately.
    # only_if_blank=True: an expiry_date already typed into the form before
    # submitting wins over whatever the background read finds later.
    if date_photo is not None:
        image_bytes = await date_photo.read()
        db.save_date_photo(item_id, image_bytes)
        db.start_date_extraction(item_id)
        background_tasks.add_task(_extract_and_update, item_id, only_if_blank=True)
    await run_in_threadpool(_sync_now)
    return _item_view(db.get_item(item_id))


@app.post("/api/items/{item_id}/date-photo")
async def upload_date_photo(item_id: int, background_tasks: BackgroundTasks, photo: UploadFile = File(...)):
    """Attaches (or replaces) a use-by date photo on an existing item - used
    by the Pantry tab's "Rescan use-by date". Saves the photo and returns
    immediately; extraction itself runs in the background exactly like a
    newly added item's does, so closing the edit panel right away doesn't
    lose it."""
    if not db.get_item(item_id):
        return JSONResponse({"error": "Item not found."}, status_code=404)
    image_bytes = await photo.read()
    db.save_date_photo(item_id, image_bytes)
    db.start_date_extraction(item_id)
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
):
    name = name.strip()
    if not name:
        return JSONResponse({"error": "A product name is required."}, status_code=400)
    existing = db.get_item(item_id)
    if not existing:
        return JSONResponse({"error": "Item not found."}, status_code=404)
    brand = brand.strip()
    quantity = quantity.strip()
    category = category.strip()
    db.update_item(
        item_id,
        name=name,
        brand=brand,
        quantity=quantity,
        category=category,
        expiry_date=expiry_date.strip() or None,
        notes=notes.strip(),
    )
    # Same reasoning as add_item: a correction made here (e.g. fixing a
    # garbled Open Food Facts name after the fact, from the Pantry tab
    # instead of at add-time) should also fix this barcode's cached
    # product, not just this one item.
    if existing["barcode"]:
        db.cache_barcode(
            existing["barcode"],
            {"name": name, "brand": brand, "quantity": quantity, "category": category, "image_url": existing["image_url"] or ""},
        )
    await run_in_threadpool(_sync_now)
    return _item_view(db.get_item(item_id))


@app.post("/api/items/{item_id}/consume")
async def consume_item(item_id: int):
    if not db.get_item(item_id):
        return JSONResponse({"error": "Item not found."}, status_code=404)
    db.consume_item(item_id)
    await run_in_threadpool(_sync_now)
    return {"ok": True}


@app.post("/api/items/{item_id}/delete")
async def delete_item(item_id: int):
    db.delete_item(item_id)
    await run_in_threadpool(_sync_now)
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    values = settings.all_settings()
    values["vision_configured"] = vision.configured()
    values["ha_sync_configured"] = ha_sync.configured()
    values["last_check_at"] = db.get_state("last_check_at")
    values["last_check_summary"] = db.get_state("last_check_summary")
    return values


@app.post("/api/settings")
async def save_settings(
    notify_days_before: int = Form(...),
    notify_repeat_hours: int = Form(...),
    check_interval_hours: int = Form(...),
):
    # notify_service isn't settable here - see settings.py's module
    # docstring for why it's add-on-configuration-only.
    settings.set_many(
        {
            "notify_days_before": notify_days_before,
            "notify_repeat_hours": notify_repeat_hours,
            "check_interval_hours": check_interval_hours,
        }
    )
    _reschedule_check_job()
    return await get_settings()


@app.post("/api/check-now")
async def check_now():
    await run_in_threadpool(run_check_cycle)
    return {"ok": True, "summary": db.get_state("last_check_summary")}


@app.get("/export")
async def export_data():
    items = [_item_view(i) for i in db.list_items()]
    payload = json.dumps({"items": items}, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=pantry-tracker-export.json"},
    )
