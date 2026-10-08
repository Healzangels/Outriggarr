"""App factory. `uvicorn outriggarr.main:app` for dev; the Dockerfile CMD does the same.

Shutdown: uvicorn turns SIGTERM/SIGINT into a lifespan exit, which sets the worker's
stop event and awaits the task, so the container stops cleanly under `docker stop`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from outriggarr import __version__
from outriggarr.api.connections import router as connections_router
from outriggarr.api.dates import router as dates_router
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
from outriggarr.source import VideoSource, YtDlpSource, pot_provider_probe
from outriggarr.web.middleware import SameOriginGuard, StaticCacheHeaders
from outriggarr.web.pages import STATIC_DIR
from outriggarr.web.pages import router as pages_router
from outriggarr.worker.runner import RunnerDeps, acquire_instance_lock, run_worker
from outriggarr.worker.scheduler import run_scheduler

log = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILE_BYTES = 2_000_000  # five files of this: a month of a busy install, ten megabytes at most
LOG_FILE_KEEP = 4


def attach_file_log(config_dir: Path) -> Path | None:
    """Keep the log under the config dir as well as on stdout. A container's stdout log
    dies with the container, so every redeploy wiped the week's warnings before anyone
    read them (an audit found four hours of log where twelve days were asked for).
    Rotating, so it never grows past LOG_FILE_KEEP + 1 files of LOG_FILE_BYTES; a dir
    that cannot be written leaves stdout as the only log, with a warning, not a crash."""
    root = logging.getLogger()
    for old in list(root.handlers):  # one app per process; tests make several
        if old.get_name() == "outriggarr-file":
            root.removeHandler(old)
            old.close()
    path = config_dir / "outriggarr.log"
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_KEEP)
    except OSError as exc:
        log.warning("not keeping a log file under %s: %s", config_dir, exc)
        return None
    handler.set_name("outriggarr-file")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    return path


def _log_task_death(name: str, task: asyncio.Task) -> None:
    """The traceback of a loop that died, when it dies (health and the pages already
    say it stopped; without this the log only learned why at shutdown)."""
    if not task.cancelled() and task.exception() is not None:
        log.error("%s task died", name, exc_info=task.exception())


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
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT)
    # httpx logs every 200 OK at INFO: two thirds of a day's log said Sonarr answered.
    # Failures still reach the job and the scan verbatim; the app logs its own listings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    attach_file_log(settings.config_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        # The lock comes before the migrations: a second instance on this database must
        # not upgrade the schema under the one that is running it.
        lock = acquire_instance_lock(settings.config_dir) if start_worker else None
        if start_worker and lock is None:
            log.error(
                "another Outriggarr instance already runs this database; "
                "this one serves the pages only (no worker, no scheduler, no migrations)"
            )
            app.state.worker_note = (
                "Another Outriggarr instance holds this database, so this one serves "
                "the pages only: nothing downloads or scans from here."
            )
            app.state.runner_deps.page_only = True
        else:
            run_migrations(settings.database_url)
        engine = make_engine(settings.database_url)
        # downloads block a thread for hours each; the default pool (cpu + 4) would let
        # them starve scans, notifications and Grab on a small host
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=32, thread_name_prefix="outriggarr")
        )
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

        # the PO-token plugin's own check, once: it takes a second and does not change
        app.state.pot_probe = await asyncio.to_thread(pot_provider_probe, settings.pot_server_home)
        if app.state.pot_probe:
            log.warning("PO-token provider not usable: %s", app.state.pot_probe)
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
        app.state.tasks = set()  # rechecks and date fetches, owned so shutdown can await them
        settings.staging_dir.mkdir(parents=True, exist_ok=True)
        if start_worker and lock is not None:
            deps = app.state.runner_deps
            # one lock for both loops: a second instance on the same database must not
            # scan and queue jobs either, not just refrain from downloading them
            task = asyncio.create_task(run_worker(deps, stop, lock=lock))
            scheduler_task = asyncio.create_task(run_scheduler(deps, stop))
            for name, t in (("worker", task), ("scheduler", scheduler_task)):
                t.add_done_callback(functools.partial(_log_task_death, name))
        app.state.background_tasks = {"worker": task, "scheduler": scheduler_task}
        log.info("outriggarr %s ready (db=%s)", __version__, settings.database_url)
        try:
            yield
        finally:
            stop.set()
            # background fetches first: cancel, then await, so what they fetched is kept
            fetches = list(app.state.tasks)
            for t in fetches:
                t.cancel()
            if fetches:
                await asyncio.gather(*fetches, return_exceptions=True)
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
    app.add_middleware(StaticCacheHeaders)
    # outermost, so pages, partials and the static bundle all travel compressed: Activity's
    # 200 rows are 240 KB plain and 20 KB gzipped, Pico's stylesheet 83 KB and 12 KB
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.include_router(health_router)
    app.include_router(connections_router)
    app.include_router(jobs_router)
    app.include_router(library_router)
    app.include_router(matches_router)
    app.include_router(dates_router)
    app.include_router(subscriptions_router)
    app.include_router(settings_router)
    app.include_router(pages_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
