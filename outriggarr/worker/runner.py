"""Job runner: the background task started by main.py.

State machine per job: queued → downloading → importing → done | failed | cancelled.
`run_worker` claims due jobs and runs `process_job` for each; `process_job` is the
whole pipeline for one job and is what the tests drive directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from outriggarr.arr import ArrFactory
from outriggarr.arr.base import (
    ArrClient,
    ArrError,
    CommandStatus,
    ImportFile,
    Target,
    languages_for_import,
)
from outriggarr.db.models import Job, JobStatus, utcnow
from outriggarr.db.session import SessionFactory
from outriggarr.naming import (
    episode_filename,
    movie_filename,
    quality_for_height,
    quality_from_filename,
)
from outriggarr.settings import get_setting
from outriggarr.source import DownloadAborted, SourceError, VideoSource

log = logging.getLogger(__name__)

BACKOFF: tuple[timedelta, ...] = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
MAX_ATTEMPTS = len(BACKOFF) + 1
COMMAND_TIMEOUT_SECONDS = 600.0
PROGRESS_WRITE_INTERVAL = 2.0
CANCEL_CHECK_INTERVAL = 2.0


@dataclass
class RunnerDeps:
    session_factory: SessionFactory
    arr_factory: ArrFactory
    source: VideoSource
    staging_dir: Path
    poll_seconds: float = 5.0
    command_poll_seconds: float = 2.0
    scheduler_tick_seconds: float = 60.0
    now: Callable[[], datetime] = utcnow
    sleep: Callable[[float], object] = field(default=asyncio.sleep)


class _NoRetry(Exception):
    """A failure that config or user action must fix; the runner will not retry it."""


class _Retry(Exception):
    """A transient failure; the runner schedules a retry with backoff."""


def target_of(job: Job) -> Target:
    if job.movie_id is not None:
        return Target(movie_id=job.movie_id)
    return Target(series_id=job.series_id, episode_ids=tuple(job.episode_ids or ()))


def sweep_cancelled(session: Session, staging_dir: Path) -> int:
    """Remove staging folders of cancelled jobs that still reference one."""
    rows = list(
        session.scalars(
            select(Job).where(Job.status == JobStatus.cancelled, Job.staged_path.is_not(None))
        )
    )
    for job in rows:
        shutil.rmtree(staging_dir / str(job.id), ignore_errors=True)
        job.staged_path = None
    if rows:
        session.commit()
    return len(rows)


def abort_check(
    deps: RunnerDeps, job_id: int, stop_is_set: Callable[[], bool]
) -> Callable[[], bool]:
    """Abort when the worker is stopping or the job was cancelled (DB read, throttled)."""
    last = 0.0
    cancelled = False

    def check() -> bool:
        nonlocal last, cancelled
        if stop_is_set():
            return True
        t = time.monotonic()
        if t - last >= CANCEL_CHECK_INTERVAL:
            last = t
            with deps.session_factory() as s:
                row = s.get(Job, job_id)
                cancelled = row is not None and row.status is JobStatus.cancelled
        return cancelled

    return check


def claim_next_jobs(session: Session, limit: int, now: datetime) -> list[int]:
    """Mark up to `limit` due jobs as downloading and return their ids."""
    if limit <= 0:
        return []
    due = or_(
        (Job.status == JobStatus.queued)
        & (or_(Job.next_retry_at.is_(None), Job.next_retry_at <= now)),
        (Job.status == JobStatus.failed)
        & (Job.next_retry_at.is_not(None))
        & (Job.next_retry_at <= now),
    )
    jobs = list(
        session.scalars(select(Job).where(due).order_by(Job.created_at, Job.id).limit(limit))
    )
    for job in jobs:
        job.status = JobStatus.downloading
        job.next_retry_at = None
    session.commit()
    return [job.id for job in jobs]


async def run_worker(deps: RunnerDeps, stop: asyncio.Event) -> None:
    log.info("worker started")
    running: set[asyncio.Task[None]] = set()
    while not stop.is_set():
        running = {t for t in running if not t.done()}
        try:
            with deps.session_factory() as session:
                sweep_cancelled(session, deps.staging_dir)
                concurrency = int(get_setting(session, "concurrency"))
                ids = claim_next_jobs(session, concurrency - len(running), deps.now())
        except Exception:
            log.exception("claiming jobs failed")
            ids = []
        for job_id in ids:
            running.add(asyncio.create_task(_guarded(deps, job_id, stop.is_set)))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=deps.poll_seconds)
    if running:
        log.info("worker stopping; waiting for %d job(s) to abort", len(running))
        await asyncio.gather(*running, return_exceptions=True)
    log.info("worker stopped")


async def _guarded(deps: RunnerDeps, job_id: int, stop_is_set: Callable[[], bool]) -> None:
    try:
        await process_job(deps, job_id, abort_check(deps, job_id, stop_is_set))
    except Exception as exc:  # a bug, not a job outcome: record it, keep the worker alive
        log.exception("job %d crashed", job_id)
        with deps.session_factory() as session:
            job = session.get(Job, job_id)
            if job is not None:
                _fail(job, f"internal error: {exc!r}", retry=False, now=deps.now())
                session.commit()


async def process_job(
    deps: RunnerDeps, job_id: int, should_abort: Callable[[], bool] = lambda: False
) -> None:
    with deps.session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        if job.status in (JobStatus.queued, JobStatus.failed):
            job.status = JobStatus.downloading
        job.attempts += 1
        job.error = None
        job.progress_pct = 0
        session.commit()
        conn = job.connection
        target = target_of(job)
        dest = deps.staging_dir / str(job.id)
        client = deps.arr_factory(conn)
        remote_folder = f"{conn.staging_path_remote.rstrip('/')}/{job.id}"

        try:
            staged = await _download_stage(deps, session, job, client, target, dest, should_abort)
            if _cancelled_meanwhile(session, job):
                raise DownloadAborted("cancelled")
            job.status = JobStatus.importing
            session.commit()
            imported = await _import_stage(
                deps, session, job, client, target, remote_folder, staged
            )
        except DownloadAborted:
            shutil.rmtree(dest, ignore_errors=True)
            if _cancelled_meanwhile(session, job):
                job.staged_path = None
                job.error = "cancelled during download"
                job.finished_at = deps.now()
            else:
                job.status = JobStatus.queued
                job.attempts -= 1
                job.error = "interrupted; will resume"
            session.commit()
            return
        except _Retry as exc:
            _fail(job, str(exc), retry=True, now=deps.now())
            session.commit()
            return
        except _NoRetry as exc:
            _fail(job, str(exc), retry=False, now=deps.now())
            session.commit()
            return

        shutil.rmtree(dest, ignore_errors=True)
        job.finished_at = deps.now()
        if imported:
            job.status = JobStatus.done
            job.progress_pct = 100
            log.info("job %d done: %s", job.id, staged.name)
        else:
            job.status = JobStatus.cancelled
            job.error = "target already has a file; nothing imported"
            log.info("job %d: target already satisfied, staged file discarded", job.id)
        session.commit()


def _cancelled_meanwhile(session: Session, job: Job) -> bool:
    """The API may have flipped the row to cancelled while we were busy in this session."""
    session.refresh(job, attribute_names=["status"])
    return job.status is JobStatus.cancelled


async def _download_stage(
    deps: RunnerDeps,
    session: Session,
    job: Job,
    client: ArrClient,
    target: Target,
    dest: Path,
    should_abort: Callable[[], bool],
) -> Path:
    if job.staged_path and Path(job.staged_path).exists():
        log.info("job %d: staged file already present, skipping download", job.id)
        return Path(job.staged_path)

    try:
        info = await client.target_info(target)
    except ArrError as exc:
        raise _Retry(str(exc)) from exc

    fmt = job.format or get_setting(session, "default_format")
    container = get_setting(session, "merge_container")
    last_write = 0.0

    def progress(pct: float) -> None:
        # Called from the yt-dlp thread; throttle DB writes.
        nonlocal last_write
        t = time.monotonic()
        if t - last_write < PROGRESS_WRITE_INTERVAL:
            return
        last_write = t
        with deps.session_factory() as s:
            row = s.get(Job, job.id)
            if row is not None:
                row.progress_pct = int(pct)
                s.commit()

    try:
        result = await asyncio.to_thread(
            deps.source.download,
            job.video_url,
            dest,
            fmt=fmt,
            merge_container=container,
            progress=progress,
            should_abort=should_abort,
        )
    except SourceError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise _Retry(str(exc)) from exc
    except DownloadAborted:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    quality = quality_for_height(result.height)
    if target.is_movie:
        name = movie_filename(info.title, info.year, quality, result.ext)
    else:
        name = episode_filename(
            info.title,
            info.season or 0,
            list(info.episode_numbers),
            info.episode_title,
            quality,
            result.ext,
        )
    staged = dest / name
    result.path.rename(staged)
    language = get_setting(session, "audio_language")
    if language:
        try:
            await asyncio.to_thread(deps.source.tag_audio_language, staged, language)
        except SourceError as exc:
            # The file is still importable; keep the note on the job rather than fail it.
            log.warning("job %d: audio language tag failed: %s", job.id, exc)
            job.error = f"audio language tag failed (file imported untagged): {exc}"
    job.staged_path = str(staged)
    job.video_title = job.video_title or result.title
    job.progress_pct = 100
    session.commit()
    return staged


async def _import_stage(
    deps: RunnerDeps,
    session: Session,
    job: Job,
    client: ArrClient,
    target: Target,
    remote_folder: str,
    staged: Path,
) -> bool:
    """True if the file was imported; False if the target already had a file."""
    quality = quality_from_filename(staged.name)
    if quality is None:
        raise _NoRetry(f"staged file {staged.name!r} carries no quality tag")
    try:
        info = await client.target_info(target)
        if info.has_file:
            return False
        candidates = await client.manual_import_candidates(remote_folder)
        cand = next(
            (
                c
                for c in candidates
                if c.relative_path == staged.name or c.path.endswith("/" + staged.name)
            ),
            None,
        )
        if cand is None:
            seen = [c.relative_path or c.path for c in candidates]
            raise _NoRetry(
                f"no import candidate for {staged.name!r} in {remote_folder!r}; "
                f"the server listed {seen}"
            )
        if cand.rejections:
            raise _NoRetry("import rejected: " + "; ".join(cand.rejections))
        command_id = await client.manual_import(
            [
                ImportFile(
                    path=cand.path,
                    quality_name=quality,
                    languages=languages_for_import(cand),
                    target=target,
                )
            ]
        )
        status = await _wait_for_command(deps, client, command_id)
        if not status.ok:
            raise _NoRetry(f"ManualImport {status.status}: {status.message or ''}".rstrip(": "))
        after = await client.target_info(target)
        if not after.has_file:
            raise _NoRetry(
                "ManualImport completed but the server still reports no file for the target; "
                "staged file kept"
            )
    except ArrError as exc:
        raise _Retry(str(exc)) from exc
    return True


async def _wait_for_command(deps: RunnerDeps, client: ArrClient, command_id: int) -> CommandStatus:
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    while True:
        status = await client.command(command_id)
        if status.finished:
            return status
        if time.monotonic() > deadline:
            raise _NoRetry(f"ManualImport command {command_id} still {status.status} after timeout")
        await deps.sleep(deps.command_poll_seconds)


def _fail(job: Job, message: str, *, retry: bool, now: datetime) -> None:
    job.error = message
    job.status = JobStatus.failed
    if retry and job.attempts < MAX_ATTEMPTS:
        job.next_retry_at = now + BACKOFF[min(job.attempts, len(BACKOFF)) - 1]
        job.finished_at = None
    else:
        job.next_retry_at = None
        job.finished_at = now
