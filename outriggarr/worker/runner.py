"""Job runner: the background task started by main.py.

State machine per job: queued → downloading → importing → done | failed | cancelled.
`run_worker` claims due jobs and runs `process_job` for each; `process_job` is the
whole pipeline for one job and is what the tests drive directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from outriggarr.arr import ArrFactory
from outriggarr.arr.base import (
    ArrClient,
    ArrError,
    CommandStatus,
    ImportCandidate,
    ImportFile,
    Target,
    TargetInfo,
    languages_for_import,
)
from outriggarr.db.models import Connection, Job, JobStatus, utcnow
from outriggarr.db.session import SessionFactory
from outriggarr.naming import (
    episode_filename,
    movie_filename,
    quality_for_height,
    quality_from_filename,
)
from outriggarr.notify import Notifier, NullNotifier
from outriggarr.settings import get_setting
from outriggarr.source import (
    CoolOff,
    DownloadAborted,
    SourceError,
    VideoSource,
    is_permanent_failure,
    is_rate_limited,
)

log = logging.getLogger(__name__)

BACKOFF: tuple[timedelta, ...] = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
MAX_ATTEMPTS = len(BACKOFF) + 1
# A download that stops advancing is abandoned (and retried later) rather than holding
# the worker until a restart: no progress for this long, or this long in total.
STALL_IDLE_SECONDS = 30 * 60.0
STALL_CAP_SECONDS = 6 * 3600.0
COMMAND_TIMEOUT_SECONDS = 600.0
PROGRESS_WRITE_INTERVAL = 2.0
CANCEL_CHECK_INTERVAL = 2.0
# GET /manualimport evaluates rejections without our ids; these two are the parser's
# own "I could not tell which series/movie this is" and mean nothing once we reprocess
# with explicit ids.
PARSE_ONLY_REJECTIONS = frozenset({"unknown series", "unknown movie"})


@dataclass
class RunnerDeps:
    session_factory: SessionFactory
    arr_factory: ArrFactory
    source: VideoSource
    staging_dir: Path
    poll_seconds: float = 5.0
    command_poll_seconds: float = 2.0
    scheduler_tick_seconds: float = 60.0
    notifier: Notifier = field(default_factory=NullNotifier)
    lock_dir: Path | None = None  # where the single-instance lock file lives (config dir)
    now: Callable[[], datetime] = utcnow
    sleep: Callable[[float], object] = field(default=asyncio.sleep)
    cooloff: CoolOff = field(default_factory=CoolOff)  # shared with the scheduler and fetches
    stall_idle_seconds: float = STALL_IDLE_SECONDS
    stall_cap_seconds: float = STALL_CAP_SECONDS
    clock: Callable[[], float] = time.monotonic


class _StallGuard:
    """Trips when a download stops advancing: no progress for `idle` seconds, or `cap`
    seconds since it started. The source's progress hook asks `tripped()` on every call
    and aborts; yt-dlp's own socket timeout (20 s) keeps that hook cycling through a
    dead connection, so the abort is delivered. Progress needs a known total, as the
    percentage does; a hung ffmpeg merge is the one thing this cannot reach."""

    def __init__(self, idle: float, cap: float, clock: Callable[[], float]) -> None:
        self.idle = idle
        self.cap = cap
        self.clock = clock
        self.started = self.last_advance = clock()
        self.last_pct = -1.0
        self.reason: str | None = None

    def advanced(self, pct: float) -> None:
        # not "greater": with bestvideo+bestaudio the hook restarts at 0 % for the audio
        # stream, and steady audio progress must count as progress
        if pct != self.last_pct:
            self.last_pct = pct
            self.last_advance = self.clock()

    def tripped(self) -> bool:
        if self.reason is not None:
            return True
        now = self.clock()
        if now - self.last_advance >= self.idle:
            self.reason = (
                f"no download progress for {int(self.idle // 60)} min "
                f"(stuck at {max(self.last_pct, 0):.0f}%); abandoned, will retry"
            )
        elif now - self.started >= self.cap:
            self.reason = (
                f"download still running after {int(self.cap // 3600)} h; abandoned, will retry"
            )
        return self.reason is not None


class _NoRetry(Exception):
    """A failure that config or user action must fix; the runner will not retry it."""


class _Retry(Exception):
    """A transient failure; the runner schedules a retry with backoff."""


# A rate-limit answer re-queues the job without spending an attempt, because the wall
# is the session's. When the same video keeps answering that way while everything else
# flows, the wall is that video's own (a caption or fragment URL): the normal ladder.
RATE_LIMIT_MAX_REQUEUES = 3


class _RateLimited(Exception):
    """The source rate-limited the session: the job waits out the shared cool-off
    without spending an attempt, and the worker starts nothing else meanwhile."""

    def __init__(self, message: str, wait_seconds: float) -> None:
        super().__init__(message)
        self.message = message
        self.wait_seconds = wait_seconds


class _Cancelled(Exception):
    """The API cancelled the job while we were working on it."""


class _Interrupted(Exception):
    """The worker is stopping mid-import: keep the staged file, resume after restart."""


def _arr_failure(exc: ArrError) -> Exception:
    """Transport/5xx → retry with backoff; a 4xx/validation answer is deterministic."""
    return _Retry(str(exc)) if exc.retryable else _NoRetry(str(exc))


def target_of(job: Job) -> Target:
    if job.movie_id is not None:
        return Target(movie_id=job.movie_id)
    return Target(series_id=job.series_id, episode_ids=tuple(job.episode_ids or ()))


def sweep_cancelled(session: Session, staging_dir: Path, *, full: bool = False) -> int:
    """Remove the staging folders of cancelled jobs. Each tick looks only at rows that
    still point at a staged file; `full=True` (startup) also stats every cancelled
    job's folder, so an orphan from an internal error is caught once, not every 5 s."""
    q = select(Job).where(Job.status == JobStatus.cancelled)
    if not full:
        q = q.where(Job.staged_path.is_not(None))
    rows = list(session.scalars(q))
    swept = 0
    for job in rows:
        folder = staging_dir / str(job.id)
        touched = False
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            touched = True
        if job.staged_path is not None:
            job.staged_path = None
            touched = True
        swept += touched
    if swept:
        session.commit()
    return swept


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
                # a row deleted meanwhile is gone for good: stop, do not recreate its folder
                cancelled = row is None or row.status is JobStatus.cancelled
        return cancelled

    return check


