"""Upload dates on demand. Flat listings carry no upload date; a scheduled scan fetches
at most a handful per pass, newest first, so a 1500-video channel takes a day to date.
This fetches every undated listed video of one subscription once, in the background
with progress, and caches the dates for good — the next scan can then pair by date."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.orm import Session

from outriggarr.db.models import Subscription, utcnow
from outriggarr.settings import get_setting
from outriggarr.source import SourceError
from outriggarr.worker.scheduler import _date_known, _remember_date, list_source_videos

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/subscriptions", tags=["dates"])

DATE_FETCH_MAX = 3000  # videos per run
DATE_FETCH_PARALLEL = 4
COMMIT_EVERY = 10


@dataclass
class DateFetchProgress:
    subscription_id: int = 0
    running: bool = False
    total: int = 0  # undated videos to fetch
    done: int = 0
    dated: int = 0
    unknown: int = 0  # fetched, but the source gave no date
    error_count: int = 0
    first_error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure: str | None = None
    reported: bool = False  # the finished summary has been shown once

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("started_at", "finished_at"):
            d[k] = d[k].isoformat() if d[k] else None
        d["summary"] = self.summary()
        return d

    def summary(self) -> str:
        if self.failure:
            return f"Date fetch failed: {self.failure}"
        if self.running:
            return (
                f"Fetching upload dates: {self.done} of {self.total} videos… "
                "leaving this page will not stop it."
            )
        if self.finished_at is None:
            return ""
        if self.total == 0:
            return "Every listed video already has its date."
        text = f"Fetched dates for {self.dated} of {self.total} videos"
        if self.unknown:
            text += f" ({self.unknown} carry none)"
        if self.error_count:
            text += f"; {self.error_count} could not be fetched (first: {self.first_error})"
        return text + ". Scan now to match by date."


def progress_map(app) -> dict[int, DateFetchProgress]:
    m = getattr(app.state, "date_fetch", None)
    if m is None:
        m = {}
        app.state.date_fetch = m
    return m


async def fetch_dates(
    session: Session, deps, sub: Subscription, progress: DateFetchProgress
) -> DateFetchProgress:
    """Network first, small commits: SQLite's single write lock is shared with the
    worker, and each cached date is useful on its own."""
    limit = sub.video_limit or int(get_setting(session, "scan_video_limit"))
    refs = await list_source_videos(deps, sub, limit)
    with session.no_autoflush:
        need = [
            r
            for r in refs
            if r.upload_date is None and r.title != r.id and not _date_known(session, r.id)
        ][:DATE_FETCH_MAX]
    progress.total = len(need)
    gate = asyncio.Semaphore(DATE_FETCH_PARALLEL)

    async def fetch(ref):
        async with gate:
            try:
                info = await asyncio.to_thread(deps.source.fetch_info, ref.url)
            except SourceError as exc:
                return ref, None, str(exc)
            return ref, info.upload_date, None

    since_commit = 0
    with session.no_autoflush:
        for fut in asyncio.as_completed([fetch(r) for r in need]):
            ref, upload_date, err = await fut
            if err:
                progress.error_count += 1
                progress.first_error = progress.first_error or f"{ref.id}: {err}"
                _remember_date(session, ref.id, None)  # do not re-ask for a week
            else:
                _remember_date(session, ref.id, upload_date)
                if upload_date:
                    progress.dated += 1
                else:
                    progress.unknown += 1
            progress.done += 1
            since_commit += 1
            if since_commit >= COMMIT_EVERY:
                session.commit()
                since_commit = 0
        session.commit()
    return progress


async def run_date_fetch(app, subscription_id: int) -> None:
    progress = progress_map(app)[subscription_id]
    try:
        with app.state.session_factory() as session:
            sub = session.get(Subscription, subscription_id)
            if sub is None:
                raise LookupError(f"subscription {subscription_id} not found")
            await fetch_dates(session, app.state.runner_deps, sub, progress)
    except Exception as exc:  # the task must not die silently
        progress.failure = f"{type(exc).__name__}: {exc}"
        log.exception("date fetch failed for subscription %d", subscription_id)
    finally:
        progress.running = False
        progress.finished_at = utcnow()


def start_date_fetch(app, subscription_id: int) -> DateFetchProgress:
    """Start a fetch for this subscription unless one is running; returns its progress."""
    m = progress_map(app)
    current = m.get(subscription_id)
    if current is not None and current.running:
        return current
    progress = DateFetchProgress(subscription_id=subscription_id, running=True, started_at=utcnow())
    m[subscription_id] = progress
    app.state.date_fetch_task = asyncio.create_task(run_date_fetch(app, subscription_id))
    return progress


def _exists(request: Request, subscription_id: int) -> None:
    with request.app.state.session_factory() as session:
        if session.get(Subscription, subscription_id) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"subscription {subscription_id} not found"
            )


@router.post("/{subscription_id}/dates")
async def start(request: Request, subscription_id: int) -> dict:
    _exists(request, subscription_id)
    return start_date_fetch(request.app, subscription_id).as_dict()


@router.get("/{subscription_id}/dates")
async def status_(request: Request, subscription_id: int) -> dict:
    _exists(request, subscription_id)
    p = progress_map(request.app).get(subscription_id)
    return (p or DateFetchProgress(subscription_id=subscription_id)).as_dict()
