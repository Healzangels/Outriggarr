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
from fastapi.staticfiles import StaticFiles

from outriggarr import __version__
from outriggarr.api.connections import router as connections_router
from outriggarr.api.health import router as health_router
from outriggarr.api.jobs import router as jobs_router
from outriggarr.api.library import router as library_router
from outriggarr.api.matches import router as matches_router
from outriggarr.api.settings import router as settings_router
from outriggarr.api.subscriptions import router as subscriptions_router
from outriggarr.arr import ArrFactory, make_client
from outriggarr.db.session import make_engine, make_session_factory, run_migrations
from outriggarr.notify import AppriseNotifier, Notifier
from outriggarr.settings import Settings, apprise_urls, ytdlp_options
from outriggarr.source import VideoSource, YtDlpSource
from outriggarr.web.middleware import SameOriginGuard
from outriggarr.web.pages import STATIC_DIR
from outriggarr.web.pages import router as pages_router
from outriggarr.worker.runner import RunnerDeps, run_worker
from outriggarr.worker.scheduler import run_scheduler

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    start_worker: bool = True,
    arr_factory: ArrFactory | None = None,
    source: VideoSource | None = None,
    notifier: Notifier | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    source_given = source
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
        if source_given is None:
            sf = app.state.session_factory

            def _extra_opts() -> dict:
                with sf() as s:
                    return ytdlp_options(s)

            source = YtDlpSource(extra_opts=_extra_opts, pot_server_home=settings.pot_server_home)
        else:
            source = source_given
        app.state.source = source
        sf_notify = app.state.session_factory

        def _urls() -> list[str]:
            with sf_notify() as s:
                return apprise_urls(s)

        app.state.runner_deps = RunnerDeps(
            session_factory=app.state.session_factory,
            arr_factory=app.state.arr_factory,
            source=source,
            staging_dir=settings.staging_dir,
            notifier=notifier or AppriseNotifier(_urls),
            lock_dir=settings.config_dir,
        )

        stop = asyncio.Event()
        task = None
        scheduler_task = None
        settings.staging_dir.mkdir(parents=True, exist_ok=True)
        if start_worker:
            deps = app.state.runner_deps
            task = asyncio.create_task(run_worker(deps, stop))
            scheduler_task = asyncio.create_task(run_scheduler(deps, stop))
        app.state.background_tasks = {"worker": task, "scheduler": scheduler_task}
        log.info("outriggarr %s ready (db=%s)", __version__, settings.database_url)
        try:
            yield
        finally:
            stop.set()
            pending = [t for t in (task, scheduler_task) if t is not None]
            if pending:
                results = await asyncio.gather(*pending, return_exceptions=True)
                for name, r in zip(("worker", "scheduler"), results, strict=False):
                    if isinstance(r, BaseException):
                        log.error("%s task ended with %r", name, r)
            await app.state.http.aclose()
            engine.dispose()

    app = FastAPI(title="Outriggarr", version=__version__, lifespan=lifespan)
    app.add_middleware(SameOriginGuard)
    app.include_router(health_router)
    app.include_router(connections_router)
    app.include_router(jobs_router)
    app.include_router(library_router)
    app.include_router(matches_router)
    app.include_router(subscriptions_router)
    app.include_router(settings_router)
    app.include_router(pages_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
