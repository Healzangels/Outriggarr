"""Subscriptions → jobs. `scan_subscription` is one scan (also the GUI's preview when
dry_run=True); `run_scheduler` runs due subscriptions on the configured interval."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from outriggarr.arr.base import ArrError, EpisodeRef
from outriggarr.db.models import Connection, Job, JobStatus, Subscription, TargetKind, VideoMeta
from outriggarr.matcher import (
    Episode,
    MatchConfig,
    MatchResult,
    Override,
    Video,
    in_scope,
    match,
    videos_needing_dates,
)
from outriggarr.naming import episode_code
from outriggarr.settings import get_setting
from outriggarr.source import SourceError, VideoRef, is_rate_limited
from outriggarr.worker.runner import RunnerDeps, notify

log = logging.getLogger(__name__)

DATE_FETCH_LIMIT = 20  # per-video info fetches per scan, at most


@dataclass
class ScanReport:
    subscription_id: int
    scanned_at: datetime
    dry_run: bool
    manual: bool = False  # a Download button: the auto-download policy does not apply
    sources: int = 0  # how many sources the listing came from
    videos: list[dict] = field(default_factory=list)
    matches: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)  # paired, but the length check objects
    skipped_existing: list[dict] = field(default_factory=list)
    created_job_ids: list[int] = field(default_factory=list)
    in_scope: int | None = None  # listed videos whose title carries the required phrase
    error: str | None = None

    def summary(self) -> dict:
        return {
            "at": self.scanned_at.isoformat(),
            "videos": len(self.videos),
            "wanted": len(self.matches)
            + len(self.unmatched)
            + len(self.held)
            + len(self.skipped_existing),
            "matched": len(self.matches),
            "created": len(self.created_job_ids),
            "unmatched": len(self.unmatched),
            "held": len(self.held),
            "not_auto": sum(1 for m in self.matches if m.get("skipped") and not m.get("job_id")),
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
        air_date=e.air_date or (e.air_date_utc.date() if e.air_date_utc else None),
        runtime_minutes=e.runtime,
    )


def _video(v: VideoRef) -> Video:
    upload = None
    if v.upload_date and len(v.upload_date) == 8:
        with contextlib.suppress(ValueError):
            upload = datetime.strptime(v.upload_date, "%Y%m%d").date()
    return Video(id=v.id, title=v.title, url=v.url, upload_date=upload, duration=v.duration)


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
    session: Session,
    connection_id: int,
    series_id: int,
    overrides: dict[str, tuple[int, int]] | None = None,
) -> dict[int, Job]:
    """episode id → the job that already covers it: anything not `done`. Active jobs are
    in flight; failed and cancelled ones wait for the user's Retry. A done job is history —
    whether the episode needs a file again is Sonarr's `hasFile`, not ours.
    A cancelled or terminally failed job does NOT cover an episode the user has since
    pinned to a different video: the pin is the correction, it must be able to run."""
    rows = session.scalars(
        select(Job).where(
            Job.connection_id == connection_id,
            Job.series_id == series_id,
            Job.target_kind == TargetKind.episode,
            Job.status != JobStatus.done,
        )
    )
    pinned_video_by_episode: dict[tuple[int, int], str] = {}
    for vid, (season, number) in (overrides or {}).items():
        pinned_video_by_episode[(season, number)] = vid
    out: dict[int, Job] = {}
    for job in rows:
        terminal = job.status is JobStatus.cancelled or (
            job.status is JobStatus.failed and job.next_retry_at is None
        )
        for eid in job.episode_ids or []:
            if terminal and _pinned_elsewhere(job, int(eid), pinned_video_by_episode, session):
                continue
            out.setdefault(int(eid), job)
    return out


def _pinned_elsewhere(
    job: Job, episode_id: int, pins: dict[tuple[int, int], str], session: Session
) -> bool:
    """True if the user pinned this episode to a video other than the job's."""
    if not pins:
        return False
    code = job.target_label or ""
    # cheap path: the job's label carries SxxEyy; parse it rather than call Sonarr
    m = re.search(r"S(\d+)E(\d+)", code)
    if not m:
        return False
    key = (int(m.group(1)), int(m.group(2)))
    return key in pins and pins[key] != job.video_id


