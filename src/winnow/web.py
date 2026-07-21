import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from winnow import auth
from winnow.db import connect
from winnow.feed import (
    build_detail,
    build_feed,
    build_settings,
    record_verdict,
    save_settings,
)
from winnow.scheduler import due_check_loop, tick
from winnow.youtube import build_client

logger = logging.getLogger("winnow.scheduler")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def run_due_check(db_path, client_secrets_path):
    conn = connect(db_path)
    try:
        client = _build_client(conn, client_secrets_path)
        if client is None:
            logger.info("skipping due-check: no connected google account")
            return
        tick(conn, client)
    finally:
        conn.close()


def _build_client(conn, client_secrets_path):
    try:
        client_config = auth.load_client_config(client_secrets_path)
    except FileNotFoundError:
        return None
    creds = auth.load_credentials(conn, client_config)
    if creds is None:
        return None
    return build_client(creds)


def create_app(db_path, client_secrets_path):
    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def on_tick():
            try:
                await asyncio.to_thread(run_due_check, db_path, client_secrets_path)
            except Exception:
                logger.exception("due-check tick failed")

        loop = asyncio.create_task(due_check_loop(on_tick, stop))
        try:
            yield
        finally:
            stop.set()
            await loop

    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")),
              name="static")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def feed(request: Request, channel: str | None = None,
             since: str | None = None, until: str | None = None):
        conn = connect(db_path)
        try:
            context = build_feed(conn, channel=channel, since=since, until=until)
        finally:
            conn.close()
        return templates.TemplateResponse(request, "feed.html", context)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        conn = connect(db_path)
        try:
            context = build_settings(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(request, "settings.html", context)

    @app.post("/settings")
    def update_settings(
        threshold: float = Form(...),
        info_density: float = Form(...),
        originality: float = Form(...),
        clickbait_gap: float = Form(...),
        padding: float = Form(...),
        depth: float = Form(...),
        production: float = Form(...),
    ):
        weights = {
            "info_density": info_density,
            "originality": originality,
            "clickbait_gap": clickbait_gap,
            "padding": padding,
            "depth": depth,
            "production": production,
        }
        conn = connect(db_path)
        try:
            save_settings(conn, threshold, {k: v / 100 for k, v in weights.items()})
        finally:
            conn.close()
        return RedirectResponse("/settings", status_code=303)

    @app.get("/video/{yt_video_id}", response_class=HTMLResponse)
    def video_detail(request: Request, yt_video_id: str):
        conn = connect(db_path)
        try:
            detail = build_detail(conn, yt_video_id)
        finally:
            conn.close()
        if detail is None:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(request, "detail.html", detail)

    @app.post("/video/{yt_video_id}/verdict", response_class=HTMLResponse)
    def video_verdict(request: Request, yt_video_id: str,
                      verdict: Literal["great", "slop"] = Form(...)):
        conn = connect(db_path)
        try:
            new = record_verdict(conn, yt_video_id, verdict)
        except LookupError as exc:
            raise HTTPException(status_code=404) from exc
        finally:
            conn.close()
        return templates.TemplateResponse(request, "verdict_snippet.html", {
            "verdict_url": f"/video/{yt_video_id}/verdict",
            "verdict": new,
        })

    return app