def claim_next_jobs(
    session: Session, limit: int, now: datetime, exclude: Iterable[int] = ()
) -> list[int]:
    """Mark up to `limit` due jobs as downloading and return their ids. `exclude` is the
    set a running task still owns (a job cancelled then retried mid-run is `queued`
    again while its first run is still going; claiming it twice would run two
    downloads into one folder)."""
    if limit <= 0:
        return []
    due = or_(
        (Job.status == JobStatus.queued)
        & (or_(Job.next_retry_at.is_(None), Job.next_retry_at <= now)),
        (Job.status == JobStatus.failed)
        & (Job.next_retry_at.is_not(None))
        & (Job.next_retry_at <= now),
    )
    q = (
        select(Job)
        .join(Connection, Connection.id == Job.connection_id)
        .where(due, Connection.enabled)
        .order_by(Job.created_at, Job.id)
        .limit(limit)
    )
    excluded = list(exclude)
    if excluded:
        q = q.where(Job.id.not_in(excluded))
    ids = [job.id for job in session.scalars(q)]
    claimed: list[int] = []
    for job_id in ids:
        # conditional: a Cancel committed between the SELECT and this UPDATE must win
        res = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_((JobStatus.queued, JobStatus.failed)))
            .values(status=JobStatus.downloading, next_retry_at=None)
        )
        if res.rowcount == 1:
            claimed.append(job_id)
    session.commit()
    return claimed


def recover_stale_jobs(session: Session, exclude: Iterable[int] = ()) -> int:
    """Jobs left in downloading/importing by a crash or a hard kill go back to queued.
    A staged file that survived is reused (the download stage skips it). `exclude` are
    the jobs this worker is running now: anything else in those states is an orphan
    (its task died without recording an outcome)."""
    owned = set(exclude)
    rows = [
        j
        for j in session.scalars(
            select(Job).where(Job.status.in_((JobStatus.downloading, JobStatus.importing)))
        )
        if j.id not in owned
    ]
    for job in rows:
        job.status = JobStatus.queued
        job.next_retry_at = None
        job.error = "recovered after a restart; will resume"
        if job.attempts > 0:
            job.attempts -= 1  # the interrupted run does not count
    if rows:
        session.commit()
        log.warning("recovered %d job(s) interrupted by a restart", len(rows))
    return len(rows)


