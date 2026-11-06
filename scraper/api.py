"""HTTP API + tiny web page + built-in scheduler. Run with: python cli.py serve"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import export
from .core.config import load_settings
from .core.db import DB
from .events.pipeline import crawl_lock, run_crawl
from .site import render_events_page

log = logging.getLogger(__name__)
settings = load_settings()
db = DB(settings.db_path)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.crawl_interval_hours > 0:
        threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler").start()
        log.info("Scheduler on: every %s h", settings.crawl_interval_hours)
    yield


app = FastAPI(title="Tech Events Scraper", version="1.0", lifespan=lifespan)


def _scheduler_loop() -> None:
    interval = settings.crawl_interval_hours * 3600
    time.sleep(10)  # let the server come up first
    while True:
        log.info("Scheduled crawl starting")
        try:
            run_crawl(settings)
        except Exception:
            log.exception("Scheduled crawl crashed")
        time.sleep(interval)


def _check_token(authorization: Optional[str]) -> None:
    if not settings.api_token:
        raise HTTPException(403, "Set API_TOKEN to enable manual crawls over HTTP")
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(401, "Bad or missing token")


@app.get("/health")
def health():
    return {"ok": True, "crawl_running": crawl_lock.locked()}


@app.get("/api/events")
def list_events(
    upcoming: bool = True,
    q: str = "",
    city: str = "",
    limit: int = Query(200, le=1000),
    offset: int = 0,
):
    return db.list_events(upcoming=upcoming, q=q, city=city, limit=limit, offset=offset)


@app.get("/api/runs")
def list_runs(limit: int = 50):
    return db.list_runs(limit)


@app.post("/api/crawl", status_code=202)
def trigger_crawl(background: BackgroundTasks, source: Optional[str] = None,
                  authorization: Optional[str] = Header(None)):
    _check_token(authorization)
    if crawl_lock.locked():
        raise HTTPException(409, "A crawl is already running")
    background.add_task(run_crawl, settings, source)
    return {"started": True, "source": source or "all"}


@app.get("/export/events.xlsx")
def export_xlsx(upcoming: bool = True):
    return Response(
        export.to_xlsx_bytes(db, upcoming),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="tech-events.xlsx"'},
    )


@app.get("/export/events.csv")
def export_csv(upcoming: bool = True):
    return Response(
        export.to_csv_text(db, upcoming),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="tech-events.csv"'},
    )


@app.get("/brochures/{brochure_id}")
def get_brochure(brochure_id: int):
    b = db.get_brochure(brochure_id)
    if not b:
        raise HTTPException(404)
    path = (settings.data_dir / b["path"]).resolve()
    if settings.data_dir.resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/", response_class=HTMLResponse)
def index(q: str = "", city: str = ""):
    return render_events_page(db.list_events(upcoming=True, q=q, city=city, limit=500), q, city)