def live_video_ids_for_series(session: Session, connection_id: int, series_id: int) -> set[str]:
    """Videos already spoken for by a job that is not done: never re-match them."""
    return set(
        session.scalars(
            select(Job.video_id).where(
                Job.connection_id == connection_id,
                Job.series_id == series_id,
                Job.status != JobStatus.done,
            )
        )
    )


AUTO_DOWNLOAD = ("future", "all", "none")


def auto_queues(sub: Subscription, ep: Episode) -> bool:
    """Whether the scheduler queues this match by itself under the subscription's
    auto-download policy; Download buttons never ask."""
    if sub.auto_download == "all":
        return True
    if sub.auto_download == "future":
        return ep.air_date is not None and ep.air_date >= sub.created_at.date()
    return False


async def scan_subscription(
    deps: RunnerDeps,
    subscription_id: int,
    *,
    dry_run: bool = False,
    manual: bool = False,
    episode_ids: set[int] | None = None,
) -> ScanReport:
    """dry_run: report only. Otherwise queue jobs — every match the policy allows on a
    scheduled scan, or (manual) every match, or just `episode_ids`, from a button."""
    now = deps.now()
    with deps.session_factory() as session:
        sub = session.get(
            Subscription, subscription_id, options=[selectinload(Subscription.overrides)]
        )
        if sub is None:
            raise SubscriptionNotFound(subscription_id)
        report = ScanReport(subscription_id=sub.id, scanned_at=now, dry_run=dry_run, manual=manual)
        try:
            await _scan(deps, session, sub, report, now, episode_ids=episode_ids)
        except (ArrError, SourceError) as exc:
            report.error = str(exc)
            if isinstance(exc, SourceError) and is_rate_limited(report.error):
                # a listing hit the same wall a download would: pause everything, once
                deps.cooloff.hit(report.error)
        if not dry_run:
            previous_error = (sub.last_scan_result or {}).get("error")
            sub.last_scan_at = now
            sub.last_scan_result = report.summary()
            session.commit()
            if (
                report.error
                and report.error != previous_error
                and get_setting(session, "notify_on_scan_error") == "1"
            ):
                await notify(
                    deps,
                    "Outriggarr: scan error",
                    f"{sub.title} (subscription {sub.id})\n{report.error}",
                )
        return report


async def list_source_videos(deps: RunnerDeps, sub: Subscription, limit: int) -> list[VideoRef]:
    """Every source listed with the same depth, pooled once by video id (a video on both
    a channel and a playlist counts once). One source failing fails the whole listing:
    matching against a partial pool could turn an ambiguous pair into a confident
    wrong match."""
    refs: list[VideoRef] = []
    seen_ids: set[str] = set()
    for src in sub.sources:
        try:
            listed = await asyncio.to_thread(deps.source.list_recent, src, limit)
        except SourceError as exc:
            if len(sub.sources) == 1:
                raise  # verbatim, as always
            raise SourceError(f"{src}: {exc}") from exc  # say which source
        for ref in listed:
            if ref.id not in seen_ids:
                seen_ids.add(ref.id)
                refs.append(ref)
    return refs