def acquire_instance_lock(config_dir: Path):
    """One worker per database. Returns the open lock file (keep it alive) or None when
    another live instance holds it; on filesystems without flock it warns and proceeds."""
    import fcntl

    path = config_dir / ".outriggarr.lock"
    try:
        fh = open(path, "a+")  # noqa: SIM115 — held for the process lifetime
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    except OSError as exc:
        log.warning("instance lock unavailable on this filesystem (%s); continuing", exc)
        return open(path, "a+")  # noqa: SIM115
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


async def run_worker(deps: RunnerDeps, stop: asyncio.Event, lock: object | None = None) -> None:
    # the app passes a lock it already holds (it covers the scheduler too); standalone
    # callers (tests) let the worker take its own
    if lock is None:
        lock = acquire_instance_lock(
            deps.staging_dir.parent if deps.lock_dir is None else deps.lock_dir
        )
    if lock is None:
        log.error(
            "another Outriggarr instance already runs the worker for this database; "
            "this worker stays idle (two workers would download the same jobs twice)"
        )
        await stop.wait()
        return
    log.info("worker started")
    try:
        with deps.session_factory() as session:
            recover_stale_jobs(session)
            sweep_cancelled(session, deps.staging_dir, full=True)
    except Exception:
        log.exception("recovering stale jobs failed; continuing")
    running: dict[int, asyncio.Task[None]] = {}
    paused_logged = False
    while not stop.is_set():
        for jid, t in running.items():  # a task that died is a log line, not a mystery
            if t.done() and not t.cancelled() and t.exception() is not None:
                log.error("job %d: task ended with %r", jid, t.exception())
        running = {jid: t for jid, t in running.items() if not t.done()}
        try:
            with deps.session_factory() as session:
                sweep_cancelled(session, deps.staging_dir)
                recover_stale_jobs(session, exclude=running.keys())  # orphans, not ours
                if deps.cooloff.active():
                    # rate-limited: running jobs finish, nothing new starts until it lifts
                    ids = []
                    if not paused_logged:
                        log.warning(
                            "worker: the source rate-limited us; starting no jobs for %d s",
                            int(deps.cooloff.remaining()),
                        )
                        paused_logged = True
                else:
                    paused_logged = False
                    concurrency = int(get_setting(session, "concurrency"))
                    ids = claim_next_jobs(
                        session, concurrency - len(running), deps.now(), exclude=running.keys()
                    )
        except Exception:
            log.exception("claiming jobs failed")
            ids = []
        for job_id in ids:
            running[job_id] = asyncio.create_task(_guarded(deps, job_id, stop.is_set))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=deps.poll_seconds)
    if running:
        log.info("worker stopping; waiting for %d job(s) to abort", len(running))
        await asyncio.gather(*running.values(), return_exceptions=True)
    log.info("worker stopped")


async def _guarded(deps: RunnerDeps, job_id: int, stop_is_set: Callable[[], bool]) -> None:
    try:
        await process_job(deps, job_id, abort_check(deps, job_id, stop_is_set))
    except Exception as exc:  # a bug, not a job outcome: record it, keep the worker alive
        log.exception("job %d crashed", job_id)
        try:
            with deps.session_factory() as session:
                job = session.get(Job, job_id)
                if job is not None:
                    if job.staged_path is None:
                        # nothing usable to keep for a Retry: do not leave a multi-GB orphan
                        shutil.rmtree(deps.staging_dir / str(job.id), ignore_errors=True)
                    if _fail(session, job, f"internal error: {exc!r}", retry=False, now=deps.now()):
                        session.commit()
                        await _notify_failed(deps, session, job)
        except Exception:  # the row stays downloading; the next tick's orphan sweep requeues it
            log.exception("job %d: recording the crash failed", job_id)


