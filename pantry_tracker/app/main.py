"""Pantry Tracker - scan a grocery barcode, look it up, read its use-by date
from a photo, and get notified before it expires. See README.md for the
full workflow.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

import barcode
import db
import ha_sync
import lookup
import settings
import vision

_CHECK_JOB_ID = "check_cycle"

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(
        run_check_cycle,
        "interval",
        hours=settings.get_int("check_interval_hours"),
        next_run_time=datetime.now(),
        id=_CHECK_JOB_ID,
    )
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


def _item_view(item: dict) -> dict:
    notify_days_before = settings.get_int("notify_days_before")
    bucket = db.bucket_for(item["expiry_date"], notify_days_before)
    return {**item, "bucket": bucket, "days_left": db.days_left_for(item["expiry_date"])}


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
    product = await run_in_threadpool(lookup.lookup_barcode, code)
    if not product:
        return {"found": False}
    return {"found": True, **product}


@app.post("/api/extract-date")
async def extract_date(photo: UploadFile = File(...)):
    image_bytes = await photo.read()
    return await run_in_threadpool(vision.extract_use_by_date, image_bytes)


@app.get("/api/vision-configured")
async def vision_configured():
    return {"configured": vision.configured()}


@app.get("/api/items")
async def list_items():
    items = db.list_items([db.STATUS_ACTIVE])
    return {"items": [_item_view(i) for i in items]}


@app.post("/api/items")
async def add_item(
    barcode: str = Form(""),
    name: str = Form(...),
    brand: str = Form(""),
    quantity: str = Form(""),
    category: str = Form(""),
    expiry_date: str = Form(""),
    notes: str = Form(""),
    image_url: str = Form(""),
    source: str = Form("manual"),
):
    name = name.strip()
    if not name:
        return JSONResponse({"error": "A product name is required."}, status_code=400)
    item_id = db.add_item(
        barcode=barcode.strip() or None,
        name=name,
        brand=brand.strip(),
        quantity=quantity.strip(),
        category=category.strip(),
        expiry_date=expiry_date.strip() or None,
        notes=notes.strip(),
        image_url=image_url.strip(),
        source=source.strip() or "manual",
    )
    await run_in_threadpool(_sync_now)
    return _item_view(db.get_item(item_id))


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
    if not db.get_item(item_id):
        return JSONResponse({"error": "Item not found."}, status_code=404)
    db.update_item(
        item_id,
        name=name,
        brand=brand.strip(),
        quantity=quantity.strip(),
        category=category.strip(),
        expiry_date=expiry_date.strip() or None,
        notes=notes.strip(),
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
    notify_service: str = Form(""),
):
    settings.set_many(
        {
            "notify_days_before": notify_days_before,
            "notify_repeat_hours": notify_repeat_hours,
            "check_interval_hours": check_interval_hours,
            "notify_service": notify_service,
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