async def _scan(
    deps: RunnerDeps,
    session: Session,
    sub: Subscription,
    report: ScanReport,
    now: datetime,
    episode_ids: set[int] | None = None,
) -> None:
    conn = sub.connection
    client = deps.arr_factory(conn)
    # A deleted/re-added series answers episodes() with [] and would scan as healthy;
    # ask for the series itself (404 → a visible, non-retryable scan error) and keep
    # the display title current.
    title = await client.series_title(sub.series_id)
    if title and title != sub.title:
        sub.title = title
        session.commit()  # a pending UPDATE would hold SQLite's write lock across the listing
    all_episodes = await client.episodes(sub.series_id)
    pinned = {(o.season, o.episode) for o in sub.overrides}
    wanted = [
        _episode(e)
        for e in all_episodes
        if e.monitored
        and not e.has_file
        and (
            (e.air_date_utc is not None and e.air_date_utc <= now)
            or (e.season_number, e.episode_number) in pinned  # a pin makes it wanted even undated
        )
    ]
    covered = existing_jobs_for_series(
        session, conn.id, sub.series_id, {o.video_id: (o.season, o.episode) for o in sub.overrides}
    )
    todo: list[Episode] = []
    for ep in wanted:
        job = covered.get(ep.id)
        if job is None:
            todo.append(ep)
        else:
            report.skipped_existing.append(
                {**_episode_dict(ep), "job_id": job.id, "job_status": job.status.value}
            )

    limit = sub.video_limit or int(get_setting(session, "scan_video_limit"))
    refs = await list_source_videos(deps, sub, limit)
    report.sources = len(sub.sources)
    ages = {r.id: r.approx_age for r in refs}  # "3 years ago" from the listing page, if any
    taken = live_video_ids_for_series(session, conn.id, sub.series_id)
    videos = [_video(v) for v in refs if v.id not in taken]
    _apply_cached_dates(session, videos)
    cfg = MatchConfig(
        strategies=tuple(sub.strategies or ()),
        date_tolerance_days=sub.date_tolerance_days,
        date_offset_days=sub.date_offset_days,
        title_regex=sub.title_regex,
        title_require=sub.title_require,
    )
    overrides = [Override(o.video_id, o.season, o.episode) for o in sub.overrides]
    listed = {v.id for v in videos}
    for o in sub.overrides:
        # An override given as a URL may point outside the listing: add it to the pool.
        if o.video_id not in listed and o.video_url:
            videos.append(Video(id=o.video_id, title=o.video_title or o.video_id, url=o.video_url))
            listed.add(o.video_id)

    result = match(todo, videos, overrides, cfg)
    need = videos_needing_dates(result, videos, cfg, overrides)
    if need and not any(x.episode.air_date for x in (*result.unmatched, *result.held)):
        need = []  # nothing to compare a date against
    known = known_date_ids(session, [v.id for v in need], now)
    need = [v for v in need if v.id not in known][:DATE_FETCH_LIMIT]
    if need:
        by_id = {v.id: v for v in videos}
        learned: dict[str, str | None] = {}
        for v in need:  # network calls: NO writes here (SQLite has one write lock)
            try:
                info = await asyncio.to_thread(deps.source.fetch_info, v.url)
            except SourceError as exc:
                log.warning("subscription %d: fetch_info(%s) failed: %s", sub.id, v.id, exc)
                learned[v.id] = None
                continue
            by_id[v.id] = _video(info)
            learned[v.id] = info.upload_date
        with session.no_autoflush:
            for vid, upload_date in learned.items():
                _remember_date(session, vid, upload_date)
        session.commit()  # one short write transaction
        videos = list(by_id.values())
        result = match(todo, videos, overrides, cfg)

    report.videos = [
        {
            "id": v.id,
            "title": v.title,
            "url": v.url,
            "upload_date": v.upload_date.isoformat() if v.upload_date else None,
            "approx_age": ages.get(v.id),
        }
        for v in videos
    ]
    if sub.title_require:
        report.in_scope = sum(1 for v in videos if in_scope(v.title, sub.title_require))
    _fill_report(report, result, videos)
    for entry, m in zip(report.matches, result.matches, strict=True):
        entry["auto"] = auto_queues(sub, m.episode)
    if not report.dry_run:
        if report.manual:

            def allowed(ep: Episode) -> bool:
                return episode_ids is None or ep.id in episode_ids

        else:

            def allowed(ep: Episode) -> bool:
                return auto_queues(sub, ep)

        _create_jobs(session, sub, result, report, allowed=allowed)


DATE_RETRY_AFTER = timedelta(days=7)  # an undated fetch is retried a week later


def _apply_cached_dates(session: Session, videos: list[Video]) -> None:
    ids = [v.id for v in videos if v.upload_date is None]
    if not ids:
        return
    rows = {
        m.video_id: m for m in session.scalars(select(VideoMeta).where(VideoMeta.video_id.in_(ids)))
    }
    for i, v in enumerate(videos):
        m = rows.get(v.id)
        if m is not None and m.upload_date and v.upload_date is None:
            with contextlib.suppress(ValueError):
                videos[i] = Video(
                    id=v.id,
                    title=v.title,
                    url=v.url,
                    duration=v.duration,
                    upload_date=datetime.strptime(m.upload_date, "%Y%m%d").date(),
                )


def known_date_ids(session: Session, video_ids: list[str], now: datetime | None = None) -> set[str]:
    """The subset of `video_ids` whose date is known (cached, or asked recently enough
    that a fetch would skip it) — one query, for pages that count hundreds at a time."""
    if not video_ids:
        return set()
    at = now or datetime.now(UTC)
    rows = session.scalars(select(VideoMeta).where(VideoMeta.video_id.in_(video_ids)))
    return {m.video_id for m in rows if m.upload_date or at - m.fetched_at < DATE_RETRY_AFTER}