async def process_job(
    deps: RunnerDeps, job_id: int, should_abort: Callable[[], bool] = lambda: False
) -> None:
    with deps.session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        if job.status is JobStatus.cancelled:
            return  # cancelled between claim and start: leave the row exactly as the API left it
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
            if staged is None:  # target already satisfied before we downloaded anything
                imported = False
            else:
                _enter_importing(session, job)
                imported = await _import_stage(
                    deps, session, job, client, target, remote_folder, staged, should_abort
                )
        except _Cancelled:
            shutil.rmtree(dest, ignore_errors=True)
            session.refresh(job)
            if job.status is JobStatus.cancelled:  # a Cancel→Retry meanwhile leaves it queued
                job.staged_path = None
                if not job.error or job.error == "cancelled":
                    job.error = "cancelled during download"
                if job.finished_at is None:
                    job.finished_at = deps.now()
                session.commit()
            return
        except _Interrupted:
            job.status = JobStatus.queued
            job.attempts -= 1
            job.error = "interrupted during import; will resume"
            session.commit()
            return
        except _RateLimited as exc:
            shutil.rmtree(dest, ignore_errors=True)
            if _cancelled_meanwhile(session, job):
                job.staged_path = None
                job.error = "cancelled during download"
                job.finished_at = deps.now()
            else:
                # the wall was the source's, not this job's: back to the queue, no attempt spent
                job.status = JobStatus.queued
                job.attempts -= 1
                job.rate_limit_hits += 1
                job.next_retry_at = deps.now() + timedelta(seconds=exc.wait_seconds)
                job.error = (
                    f"rate-limited by the source; all downloads paused for "
                    f"{int(exc.wait_seconds // 60)} min: {exc.message}"
                )
                log.warning("job %d: %s", job.id, job.error)
            session.commit()
            return
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
            if _fail(session, job, str(exc), retry=True, now=deps.now()):
                session.commit()
                if job.next_retry_at is None:  # attempts exhausted
                    await _notify_failed(deps, session, job)
            return
        except _NoRetry as exc:
            if _fail(session, job, str(exc), retry=False, now=deps.now()):
                session.commit()
                await _notify_failed(deps, session, job)
            return

        shutil.rmtree(dest, ignore_errors=True)
        job.finished_at = deps.now()
        if imported:
            job.status = JobStatus.done
            job.progress_pct = 100
            log.info("job %d done: %s", job.id, staged.name if staged else "")
            session.commit()
            if get_setting(session, "notify_on_done") == "1":
                await notify(
                    deps,
                    "Outriggarr: imported",
                    f"{job.target_label or job.target_key}\n{job.video_title}",
                )
        else:
            # Nothing left to do: the *arr already has the file (imported by an earlier
            # attempt of this job, a twin job, or elsewhere). That is a finished job, not a
            # user cancellation, so it neither covers the episode nor blocks a re-grab.
            job.status = JobStatus.done
            job.progress_pct = 100
            job.staged_path = None
            job.error = "target already had a file; nothing imported"
            log.info("job %d: target already satisfied", job.id)
        session.commit()


def _enter_importing(session: Session, job: Job) -> None:
    """downloading → importing, but only if the API did not cancel meanwhile (atomic)."""
    res = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == JobStatus.downloading)
        .values(status=JobStatus.importing)
    )
    session.commit()
    session.refresh(job)
    if res.rowcount != 1:
        raise _Cancelled()


async def notify(deps: RunnerDeps, title: str, body: str) -> None:
    """Fire-and-forget; runs the (blocking) notifier in a thread and never raises."""
    try:
        await asyncio.to_thread(deps.notifier.send, title, body)
    except Exception:
        log.exception("notifier crashed")


