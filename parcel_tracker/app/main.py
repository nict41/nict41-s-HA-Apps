import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import carriers
import db
import mail_worker
from providers import seventeentrack, track123

POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "30"))
AUTO_ARCHIVE_AFTER_DAYS = int(os.environ.get("AUTO_ARCHIVE_AFTER_DAYS", "14"))

APP_DIR = Path(__file__).resolve().parent

db.init_db()

scheduler = BackgroundScheduler()

# Preference order for numbers that have never been registered with either
# provider. Track123's free tier renews monthly while 17track's is a
# one-time allowance, so new candidates draw from the renewing one first.
# A parcel's `tracking_provider` is sticky once set, so a number already
# registered elsewhere keeps using that provider rather than being
# re-registered (and re-charged against quota) on the other one.
_TRACKING_PROVIDERS = [("track123", track123), ("17track", seventeentrack)]


def tracking_providers_configured() -> bool:
    return any(mod.configured() for _, mod in _TRACKING_PROVIDERS)


def run_sync_cycle() -> None:
    sync_result = mail_worker.sync_mailbox()

    refresh_candidates = db.parcels_needing_refresh()
    active_providers = [(name, mod) for name, mod in _TRACKING_PROVIDERS if mod.configured()]
    if refresh_candidates and active_providers:
        by_provider: dict[str, list[dict]] = {}
        for parcel in refresh_candidates:
            provider_name = next(
                (name for name, _ in active_providers if name == parcel["tracking_provider"]),
                active_providers[0][0],
            )
            by_provider.setdefault(provider_name, []).append(parcel)

        providers_by_name = dict(active_providers)
        for provider_name, parcels in by_provider.items():
            mod = providers_by_name[provider_name]
            numbers = [p["tracking_number"] for p in parcels]
            mod.register(numbers)
            track_info = mod.get_track_info(numbers)
            for parcel in parcels:
                info = track_info.get(parcel["tracking_number"])
                if not info:
                    continue
                # Pending candidates only get a preview (carrier/status
                # text) - their lifecycle status stays "pending" until the
                # user explicitly confirms or dismisses it.
                new_status = None if parcel["status"] == db.STATUS_PENDING else info["status"]
                db.update_tracking_status(
                    parcel["id"],
                    status=new_status,
                    status_detail=info["status_detail"],
                    last_event_time=info["last_event_time"],
                    estimated_delivery=info["estimated_delivery"],
                    carrier_name=info.get("carrier_name"),
                    tracking_provider=provider_name,
                )

    archived = db.auto_archive_delivered(AUTO_ARCHIVE_AFTER_DAYS)

    db.set_state("last_sync_at", db.now_iso())
    db.set_state(
        "last_sync_summary",
        f"scanned {sync_result['scanned']} email(s), "
        f"found {sync_result['new_candidates']} candidate(s), "
        f"archived {archived} delivered parcel(s)"
        + (f" - mail error: {sync_result['error']}" if not sync_result["ok"] else ""),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(run_sync_cycle, "interval", minutes=POLL_INTERVAL_MINUTES, next_run_time=datetime.now())
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Parcel Tracker", lifespan=lifespan)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _dashboard_context() -> dict:
    pending = db.list_parcels([db.STATUS_PENDING])
    active = db.list_parcels([db.STATUS_ACTIVE, db.STATUS_EXCEPTION])
    delivered = db.list_parcels([db.STATUS_DELIVERED])
    archived = db.list_parcels([db.STATUS_ARCHIVED, db.STATUS_DISMISSED])
    return {
        "pending": pending,
        "active": active,
        "delivered": delivered,
        "archived": archived,
        "last_sync_at": db.get_state("last_sync_at"),
        "last_sync_summary": db.get_state("last_sync_summary"),
        "tracking_providers_configured": tracking_providers_configured(),
        "get_tracking_url": carriers.get_tracking_url,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_context())


@app.post("/sync")
async def trigger_sync():
    run_sync_cycle()
    return RedirectResponse("./", status_code=303)


@app.post("/add")
async def add_parcel(
    tracking_number: str = Form(...),
    carrier_name: str = Form(""),
    description: str = Form(""),
):
    tracking_number = tracking_number.strip()
    if tracking_number:
        db.upsert_parcel(
            tracking_number=tracking_number,
            carrier_name=carrier_name.strip() or "Unknown",
            description=description.strip() or "Manually added",
            confidence=1.0,
            source_message_id=None,
            initial_status=db.STATUS_ACTIVE,
        )
    return RedirectResponse("./", status_code=303)


@app.post("/confirm")
async def confirm(parcel_id: int = Form(...)):
    db.confirm_parcel(parcel_id)
    return RedirectResponse("./", status_code=303)


@app.post("/dismiss")
async def dismiss(parcel_id: int = Form(...)):
    db.dismiss_parcel(parcel_id)
    return RedirectResponse("./", status_code=303)


@app.post("/archive")
async def archive(parcel_id: int = Form(...)):
    db.archive_parcel(parcel_id)
    return RedirectResponse("./", status_code=303)


@app.post("/delete")
async def delete(parcel_id: int = Form(...)):
    db.delete_parcel(parcel_id)
    return RedirectResponse("./", status_code=303)


@app.get("/api/parcels")
async def api_parcels():
    return JSONResponse({"parcels": db.list_parcels()})