def _date_known(session: Session, video_id: str, now: datetime | None = None) -> bool:
    m = session.get(VideoMeta, video_id)
    if m is None:
        return False
    if m.upload_date:
        return True
    return (now or datetime.now(UTC)) - m.fetched_at < DATE_RETRY_AFTER


def _remember_date(session: Session, video_id: str, upload_date: str | None) -> None:
    m = session.get(VideoMeta, video_id)
    if m is None:
        session.add(
            VideoMeta(video_id=video_id, upload_date=upload_date, fetched_at=datetime.now(UTC))
        )
    else:
        m.upload_date, m.fetched_at = upload_date, datetime.now(UTC)


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
                "tier": m.tier,
                "job_id": None,
            }
        )
    shown: set[int] = set()
    for h in result.held:
        if h.episode.id in shown:
            continue  # two strategies can hold one episode; the first hold is the one shown
        shown.add(h.episode.id)
        report.held.append(
            {
                **_episode_dict(h.episode),
                "video_id": h.video.id,
                "video_title": h.video.title,
                "video_url": h.video.url,
                "strategy": h.strategy,
                "tier": h.tier,
                "reason": h.reason,
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
    session: Session,
    sub: Subscription,
    result: MatchResult,
    report: ScanReport,
    *,
    allowed: Callable[[Episode], bool] = lambda ep: True,
) -> None:
    for entry, m in zip(report.matches, result.matches, strict=True):
        ep = m.episode
        if not allowed(ep):
            entry["job_id"] = None
            entry["skipped"] = (
                "not selected" if report.manual else f"not automatic ({sub.auto_download})"
            )
            continue
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
            matched_by=m.tier if m.strategy == "title" else m.strategy,
            video_duration=m.video.duration,
            target_runtime=m.episode.runtime_minutes,
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
        .join(Connection, Connection.id == Subscription.connection_id)
        .where(Subscription.enabled, Connection.enabled)
        .where(or_(Subscription.last_scan_at.is_(None), Subscription.last_scan_at <= cutoff))
        .order_by(Subscription.last_scan_at.nulls_first(), Subscription.id)
    )
    return list(rows)


async def run_scheduler(deps: RunnerDeps, stop: asyncio.Event) -> None:
    log.info("scheduler started")
    paused_logged = False
    while not stop.is_set():
        if deps.cooloff.active():
            # rate-limited: a scan would fail the same way and stamp a scan error on every
            # subscription; wait it out (the scans stay due, so they run once it lifts)
            if not paused_logged:
                log.warning(
                    "scheduler: the source rate-limited us; scans paused for %d s",
                    int(deps.cooloff.remaining()),
                )
                paused_logged = True
            ids = []
        else:
            paused_logged = False
            try:
                with deps.session_factory() as session:
                    interval = timedelta(minutes=int(get_setting(session, "scan_interval_minutes")))
                    ids = due_subscription_ids(session, deps.now(), interval)
            except Exception:
                log.exception("scheduler: listing due subscriptions failed")
                ids = []
        for sub_id in ids:
            if stop.is_set() or deps.cooloff.active():
                break  # rate-limited mid-batch: the rest would fail the same way
            try:
                report = await scan_subscription(deps, sub_id)
                log.info("subscription %d scanned: %s", sub_id, report.summary())
            except Exception as exc:
                log.exception("subscription %d scan crashed", sub_id)
                try:
                    with deps.session_factory() as session:
                        sub = session.get(Subscription, sub_id)
                        if sub is not None:
                            previous = (sub.last_scan_result or {}).get("error")
                            message = f"internal error: {exc!r}"
                            sub.last_scan_at = deps.now()
                            sub.last_scan_result = {"error": message}
                            session.commit()
                            if message != previous and (
                                get_setting(session, "notify_on_scan_error") == "1"
                            ):
                                await notify(
                                    deps,
                                    "Outriggarr: scan error",
                                    f"{sub.title} (subscription {sub.id})\n{message}",
                                )
                except Exception:
                    log.exception("recording the crash for subscription %d failed", sub_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=deps.scheduler_tick_seconds)
    log.info("scheduler stopped")