async def _notify_failed(deps: RunnerDeps, session: Session, job: Job) -> None:
    if get_setting(session, "notify_on_failed") != "1":
        return
    await notify(
        deps,
        "Outriggarr: job failed",
        f"#{job.id} {job.target_label or job.target_key}\n{job.video_title}\n\n{job.error}",
    )


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
) -> Path | None:
    """Returns the staged file, or None when the target already has a file (nothing to do)."""
    if job.staged_path and Path(job.staged_path).exists():
        log.info("job %d: staged file already present, skipping download", job.id)
        return Path(job.staged_path)

    try:
        info = await client.target_info(target)
    except ArrError as exc:
        raise _arr_failure(exc) from exc
    if info.has_file:
        # Sonarr/Radarr already have it — often OUR earlier attempt's import, whose
        # staged file the *arr has since moved (a blip on the command poll, a hard stop
        # while importing). Do not spend a download; the caller records the job as done.
        return None
    if info.partially_satisfied:
        raise _NoRetry(
            "some of the target episodes already have a file; importing this multi-episode "
            "file would replace them — split the target or delete those files first"
        )

    fmt = job.format or get_setting(session, "default_format")
    container = get_setting(session, "merge_container")
    sub_langs = tuple(x for x in get_setting(session, "subtitles_langs").split(",") if x)
    auto_subs = get_setting(session, "subtitles_auto") == "1"
    guard = _StallGuard(deps.stall_idle_seconds, deps.stall_cap_seconds, deps.clock)
    last_write = 0.0

    def progress(pct: float) -> None:
        # Called from the yt-dlp thread; throttle DB writes.
        nonlocal last_write
        guard.advanced(pct)
        t = time.monotonic()
        if t - last_write < PROGRESS_WRITE_INTERVAL:
            return
        last_write = t
        with deps.session_factory() as s:
            row = s.get(Job, job.id)
            if row is not None:
                row.progress_pct = int(pct)
                s.commit()

    def abort_or_stalled() -> bool:
        return should_abort() or guard.tripped()

    started = deps.cooloff.clock()
    try:
        result = await asyncio.to_thread(
            deps.source.download,
            job.video_url,
            dest,
            fmt=fmt,
            merge_container=container,
            progress=progress,
            should_abort=abort_or_stalled,
            subtitle_langs=sub_langs,
            auto_subtitles=auto_subs,
        )
        deps.cooloff.clear(since=started)  # the source is serving us again
        job.rate_limit_hits = 0
        quality = quality_for_height(result.height)
        staged = dest / _staging_name(target, info, quality, result.ext)
        result.path.rename(staged)
        # Subtitle sidecars keep the video's stem so the *arr imports them as extra files:
        # <id>.<lang>.srt → <staged stem>.<lang>.srt
        for sub in result.subtitles:
            suffix = sub.name[len(result.video_id) :]  # ".en.srt"
            sub.rename(dest / f"{staged.stem}{suffix}")
    except SourceError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        if is_rate_limited(str(exc)):
            if job.rate_limit_hits >= RATE_LIMIT_MAX_REQUEUES:
                raise _Retry(
                    f"rate-limited answer {job.rate_limit_hits + 1} times in a row for this "
                    f"video while other downloads went through: {exc}"
                ) from exc
            raise _RateLimited(str(exc), deps.cooloff.hit(str(exc))) from exc
        if is_permanent_failure(str(exc)):
            # gone, walled off, or a wrong request: a day of retries changes nothing
            raise _NoRetry(str(exc)) from exc
        raise _Retry(str(exc)) from exc
    except OSError as exc:
        # e.g. PermissionError creating /staging/<id>, ENAMETOOLONG on the rename: an
        # operator/environment problem, retryable once fixed. Keep the OS text verbatim.
        shutil.rmtree(dest, ignore_errors=True)
        raise _Retry(f"staging error: {exc}") from exc
    except DownloadAborted:
        shutil.rmtree(dest, ignore_errors=True)
        if guard.reason is not None and not should_abort():
            raise _Retry(guard.reason) from None  # our own stall abort: a failure, not a stop
        raise

    if result.subtitles:
        log.info("job %d: %d subtitle sidecar(s) staged", job.id, len(result.subtitles))
    language, origin = audio_language_for(
        job, result.audio_language, get_setting(session, "audio_language")
    )
    if language:
        try:
            await asyncio.to_thread(deps.source.tag_audio_language, staged, language)
            log.info("job %d: audio tagged %s (%s)", job.id, language, origin)
        except SourceError as exc:
            # The file is still importable; keep the note on the job rather than fail it.
            log.warning("job %d: audio language tag failed: %s", job.id, exc)
            job.error = f"audio language tag failed (file imported untagged): {exc}"
    job.staged_path = str(staged)
    job.video_title = job.video_title or result.title
    job.progress_pct = 100
    session.commit()
    return staged


