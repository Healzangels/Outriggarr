"""App factory. `uvicorn outriggarr.main:app` for dev; the Dockerfile CMD does the same.

Shutdown: uvicorn turns SIGTERM/SIGINT into a lifespan exit, which sets the worker's
stop event and awaits the task, so the container stops cleanly under `docker stop`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from outriggarr import __version__
from outriggarr.api.connections import router as connections_router
from outriggarr.api.health import router as health_router
from outriggarr.arr import ArrFactory, make_client
from outriggarr.db.session import make_engine, make_session_factory, run_migrations
from outriggarr.settings import Settings
from outriggarr.worker.runner import run_worker

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    start_worker: bool = True,
    arr_factory: ArrFactory | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        run_migrations(settings.database_url)
        engine = make_engine(settings.database_url)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        app.state.arr_factory = arr_factory or (lambda conn: make_client(conn, app.state.http))

        stop = asyncio.Event()
        task = (
            asyncio.create_task(run_worker(app.state.session_factory, stop))
            if start_worker
            else None
        )
        log.info("outriggarr %s ready (db=%s)", __version__, settings.database_url)
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            await app.state.http.aclose()
            engine.dispose()

    app = FastAPI(title="Outriggarr", version=__version__, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(connections_router)
    return app


app = create_app()
