"""Subscriptions → jobs. `scan_subscription` is one scan (also the GUI's preview when
dry_run=True); `run_scheduler` runs due subscriptions on the configured interval."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from outriggarr.arr.base import ArrError, EpisodeRef
from outriggarr.db.models import Job, JobStatus, Subscription, TargetKind
from outriggarr.matcher import (
    Episode,
    MatchConfig,
    MatchResult,
    Override,
    Video,
    match,
    videos_needing_dates,
)
from outriggarr.naming import episode_code
from outriggarr.settings import get_setting
from outriggarr.source import SourceError, VideoRef
from outriggarr.worker.runner import RunnerDeps

log = logging.getLogger(__name__)

DATE_FETCH_LIMIT = 20  # per-video info fetches per scan, at most


@dataclass
class ScanReport:
    subscription_id: int
    scanned_at: datetime
    dry_run: bool
    videos: list[dict] = field(default_factory=list)
    matches: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    skipped_existing: list[dict] = field(default_factory=list)
    created_job_ids: list[int] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> dict:
        return {
            "at": self.scanned_at.isoformat(),
            "videos": len(self.videos),
            "wanted": len(self.matches) + len(self.unmatched) + len(self.skipped_existing),
            "matched": len(self.matches),
            "created": len(self.created_job_ids),
            "unmatched": len(self.unmatched),
            "skipped_existing": len(self.skipped_existing),
            "error": self.error,
        }

    def as_dict(self) -> dict:
        d = asdict(self)
        d["scanned_at"] = self.scanned_at.isoformat()
        return d


class SubscriptionNotFound(LookupError):
    pass


def _episode(e: EpisodeRef) -> Episode:
    return Episode(
        id=e.id,
        season=e.season_number,
        number=e.episode_number,
        title=e.title,
        air_date=e.air_date_utc.date() if e.air_date_utc else None,
    )


def _video(v: VideoRef) -> Video:
    upload = None
    if v.upload_date and len(v.upload_date) == 8:
        with contextlib.suppress(ValueError):
            upload = datetime.strptime(v.upload_date, "%Y%m%d").date()
    return Video(id=v.id, title=v.title, url=v.url, upload_date=upload)


def _episode_dict(ep: Episode) -> dict:
    return {
        "episode_id": ep.id,
        "season": ep.season,
        "number": ep.number,
        "code": episode_code(ep.season, [ep.number]),
        "title": ep.title,
        "air_date": ep.air_date.isoformat() if ep.air_date else None,
    }


def existing_jobs_for_series(
    session: Session, connection_id: int, series_id: int
) -> dict[int, Job]:
    """episode id → the job that already covers it (active, done, or awaiting retry)."""
    live = or_(
        Job.status.in_(
            (JobStatus.queued, JobStatus.downloading, JobStatus.importing, JobStatus.done)
        ),
        (Job.status == JobStatus.failed) & Job.next_retry_at.is_not(None),
    )
    rows = session.scalars(
        select(Job).where(
            Job.connection_id == connection_id,
            Job.series_id == series_id,
            Job.target_kind == TargetKind.episode,
            live,
        )
    )
    out: dict[int, Job] = {}
    for job in rows:
        for eid in job.episode_ids or []:
            out.setdefault(int(eid), job)
    return out


async def scan_subscription(
    deps: RunnerDeps, subscription_id: int, *, dry_run: bool = False
) -> ScanReport:
    now = deps.now()
    with deps.session_factory() as session:
        sub = session.get(
            Subscription, subscription_id, options=[selectinload(Subscription.overrides)]
        )
        if sub is None:
            raise SubscriptionNotFound(subscription_id)
        report = ScanReport(subscription_id=sub.id, scanned_at=now, dry_run=dry_run)
        try:
            await _scan(deps, session, sub, report, now)
        except (ArrError, SourceError) as exc:
            report.error = str(exc)
        if not dry_run:
            sub.last_scan_at = now
            sub.last_scan_result = report.summary()
            session.commit()
        return report


async def _scan(
    deps: RunnerDeps, session: Session, sub: Subscription, report: ScanReport, now: datetime
) -> None:
    conn = sub.connection
    client = deps.arr_factory(conn)
    all_episodes = await client.episodes(sub.series_id)
    wanted = [
        _episode(e)
        for e in all_episodes
        if e.monitored and not e.has_file and e.air_date_utc is not None and e.air_date_utc <= now
    ]
    covered = existing_jobs_for_series(session, conn.id, sub.series_id)
    todo: list[Episode] = []
    for ep in wanted:
        job = covered.get(ep.id)
        if job is None:
            todo.append(ep)
        else:
            report.skipped_existing.append(
                {**_episode_dict(ep), "job_id": job.id, "job_status": job.status.value}
            )

    limit = int(get_setting(session, "scan_video_limit"))
    refs = await asyncio.to_thread(deps.source.list_recent, sub.source_url, limit)
    videos = [_video(v) for v in refs]
    cfg = MatchConfig(
        strategies=tuple(sub.strategies or ()),
        date_tolerance_days=sub.date_tolerance_days,
        date_offset_days=sub.date_offset_days,
        title_regex=sub.title_regex,
    )
    overrides = [Override(o.video_id, o.season, o.episode) for o in sub.overrides]

    result = match(todo, videos, overrides, cfg)
    need = videos_needing_dates(result, videos, cfg)[:DATE_FETCH_LIMIT]
    if need:
        by_id = {v.id: v for v in videos}
        for v in need:
            try:
                info = await asyncio.to_thread(deps.source.fetch_info, v.url)
            except SourceError as exc:
                log.warning("subscription %d: fetch_info(%s) failed: %s", sub.id, v.id, exc)
                continue
            by_id[v.id] = _video(info)
        videos = list(by_id.values())
        result = match(todo, videos, overrides, cfg)

    report.videos = [
        {
            "id": v.id,
            "title": v.title,
            "url": v.url,
            "upload_date": v.upload_date.isoformat() if v.upload_date else None,
        }
        for v in videos
    ]
    _fill_report(report, result, videos)
    if not report.dry_run:
        _create_jobs(session, sub, result, report)


def _fill_report(report: ScanReport, result: MatchResult, videos: list[Video]) -> None:
    titles = {v.id: v.title for v in videos}
    for m in result.matches:
        report.matches.append(
            {
                **_episode_dict(m.episode),
                "video_id": m.video.id,
                "video_title": m.video.title,
                "video_url": m.video.url,
                "strategy": m.strategy,
                "job_id": None,
            }
        )
    for u in result.unmatched:
        report.unmatched.append(
            {
                **_episode_dict(u.episode),
                "candidates": {
                    strategy: [{"id": vid, "title": titles.get(vid, vid)} for vid in ids]
                    for strategy, ids in u.candidates.items()
                },
            }
        )


def _create_jobs(
    session: Session, sub: Subscription, result: MatchResult, report: ScanReport
) -> None:
    for entry, m in zip(report.matches, result.matches, strict=True):
        ep = m.episode
        label = f"{sub.title} {episode_code(ep.season, [ep.number])}"
        if ep.title:
            label += f" - {ep.title}"
        job = Job(
            connection_id=sub.connection_id,
            subscription_id=sub.id,
            target_kind=TargetKind.episode,
            series_id=sub.series_id,
            episode_ids=[ep.id],
            target_key=Job.make_target_key(
                TargetKind.episode, series_id=sub.series_id, episode_ids=[ep.id]
            ),
            target_label=label[:300],
            video_id=m.video.id,
            video_url=m.video.url,
            video_title=m.video.title[:500],
            format=sub.format,
        )
        session.add(job)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            entry["job_id"] = None
            entry["skipped"] = "a job for this episode and video already exists"
            continue
        entry["job_id"] = job.id
        report.created_job_ids.append(job.id)


def due_subscription_ids(session: Session, now: datetime, interval: timedelta) -> list[int]:
    cutoff = now - interval
    rows = session.scalars(
        select(Subscription.id)
        .where(Subscription.enabled)
        .where(or_(Subscription.last_scan_at.is_(None), Subscription.last_scan_at <= cutoff))
        .order_by(Subscription.last_scan_at.nulls_first(), Subscription.id)
    )
    return list(rows)


async def run_scheduler(deps: RunnerDeps, stop: asyncio.Event) -> None:
    log.info("scheduler started")
    while not stop.is_set():
        try:
            with deps.session_factory() as session:
                interval = timedelta(minutes=int(get_setting(session, "scan_interval_minutes")))
                ids = due_subscription_ids(session, deps.now(), interval)
        except Exception:
            log.exception("scheduler: listing due subscriptions failed")
            ids = []
        for sub_id in ids:
            if stop.is_set():
                break
            try:
                report = await scan_subscription(deps, sub_id)
                log.info("subscription %d scanned: %s", sub_id, report.summary())
            except Exception as exc:
                log.exception("subscription %d scan crashed", sub_id)
                with deps.session_factory() as session:
                    sub = session.get(Subscription, sub_id)
                    if sub is not None:
                        sub.last_scan_at = deps.now()
                        sub.last_scan_result = {"error": f"internal error: {exc!r}"}
                        session.commit()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=deps.scheduler_tick_seconds)
    log.info("scheduler stopped")