def _staging_name(target: Target, info: TargetInfo, quality: str, ext: str) -> str:
    if target.is_movie:
        return movie_filename(info.title, info.year, quality, ext)
    return episode_filename(
        info.title, info.season or 0, list(info.episode_numbers), info.episode_title, quality, ext
    )


async def _import_stage(
    deps: RunnerDeps,
    session: Session,
    job: Job,
    client: ArrClient,
    target: Target,
    remote_folder: str,
    staged: Path,
    should_abort: Callable[[], bool] = lambda: False,
) -> bool:
    """True if the file was imported; False if the target already had a file."""
    quality = quality_from_filename(staged.name)
    if quality is None:
        raise _NoRetry(f"staged file {staged.name!r} carries no quality tag")
    try:
        info = await client.target_info(target)
        if info.has_file:
            return False
        if info.partially_satisfied:
            # a retry with the file already staged skips the pre-download check
            raise _NoRetry(
                "some of the target episodes already have a file; importing this multi-episode "
                "file would replace them — split the target or delete those files first"
            )
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
        languages = languages_for_import(cand)
        rejections = await _real_rejections(client, cand, target, quality, languages, info)
        if rejections:
            raise _NoRetry("import rejected: " + "; ".join(rejections))
        command_id = await client.manual_import(
            [ImportFile(path=cand.path, quality_name=quality, languages=languages, target=target)]
        )
        status = await _wait_for_command(deps, client, command_id, should_abort)
        if not status.ok:
            raise _NoRetry(f"ManualImport {status.status}: {status.message or ''}".rstrip(": "))
        after = await client.target_info(target)
        if not after.has_file:
            raise _NoRetry(
                "ManualImport completed but the server still reports no file for the target; "
                "staged file kept"
            )
    except ArrError as exc:
        raise _arr_failure(exc) from exc
    return True


async def _real_rejections(
    client: ArrClient,
    cand: ImportCandidate,
    target: Target,
    quality: str,
    languages,
    info: TargetInfo,
) -> list[str]:
    """The GET's rejections were computed without our ids, so "Unknown Series" only
    means the parser could not map the filename. Re-evaluate with explicit ids (the
    *arr's reprocess call, what its own UI does); if that is unavailable, drop just the
    parse-only rejections — the post-command `hasFile` check remains the safety net."""
    if not cand.rejections:
        return []
    try:
        return list(await client.reprocess(cand, target, quality, languages, info.season))
    except ArrError as exc:
        log.warning("reprocess unavailable (%s); ignoring parse-only rejections", exc)
        return [r for r in cand.rejections if r.strip().lower() not in PARSE_ONLY_REJECTIONS]


async def _wait_for_command(
    deps: RunnerDeps,
    client: ArrClient,
    command_id: int,
    should_abort: Callable[[], bool] = lambda: False,
) -> CommandStatus:
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    while True:
        status = await client.command(command_id)
        if status.finished:
            return status
        if should_abort():
            raise _Interrupted()
        if time.monotonic() > deadline:
            # the *arr may still be moving a large file: try again later, do not fail
            raise _Retry(f"ManualImport command {command_id} still {status.status} after timeout")
        await deps.sleep(deps.command_poll_seconds)


def audio_language_for(job: Job, detected: str | None, default: str) -> tuple[str | None, str]:
    """Which language to stamp on the audio: the subscription's own setting is the
    operator's word and wins; otherwise what the source declared (YouTube sets it per
    audio track, so anime stays Japanese); otherwise the global default; blank = none."""
    sub = job.subscription
    if sub is not None and sub.audio_language:
        return sub.audio_language, "subscription setting"
    if detected:
        return detected, "declared by the source"
    return (default or None), "global default"


def _fail(session: Session, job: Job, message: str, *, retry: bool, now: datetime) -> bool:
    """Record a failure. Returns False (and changes nothing) when the API cancelled the
    job meanwhile: the user's decision outranks a failure that landed later."""
    if _cancelled_meanwhile(session, job):
        return False
    job.error = message
    job.status = JobStatus.failed
    if retry and job.attempts < MAX_ATTEMPTS:
        job.next_retry_at = now + BACKOFF[min(job.attempts, len(BACKOFF)) - 1]
        job.finished_at = None
    else:
        job.next_retry_at = None
        job.finished_at = now
    return True
