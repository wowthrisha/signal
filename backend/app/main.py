"""Signal API — entry point.

Two surfaces and nothing else: the JSON digest under `/api`, and one static
page at `/` that renders it. The page is hand-written HTML served straight off
disk — there is no build step, no bundler and no node_modules, so "run the
container, open the port" is the whole demo path.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import digest as digest_api
from app.api import health as health_api
from app.api import watchlist as watchlist_api

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

app = FastAPI(title="Signal", version="0.1.0")
app.include_router(digest_api.router)
app.include_router(health_api.router)
app.include_router(watchlist_api.router)


@app.on_event("startup")
def seed_demo_watchlist() -> None:
    """Give the demo user a watchlist if it has none.

    Failure here is logged and swallowed on purpose: the API must still come up
    when the database is empty or still initialising, and `/api/digest` seeds
    lazily anyway. A container that refuses to start because a demo fixture is
    missing is worse than one that serves an empty funnel.
    """
    try:
        with digest_api.connect() as conn:
            n = digest_api.seed_watchlist(conn)
        log.info("demo watchlist: %d instruments", n)
    except Exception as exc:  # noqa: BLE001 - startup must not be fatal
        log.warning("watchlist seed skipped: %s", exc)


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_HTML, media_type="text/html")
