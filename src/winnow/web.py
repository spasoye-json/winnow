import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from winnow import auth
from winnow.db import connect
from winnow.feed import build_feed
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
    def feed(request: Request):
        conn = connect(db_path)
        try:
            context = build_feed(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(request, "feed.html", context)

    return app
