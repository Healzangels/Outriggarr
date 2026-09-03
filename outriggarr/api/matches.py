"""Match evidence: fetch what the scheduler did not record (older jobs) so the review
page can clear pairings automatically instead of asking a person to eyeball them.

The recheck is a background task on the app: it outlives the request that started it,
reports progress, and commits in small batches so rows clear as the evidence lands."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from fastapi import APIRouter, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from outriggarr.api.deps import track_task
from outriggarr.arr.base import ArrError
from outriggarr.db.models import Connection, Job, utcnow
from outriggarr.matcher import length_mismatch
from outriggarr.source import SourceError, is_rate_limited

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/matches", tags=["matches"])

RECHECK_LIMIT = 300  # jobs per run
RECHECK_PARALLEL = 4  # per-video fetches in flight at once
COMMIT_EVERY = 10  # fetched durations per DB write
SKIPPED = "skipped: the source rate-limited us"  # not an error: left for the next run


@dataclass
class RecheckProgress:
    running: bool = False
    checked: int = 0  # jobs considered
    total: int = 0  # videos that need a fetch
    done: int = 0  # videos fetched so far (success or failure)
    runtimes_filled: int = 0
    durations_filled: int = 0
    flagged: int = 0
    error_count: int = 0
    first_error: str | None = None
    skipped: int = 0  # left for the next run: a rate-limit answer paused the fetches
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
            return f"Recheck failed: {self.failure}"
        if self.running:
            return (
                f"Checking lengths: {self.done} of {self.total} videos fetched… "
                "leaving this page will not stop it."
            )
        if self.finished_at is None:
            return ""
        if self.checked == 0:
            return "Nothing left to check: every pairing already has its length evidence."
        text = (
            f"Checked {self.checked} pairings: {self.durations_filled} video lengths and "
            f"{self.runtimes_filled} runtimes fetched; {self.flagged} contradict their runtime."
        )
        if self.error_count:
            text += f" {self.error_count} could not be fetched (first: {self.first_error})."
        if self.skipped:
            text += (
                f" {self.skipped} left for later: the source rate-limited us, "
                "fetches are paused until it lifts."
            )
        return text


def unchecked_count(session: Session) -> int:
    """Subscription jobs still missing their length evidence (and not confirmed)."""
    return (
        session.scalar(
            select(func.count(Job.id)).where(
                Job.subscription_id.is_not(None),
                Job.reviewed_at.is_(None),
                or_(Job.video_duration.is_(None), Job.target_runtime.is_(None)),
            )
        )
        or 0
    )


def progress_of(app) -> RecheckProgress:
    progress = getattr(app.state, "recheck", None)
    if progress is None:
        progress = RecheckProgress()
        app.state.recheck = progress
    return progress


async def recheck_evidence(
    session: Session,
    deps,
    *,
    progress: RecheckProgress | None = None,
    limit: int = RECHECK_LIMIT,
) -> RecheckProgress:
    """Fill in the video duration and the *arr runtime for subscription jobs that lack
    them. Network calls hold no write lock: the runtimes land in one commit, the
    durations every few fetches."""
    progress = progress or RecheckProgress()
    jobs = list(
        session.scalars(
            select(Job)
            .where(
                Job.subscription_id.is_not(None),
                Job.reviewed_at.is_(None),
                or_(Job.video_duration.is_(None), Job.target_runtime.is_(None)),
            )
            .order_by(Job.id.desc())
            .limit(limit)
        )
    )
    progress.checked = len(jobs)
    runtimes: dict[tuple[int, int], dict[int, int | None]] = {}
    with session.no_autoflush:
        for job in jobs:
            if job.target_runtime or not (job.series_id and job.episode_ids):
                continue
            key = (job.connection_id, job.series_id)
            if key not in runtimes:
                conn = session.get(Connection, job.connection_id)
                try:
                    eps = await deps.arr_factory(conn).episodes(job.series_id)
                    runtimes[key] = {e.id: e.runtime for e in eps}
                except ArrError as exc:
                    _note_error(progress, f"{conn.name}: {exc}")
                    runtimes[key] = {}
            found = [runtimes[key].get(eid) for eid in job.episode_ids]
            if found and all(found):  # a half-known multi-episode runtime is no evidence
                job.target_runtime = sum(found)  # type: ignore[arg-type]
                progress.runtimes_filled += 1
        session.commit()

        need = [j for j in jobs if j.video_duration is None]
        progress.total = len(need)
        gate = asyncio.Semaphore(RECHECK_PARALLEL)

        async def fetch(job: Job) -> tuple[Job, int | None, str | None]:
            async with gate:
                if deps.cooloff.active():
                    return job, None, SKIPPED
                try:
                    info = await asyncio.to_thread(deps.source.fetch_info, job.video_url)
                except SourceError as exc:
                    if is_rate_limited(str(exc)):
                        deps.cooloff.hit(str(exc))  # the rest of this run skips
                        return job, None, SKIPPED
                    return job, None, f"{job.video_id}: {exc}"
                return job, info.duration, None

        since_commit = 0
        tasks = [asyncio.create_task(fetch(j)) for j in need]
        try:
            for fut in asyncio.as_completed(tasks):
                job, duration, err = await fut
                if duration:
                    job.video_duration = int(duration)
                    progress.durations_filled += 1
                elif err == SKIPPED:
                    progress.skipped += 1
                elif err:
                    _note_error(progress, err)
                progress.done += 1
                since_commit += 1
                if since_commit >= COMMIT_EVERY:
                    session.commit()
                    since_commit = 0
        finally:
            # a consumer failure or a shutdown must not leave the remaining fetches
            # running unwatched (they burn the rate limit the cool-off protects), and
            # whatever was fetched is kept
            for t in tasks:
                t.cancel()
            with contextlib.suppress(Exception):
                session.commit()
    progress.flagged = sum(1 for j in jobs if length_mismatch(j.target_runtime, j.video_duration))
    if progress.error_count:
        log.warning(
            "match recheck: %d fetches failed, e.g. %s", progress.error_count, progress.first_error
        )
    return progress


def _note_error(progress: RecheckProgress, message: str) -> None:
    progress.error_count += 1
    progress.first_error = progress.first_error or message


async def run_recheck(app) -> None:
    progress = progress_of(app)
    try:
        with app.state.session_factory() as session:
            await recheck_evidence(session, app.state.runner_deps, progress=progress)
    except Exception as exc:  # the task must not die silently
        progress.failure = f"{type(exc).__name__}: {exc}"
        log.exception("match recheck failed")
    finally:
        progress.running = False
        progress.finished_at = utcnow()


def start_recheck(app) -> RecheckProgress:
    """Start a recheck unless one is running; returns the live progress either way."""
    current = progress_of(app)
    if current.running:
        return current
    progress = RecheckProgress(running=True, started_at=utcnow())
    app.state.recheck = progress
    track_task(app, asyncio.create_task(run_recheck(app)))
    return progress


@router.post("/recheck")
async def recheck(request: Request) -> dict:
    return start_recheck(request.app).as_dict()


@router.get("/recheck")
async def recheck_status(request: Request) -> dict:
    return progress_of(request.app).as_dict()
