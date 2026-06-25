import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

import carriers
import db
import ha_sync
import mail_worker
import sync_progress
from providers import seventeentrack, track123

POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "30"))
AUTO_ARCHIVE_AFTER_DAYS = int(os.environ.get("AUTO_ARCHIVE_AFTER_DAYS", "14"))
DISMISS_UNCONFIRMED_AFTER_DAYS = int(os.environ.get("DISMISS_UNCONFIRMED_AFTER_DAYS", "3"))

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
        sync_progress.start_stage("providers")
        sync_progress.add_total(len(refresh_candidates))

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
            mod.register([(p["tracking_number"], p["carrier_name"]) for p in parcels])
            track_info = mod.get_track_info(numbers)
            for parcel in parcels:
                info = track_info.get(parcel["tracking_number"])
                sync_progress.increment()
                if not info:
                    continue

                # The provider's response is the authority on whether this is
                # a real tracking number: a candidate it has never once
                # confirmed (no detected carrier, no movement event) gets
                # auto-dismissed once it's had a fair amount of time to be
                # recognised. `provider_confirmed` is sticky, so a parcel
                # genuinely confirmed in the past can't later be dismissed by
                # a one-off inconclusive check. The grace period is measured
                # from this parcel's *first* check rather than its creation
                # time, so older parcels freshly registered with a provider
                # (or upgrading onto this feature) aren't dismissed on the
                # very first look.
                if (
                    not info["confirmed"]
                    and not parcel["provider_confirmed"]
                    and parcel["first_checked_at"]
                    and DISMISS_UNCONFIRMED_AFTER_DAYS > 0
                    and datetime.now(timezone.utc) - datetime.fromisoformat(parcel["first_checked_at"])
                    >= timedelta(days=DISMISS_UNCONFIRMED_AFTER_DAYS)
                ):
                    db.dismiss_parcel(parcel["id"])
                    continue

                # A pending candidate is auto-confirmed once the provider
                # positively recognises the number (a detected carrier or a
                # real tracking event) - the API is a far stronger signal than
                # our own pattern guess. Until then it only gets a preview
                # (carrier/status text) and stays pending for manual review.
                if parcel["status"] == db.STATUS_PENDING and not info["confirmed"]:
                    new_status = None
                else:
                    new_status = info["status"]
                db.update_tracking_status(
                    parcel["id"],
                    status=new_status,
                    status_detail=info["status_detail"],
                    last_event_time=info["last_event_time"],
                    estimated_delivery=info["estimated_delivery"],
                    carrier_name=info.get("carrier_name"),
                    tracking_provider=provider_name,
                    confirmed=info["confirmed"],
                    events=info.get("events"),
                )

        sync_progress.finish()

    archived = db.auto_archive_delivered(AUTO_ARCHIVE_AFTER_DAYS)

    ha_sync.sync(db.list_parcels())

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


class _StaticCORSMiddleware(BaseHTTPMiddleware):
    """Home Assistant's frontend loads a "JavaScript module" Lovelace
    resource via a cross-origin `import()` (the add-on's direct port vs.
    HA's own frontend port), which browsers block without an explicit
    Access-Control-Allow-Origin header - so without this, the card's script
    never runs and never registers itself, even though the URL loads fine
    from a plain browser navigation (which isn't subject to CORS at all).
    Scoped to `/static` only, rather than a blanket CORSMiddleware, since
    the rest of the app's routes mutate state and don't need this."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response


app = FastAPI(title="Parcel Tracker", lifespan=lifespan)
app.add_middleware(_StaticCORSMiddleware)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# Serves the companion Lovelace card JS. Reached via the add-on's direct
# port (8000/tcp in config.yaml), not its ingress URL - ingress paths are
# session-scoped and can't be used as a stable Lovelace resource URL.
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def _dashboard_context() -> dict:
    pending = db.list_parcels([db.STATUS_PENDING])
    active = db.list_parcels([db.STATUS_ACTIVE, db.STATUS_EXCEPTION])
    delivered = db.list_parcels([db.STATUS_DELIVERED])
    archived = db.list_parcels([db.STATUS_ARCHIVED, db.STATUS_DISMISSED])
    # Lets the add-parcel form warn before silently overwriting a tracking
    # number that's already tracked (the DB's uniqueness constraint is
    # global, not scoped to a status, so a dismissed/archived entry counts
    # as "already exists" too). Escaping "</" guards against breaking out of
    # the <script> tag this gets embedded in if a carrier/description string
    # ever contained it.
    existing_parcels = {
        p["tracking_number"]: {"status": p["status"], "carrier_name": p["carrier_name"]}
        for p in pending + active + delivered + archived
    }
    return {
        "pending": pending,
        "active": active,
        "delivered": delivered,
        "archived": archived,
        "existing_parcels_json": json.dumps(existing_parcels).replace("</", "<\\/"),
        "last_sync_at": db.get_state("last_sync_at"),
        "last_sync_summary": db.get_state("last_sync_summary"),
        "tracking_providers_configured": tracking_providers_configured(),
        "ha_sync_configured": ha_sync.configured(),
        "get_tracking_url": carriers.get_tracking_url,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Without this, a browser tab left open across an add-on rebuild can keep
    # serving its cached copy of this page (and the sync-form JS embedded in
    # it) indefinitely, masking fixes here behind what looks like a UI bug.
    response = templates.TemplateResponse(request, "dashboard.html", _dashboard_context())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/sync")
async def trigger_sync():
    # run_sync_cycle() does blocking IMAP/HTTP I/O - calling it directly here
    # would freeze the single-threaded event loop (and the whole dashboard,
    # not just this request) for as long as the sync takes.
    await run_in_threadpool(run_sync_cycle)
    return RedirectResponse("./", status_code=303)


@app.get("/sync/status")
async def sync_status():
    # Polled by the dashboard while a sync is in flight, to show a live
    # "checked X of Y" count instead of a bare spinner for however long the
    # mail check takes.
    return JSONResponse(sync_progress.get())


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


@app.post("/reset")
async def reset(parcel_id: int = Form(...)):
    db.reset_parcel(parcel_id)
    return RedirectResponse("./", status_code=303)


@app.post("/admin/reset-all")
async def admin_reset_all(confirm_text: str = Form("")):
    # A second, server-side check behind the page's own confirmation prompt -
    # this wipes every parcel and all mail-sync bookkeeping with no undo, so
    # a bare POST (e.g. a stray retry, or anyone bypassing the page's JS)
    # shouldn't be enough on its own to trigger it.
    if confirm_text.strip() == "RESET":
        db.reset_all_data()
    return RedirectResponse("./", status_code=303)


@app.get("/api/parcels")
async def api_parcels():
    return JSONResponse({"parcels": db.list_parcels()})


@app.get("/export")
async def export_data():
    payload = json.dumps({"parcels": db.list_parcels()}, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=parcel-tracker-export.json"},
    )
