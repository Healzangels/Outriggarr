"""Match evidence: fetch what the scheduler did not record (older jobs) so the review
page can clear pairings automatically instead of asking a person to eyeball them."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from outriggarr.api.deps import DbSession, RunnerDepsDep
from outriggarr.arr.base import ArrError
from outriggarr.db.models import Connection, Job
from outriggarr.matcher import length_mismatch
from outriggarr.source import SourceError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/matches", tags=["matches"])

RECHECK_LIMIT = 300  # jobs per call
RECHECK_PARALLEL = 4  # per-video fetches in flight at once


async def recheck_evidence(session: Session, deps, *, limit: int = RECHECK_LIMIT) -> dict:
    """Fill in the video duration and the *arr runtime for subscription jobs that lack
    them, then say how many pairings the length rule now contradicts. Network first,
    one write at the end: SQLite has a single write lock and the worker needs it."""
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
    errors: list[str] = []
    runtimes: dict[tuple[int, int], dict[int, int | None]] = {}
    filled_runtime = filled_duration = 0
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
                    errors.append(f"{conn.name}: {exc}")
                    runtimes[key] = {}
            found = [runtimes[key].get(eid) for eid in job.episode_ids]
            if found and all(found):
                job.target_runtime = sum(found)  # type: ignore[arg-type]
                filled_runtime += 1

        need = [j for j in jobs if j.video_duration is None]
        gate = asyncio.Semaphore(RECHECK_PARALLEL)

        async def fetch(job: Job) -> tuple[Job, int | None, str | None]:
            async with gate:
                try:
                    info = await asyncio.to_thread(deps.source.fetch_info, job.video_url)
                except SourceError as exc:
                    return job, None, f"{job.video_id}: {exc}"
                return job, info.duration, None

        for job, duration, err in await asyncio.gather(*(fetch(j) for j in need)):
            if duration:
                job.video_duration = int(duration)
                filled_duration += 1
            elif err:
                errors.append(err)
    session.commit()
    flagged = sum(1 for j in jobs if length_mismatch(j.target_runtime, j.video_duration))
    if errors:
        log.warning("match recheck: %d fetches failed, e.g. %s", len(errors), errors[0])
    return {
        "checked": len(jobs),
        "runtimes_filled": filled_runtime,
        "durations_filled": filled_duration,
        "flagged": flagged,
        "errors": errors[:10],
        "error_count": len(errors),
    }


@router.post("/recheck")
async def recheck(session: DbSession, deps: RunnerDepsDep) -> dict:
    return await recheck_evidence(session, deps)
