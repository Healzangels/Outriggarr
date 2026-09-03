from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from outriggarr.arr.base import ArrError, EpisodeRef
from outriggarr.db.models import Connection, ConnectionKind, Job, JobStatus, Override, Subscription
from outriggarr.matcher import Episode, MatchConfig, Video, match
from outriggarr.settings import set_setting
from outriggarr.source import SourceError, VideoRef
from outriggarr.worker.runner import RunnerDeps
from outriggarr.worker.scheduler import (
    ScanReport,
    SubscriptionNotFound,
    _apply_cached_dates,
    _fill_report,
    _remember_date,
    due_subscription_ids,
    run_scheduler,
    scan_subscription,
)
from tests.fakes import FakeArrFactory, FakeVideoSource

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _ep(i, s, n, title, air, has_file=False, monitored=True):
    return EpisodeRef(i, s, n, title, has_file, monitored, air)


EPISODES = [
    _ep(11, 30, 6, "Six Spicy Wings", NOW - timedelta(days=10)),
    _ep(12, 30, 7, "Seven Spicy Wings", NOW - timedelta(days=3)),
    _ep(13, 30, 8, "Eight", NOW + timedelta(days=4)),  # unaired
    _ep(14, 30, 5, "Five", NOW - timedelta(days=17), has_file=True),
    _ep(15, 30, 4, "Four", NOW - timedelta(days=24), monitored=False),
    _ep(16, 30, 9, "Nine Spicy Wings", NOW - timedelta(days=1)),
]
RECENT = [
    VideoRef("v7", "Seven Spicy Wings | Show", "https://y/v7", 100, 1, None),
    VideoRef("v6", "Six Spicy Wings | Show", "https://y/v6", 100, 2, None),
    VideoRef("vx", "Lineup reveal", "https://y/vx", 50, 3, None),
]


@pytest.fixture
def deps(session_factory, tmp_path: Path):
    d = RunnerDeps(
        session_factory=session_factory,
        arr_factory=FakeArrFactory(),
        source=FakeVideoSource(),
        staging_dir=tmp_path / "staging",
        poll_seconds=0.01,
        scheduler_tick_seconds=0.01,
        now=lambda: NOW,
    )
    d.source.recent = list(RECENT)
    return d


def make_sub(session_factory, **over) -> tuple[int, int]:
    with session_factory() as s:
        conn = Connection(
            kind=ConnectionKind.sonarr,
            name="s",
            url="http://sonarr-host:1",
            api_key="k",
            staging_path_remote="/data/outriggarr",
        )
        sub = Subscription(
            connection=conn,
            series_id=5,
            tvdb_id=1,
            title="Show",
            sources=["https://www.youtube.com/@show"],
            strategies=["title"],
            auto_download="all",  # these tests are about matching; the policy has its own
        )
        for k, v in over.items():
            setattr(sub, k, v)
        s.add(sub)
        s.commit()
        return sub.id, conn.id


def fake_client(deps: RunnerDeps, conn_id: int):
    with deps.session_factory() as s:
        client = deps.arr_factory(s.get(Connection, conn_id))
    client.episodes_by_series[5] = list(EPISODES)
    return client


async def test_scan_creates_jobs_for_matches(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory, format="best[height<=720]")
    fake_client(deps, conn_id)

    report = await scan_subscription(deps, sub_id)

    assert report.error is None
    assert deps.source.listed == [("https://www.youtube.com/@show", 50)]
    assert {(m["code"], m["video_id"], m["strategy"]) for m in report.matches} == {
        ("S30E06", "v6", "title"),
        ("S30E07", "v7", "title"),
    }
    assert [u["code"] for u in report.unmatched] == ["S30E09"]
    assert report.unmatched[0]["candidates"] == {"override": [], "title": []}
    assert len(report.created_job_ids) == 2
    with session_factory() as s:
        jobs = list(s.query(Job).order_by(Job.id))
        assert [(j.episode_ids, j.video_id, j.format, j.subscription_id) for j in jobs] == [
            ([11], "v6", "best[height<=720]", sub_id),
            ([12], "v7", "best[height<=720]", sub_id),
        ]
        assert jobs[0].target_label == "Show S30E06 - Six Spicy Wings"
        assert jobs[0].status is JobStatus.queued
        sub = s.get(Subscription, sub_id)
        assert sub.last_scan_at == NOW
        assert sub.last_scan_result["created"] == 2 and sub.last_scan_result["unmatched"] == 1
        assert sub.last_scan_result["wanted"] == 3


async def test_dry_run_creates_nothing_and_does_not_stamp(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    report = await scan_subscription(deps, sub_id, dry_run=True)
    assert len(report.matches) == 2 and report.created_job_ids == []
    assert all(m["job_id"] is None for m in report.matches)
    with session_factory() as s:
        assert s.query(Job).count() == 0
        assert s.get(Subscription, sub_id).last_scan_at is None


async def test_scan_covers_non_done_jobs_and_requeues_after_done(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    await scan_subscription(deps, sub_id)
    with session_factory() as s:
        done = s.query(Job).filter(Job.video_id == "v6").one()
        done.status = JobStatus.done  # imported; Sonarr later loses the file (hasFile False)
        failed_terminal = s.query(Job).filter(Job.video_id == "v7").one()
        failed_terminal.status = JobStatus.failed
        failed_terminal.next_retry_at = None
        s.commit()

    report = await scan_subscription(deps, sub_id)
    # the terminally failed job still covers its episode (Retry is the user's call)
    assert [(x["code"], x["job_status"]) for x in report.skipped_existing] == [("S30E07", "failed")]
    # the done job does not: the episode is wanted again in Sonarr, so a NEW job is queued
    (m,) = [m for m in report.matches if m["code"] == "S30E06"]
    assert m["job_id"] is not None and m["job_id"] != done.id
    assert report.created_job_ids == [m["job_id"]]
    with session_factory() as s:
        assert s.query(Job).filter(Job.video_id == "v6").count() == 2
        assert s.get(Job, m["job_id"]).status is JobStatus.queued


async def test_cancelled_job_still_covers_until_retried(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    await scan_subscription(deps, sub_id)
    with session_factory() as s:
        for j in s.query(Job):
            j.status = JobStatus.cancelled
        s.commit()
    report = await scan_subscription(deps, sub_id)
    assert {x["job_status"] for x in report.skipped_existing} == {"cancelled"}
    assert report.created_job_ids == []


async def test_override_and_date_strategy_fetch_only_unmatched(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory, strategies=["title", "date"], date_tolerance_days=1)
    fake_client(deps, conn_id)
    with session_factory() as s:
        s.add(Override(subscription_id=sub_id, video_id="vx", season=30, episode=9))
        s.commit()
    deps.source.recent = [
        VideoRef("v7", "Seven Spicy Wings | Show", "https://y/v7", 100, 1, None),
        VideoRef("u1", "Unrelated title", "https://y/u1", 100, 2, None),
        VideoRef("vx", "Lineup reveal", "https://y/vx", 50, 3, None),
    ]
    deps.source.infos["https://y/u1"] = VideoRef(
        "u1",
        "Unrelated title",
        "https://y/u1",
        100,
        2,
        (NOW - timedelta(days=10)).strftime("%Y%m%d"),
    )

    report = await scan_subscription(deps, sub_id)
    by_code = {m["code"]: m for m in report.matches}
    assert by_code["S30E09"]["strategy"] == "override" and by_code["S30E09"]["video_id"] == "vx"
    assert by_code["S30E07"]["strategy"] == "title"
    assert by_code["S30E06"]["strategy"] == "date" and by_code["S30E06"]["video_id"] == "u1"
    assert deps.source.fetched == ["https://y/u1"], "only the undated, unassigned video was fetched"
    assert report.unmatched == []


async def test_fetch_info_failure_is_tolerated(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory, strategies=["title", "date"])
    fake_client(deps, conn_id)
    deps.source.recent = [VideoRef("u1", "Unrelated", "https://y/u1", 1, 1, None)]
    report = await scan_subscription(deps, sub_id)
    assert report.error is None
    assert deps.source.fetched == ["https://y/u1"]
    assert len(report.unmatched) == 3


async def test_source_and_arr_errors_land_in_report_and_last_scan(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    client = fake_client(deps, conn_id)
    deps.source.recent_error = SourceError(
        "ERROR: [youtube:tab] @show: This channel does not exist"
    )
    report = await scan_subscription(deps, sub_id)
    assert report.error == "ERROR: [youtube:tab] @show: This channel does not exist"
    with session_factory() as s:
        assert s.get(Subscription, sub_id).last_scan_result["error"] == report.error

    deps.source.recent_error = None

    async def boom(series_id):
        raise ArrError("GET http://sonarr-host:1/api/v3/episode?seriesId=5 -> HTTP 500: x")

    client.episodes = boom
    report = await scan_subscription(deps, sub_id)
    assert report.error.endswith("HTTP 500: x")
    with pytest.raises(SubscriptionNotFound):
        await scan_subscription(deps, 999)


def test_due_subscription_ids(session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    with session_factory() as s:
        conn = s.get(Connection, conn_id)
        fresh = Subscription(
            connection=conn,
            series_id=6,
            title="F",
            sources=["https://x"],
            strategies=[],
            last_scan_at=NOW - timedelta(minutes=5),
        )
        stale = Subscription(
            connection=conn,
            series_id=7,
            title="S",
            sources=["https://x"],
            strategies=[],
            last_scan_at=NOW - timedelta(minutes=45),
        )
        off = Subscription(
            connection=conn,
            series_id=8,
            title="O",
            sources=["https://x"],
            strategies=[],
            enabled=False,
        )
        s.add_all([fresh, stale, off])
        s.commit()
        ids = due_subscription_ids(s, NOW, timedelta(minutes=30))
        assert ids == [sub_id, stale.id]  # never-scanned first, then oldest


async def test_run_scheduler_scans_due_and_records_crashes(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    with session_factory() as s:
        set_setting(s, "scan_interval_minutes", "30")
        s.commit()
    stop = asyncio.Event()
    task = asyncio.create_task(run_scheduler(deps, stop))
    for _ in range(200):
        await asyncio.sleep(0.01)
        with session_factory() as s:
            if s.get(Subscription, sub_id).last_scan_at is not None:
                break
    stop.set()
    await asyncio.wait_for(task, 2)
    with session_factory() as s:
        assert s.query(Job).count() == 2
        assert s.get(Subscription, sub_id).last_scan_result["created"] == 2

    # a crash inside a scan is recorded and does not kill the loop
    with session_factory() as s:
        sub = s.get(Subscription, sub_id)
        sub.last_scan_at = None
        s.commit()
    deps.source.list_recent = lambda url, limit: (_ for _ in ()).throw(RuntimeError("bug"))
    stop = asyncio.Event()
    task = asyncio.create_task(run_scheduler(deps, stop))
    for _ in range(200):
        await asyncio.sleep(0.01)
        with session_factory() as s:
            if s.get(Subscription, sub_id).last_scan_at is not None:
                break
    stop.set()
    await asyncio.wait_for(task, 2)
    with session_factory() as s:
        assert "internal error" in s.get(Subscription, sub_id).last_scan_result["error"]


async def test_url_override_outside_listing_is_matched(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    deps.source.recent = [
        VideoRef("vx", "Lineup reveal", "https://y/vx", 50, 1, None)
    ]  # nothing matches
    with session_factory() as s:
        s.add(
            Override(
                subscription_id=sub_id,
                video_id="old1",
                season=30,
                episode=9,
                video_url="https://y/old1",
                video_title="An older upload of Nine",
            )
        )
        s.add(
            Override(subscription_id=sub_id, video_id="ghost", season=30, episode=7)
        )  # no URL, not listed
        s.commit()
    report = await scan_subscription(deps, sub_id)
    by_code = {m["code"]: m for m in report.matches}
    assert by_code["S30E09"]["strategy"] == "override"
    assert (
        by_code["S30E09"]["video_id"] == "old1"
        and by_code["S30E09"]["video_url"] == "https://y/old1"
    )
    assert "S30E07" in {u["code"] for u in report.unmatched}, (
        "an id-only override needs the listing"
    )
    with session_factory() as s:
        job = s.query(Job).filter(Job.video_id == "old1").one()
        assert job.video_url == "https://y/old1" and job.video_title == "An older upload of Nine"


async def test_scan_error_notified_once_per_new_error(deps, session_factory) -> None:
    from tests.fakes import FakeNotifier

    deps.notifier = FakeNotifier()
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    deps.source.recent_error = SourceError("ERROR: channel gone")
    await scan_subscription(deps, sub_id)
    await scan_subscription(deps, sub_id)  # same error again → no second message
    assert [t for t, _ in deps.notifier.sent] == ["Outriggarr: scan error"]
    assert "channel gone" in deps.notifier.sent[0][1] and "Show" in deps.notifier.sent[0][1]
    deps.source.recent_error = SourceError("ERROR: something else")
    await scan_subscription(deps, sub_id)
    assert len(deps.notifier.sent) == 2
    deps.source.recent_error = None
    await scan_subscription(deps, sub_id)  # recovery is quiet
    await scan_subscription(deps, sub_id, dry_run=True)
    assert len(deps.notifier.sent) == 2
    with session_factory() as s:
        set_setting(s, "notify_on_scan_error", "0")
        s.commit()
    deps.source.recent_error = SourceError("ERROR: muted")
    await scan_subscription(deps, sub_id)
    assert len(deps.notifier.sent) == 2


# ---- audit fixes ----------------------------------------------------------------


async def test_live_job_videos_leave_the_pool(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory, strategies=["title", "date"], date_tolerance_days=3)
    fake_client(deps, conn_id)
    await scan_subscription(deps, sub_id)  # v6→S30E06, v7→S30E07 queued
    # v7 now also looks like a date candidate for S30E09; it must not be re-matched
    deps.source.recent = [
        VideoRef(
            "v7",
            "Seven Spicy Wings | Show",
            "https://y/v7",
            100,
            1,
            (NOW - timedelta(days=1)).strftime("%Y%m%d"),
        )
    ]
    report = await scan_subscription(deps, sub_id)
    assert report.matches == [] and [u["code"] for u in report.unmatched] == ["S30E09"]
    assert report.unmatched[0]["candidates"]["date"] == []


async def test_upload_dates_are_cached_and_the_fetch_window_moves(deps, session_factory) -> None:
    from outriggarr.db.models import VideoMeta
    from outriggarr.worker import scheduler

    sub_id, conn_id = make_sub(session_factory, strategies=["date"], date_tolerance_days=1)
    fake_client(deps, conn_id)
    deps.source.recent = [
        VideoRef(f"u{i}", f"Unrelated {i}", f"https://y/u{i}", 1, i, None) for i in range(25)
    ]
    for i in range(25):
        deps.source.infos[f"https://y/u{i}"] = VideoRef(
            f"u{i}", f"Unrelated {i}", f"https://y/u{i}", 1, i, "20200101"
        )
    deps.source.infos["https://y/u24"] = VideoRef(
        "u24", "Unrelated 24", "https://y/u24", 1, 24, (NOW - timedelta(days=10)).strftime("%Y%m%d")
    )
    # a fetch that yields no date at all must be remembered too (not re-fetched every scan)
    deps.source.infos["https://y/u3"] = VideoRef("u3", "Unrelated 3", "https://y/u3", 1, 3, None)
    deps_limit = scheduler.DATE_FETCH_LIMIT
    r1 = await scan_subscription(deps, sub_id)
    assert len(deps.source.fetched) == deps_limit and r1.matches == []
    r2 = await scan_subscription(deps, sub_id)  # the next 5, never the first 20 again
    assert len(deps.source.fetched) == 25
    assert [m["video_id"] for m in r2.matches] == ["u24"]
    with session_factory() as s:
        assert s.query(VideoMeta).count() == 25
    r3 = await scan_subscription(deps, sub_id, dry_run=True)
    assert len(deps.source.fetched) == 25, "dry runs use the cache too"
    assert r3.dry_run
    assert deps.source.fetched.count("https://y/u3") == 1, (
        "a fetch that yielded no date is remembered too"
    )


async def test_no_date_fetch_when_no_unmatched_episode_has_an_air_date(
    deps, session_factory
) -> None:
    sub_id, conn_id = make_sub(session_factory, strategies=["date"])
    client = fake_client(deps, conn_id)
    client.episodes_by_series[5] = [_ep(99, 30, 1, "Undated", None)]  # never wanted anyway
    with session_factory() as s:
        s.add(Override(subscription_id=sub_id, video_id="vx", season=30, episode=1))
        s.commit()
    deps.source.recent = [VideoRef("u1", "Unrelated", "https://y/u1", 1, 1, None)]
    report = await scan_subscription(deps, sub_id, dry_run=True)
    assert deps.source.fetched == [] and [u["code"] for u in report.unmatched] == ["S30E01"]


async def test_pinned_undated_episode_becomes_wanted(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    client = fake_client(deps, conn_id)
    client.episodes_by_series[5] = [_ep(99, 30, 50, "No Date Yet", None)]
    deps.source.recent = [VideoRef("vx", "whatever", "https://y/vx", 1, 1, None)]
    assert (await scan_subscription(deps, sub_id, dry_run=True)).unmatched == []
    with session_factory() as s:
        s.add(Override(subscription_id=sub_id, video_id="vx", season=30, episode=50))
        s.commit()
    report = await scan_subscription(deps, sub_id)
    assert [(m["code"], m["strategy"]) for m in report.matches] == [("S30E50", "override")]


async def test_pin_to_another_video_uncovers_a_cancelled_wrong_job(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    await scan_subscription(deps, sub_id)  # v6 → S30E06 queued
    with session_factory() as s:
        wrong = s.query(Job).filter(Job.video_id == "v6").one()
        wrong.status = JobStatus.cancelled  # the user saw it was the wrong video
        s.add(Override(subscription_id=sub_id, video_id="vx", season=30, episode=6))
        s.commit()
    report = await scan_subscription(deps, sub_id)
    (m,) = [m for m in report.matches if m["code"] == "S30E06"]
    assert m["video_id"] == "vx" and m["strategy"] == "override" and m["job_id"]
    with session_factory() as s:
        assert s.query(Job).filter(Job.video_id == "vx").count() == 1


async def test_scheduler_survives_a_failing_crash_stamp(deps, session_factory, monkeypatch) -> None:
    from outriggarr.worker import scheduler

    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    deps.source.list_recent = lambda url, limit: (_ for _ in ()).throw(RuntimeError("bug"))
    calls = {"n": 0}
    real = deps.session_factory

    def flaky_factory():
        calls["n"] += 1
        if calls["n"] == 3:  # the session used to stamp the crash
            raise RuntimeError("database is locked")
        return real()

    monkeypatch.setattr(deps, "session_factory", flaky_factory)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run_scheduler(deps, stop))
    await asyncio.sleep(0.15)
    assert not task.done(), "the loop must survive"
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_internal_scan_error_is_notified_once(deps, session_factory) -> None:
    from tests.fakes import FakeNotifier

    deps.notifier = FakeNotifier()
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    deps.source.list_recent = lambda url, limit: (_ for _ in ()).throw(RuntimeError("bug"))
    stop = asyncio.Event()
    task = asyncio.create_task(run_scheduler(deps, stop))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(deps.notifier.sent) >= 1:
            break
    with session_factory() as s:
        s.get(Subscription, sub_id).last_scan_at = None  # make it due again, same error
        s.commit()
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert [t for t, _ in deps.notifier.sent] == ["Outriggarr: scan error"]


# ---- discovery fixes -------------------------------------------------------------


async def test_date_fetches_do_not_hold_the_write_lock(deps, session_factory) -> None:
    """No VideoMeta row may be written while a fetch is still outstanding."""
    from outriggarr.db.models import VideoMeta

    sub_id, conn_id = make_sub(session_factory, strategies=["date"])
    fake_client(deps, conn_id)
    deps.source.recent = [
        VideoRef(f"u{i}", f"Unrelated {i}", f"https://y/u{i}", 1, i, None) for i in range(3)
    ]
    for i in range(3):
        deps.source.infos[f"https://y/u{i}"] = VideoRef(
            f"u{i}", f"Unrelated {i}", f"https://y/u{i}", 1, i, "20200101"
        )
    rows_seen: list[int] = []
    real = deps.source.fetch_info

    def spy(url):
        with session_factory() as s:  # an independent writer must not be blocked
            rows_seen.append(s.query(VideoMeta).count())
            s.execute(__import__("sqlalchemy").text("UPDATE setting SET value=value WHERE key='x'"))
            s.commit()
        return real(url)

    deps.source.fetch_info = spy
    await scan_subscription(deps, sub_id)
    assert rows_seen == [0, 0, 0], "dates are written after the network calls, in one go"
    with session_factory() as s:
        assert s.query(VideoMeta).count() == 3


async def test_disabled_connection_subscriptions_are_not_due(session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    with session_factory() as s:
        assert due_subscription_ids(s, NOW, timedelta(minutes=30)) == [sub_id]
        s.get(Connection, conn_id).enabled = False
        s.commit()
        assert due_subscription_ids(s, NOW, timedelta(minutes=30)) == []


async def test_deleted_series_is_a_visible_scan_error_and_title_refreshes(
    deps, session_factory
) -> None:
    sub_id, conn_id = make_sub(session_factory)
    client = fake_client(deps, conn_id)
    client.series_titles[5] = "Show (Renamed)"
    report = await scan_subscription(deps, sub_id)
    assert report.error is None
    with session_factory() as s:
        assert s.get(Subscription, sub_id).title == "Show (Renamed)"
    client.series_title_error = ArrError("GET series/5 -> HTTP 404: not found", retryable=False)
    client.episodes_by_series[5] = []
    report = await scan_subscription(deps, sub_id)
    assert report.error and "404" in report.error
    with session_factory() as s:
        assert s.get(Subscription, sub_id).last_scan_result["error"] == report.error


async def test_local_air_date_is_preferred_for_date_matching(deps, session_factory) -> None:
    from datetime import date

    sub_id, conn_id = make_sub(session_factory, strategies=["date"], date_tolerance_days=0)
    client = fake_client(deps, conn_id)
    # aired 2026-08-20 local, which is 2026-08-21 in UTC (an evening US show)
    client.episodes_by_series[5] = [
        EpisodeRef(
            11,
            30,
            6,
            "Six Spicy Wings",
            False,
            True,
            datetime(2026, 8, 21, 2, 0, tzinfo=UTC),
            date(2026, 8, 20),
        )
    ]
    deps.source.recent = [VideoRef("v", "Anything", "https://y/v", 1, 1, "20260820")]
    report = await scan_subscription(deps, sub_id, dry_run=True)
    assert [(m["code"], m["strategy"]) for m in report.matches] == [("S30E06", "date")]


async def test_subscription_video_limit_overrides_the_global_setting(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory, video_limit=1200)
    fake_client(deps, conn_id)
    report = await scan_subscription(deps, sub_id)
    assert report.error is None
    assert deps.source.listed == [("https://www.youtube.com/@show", 1200)]


async def test_length_mismatch_is_held_not_queued_and_jobs_carry_the_evidence(
    deps, session_factory
) -> None:
    from outriggarr.db.models import Job

    sub_id, conn_id = make_sub(session_factory)
    client = fake_client(deps, conn_id)
    # S30E07 runs 30 min on TVDB; its containment candidate is a 100-second video
    client.episodes_by_series[5] = [
        EpisodeRef(11, 30, 6, "Six Spicy Wings", False, True, NOW - timedelta(days=10)),
        EpisodeRef(
            12, 30, 7, "Seven Spicy Wings", False, True, NOW - timedelta(days=3), runtime=30
        ),
    ]
    report = await scan_subscription(deps, sub_id)
    assert report.error is None
    assert [(h["code"], h["video_id"], h["tier"]) for h in report.held] == [
        ("S30E07", "v7", "contains")
    ]
    assert report.held[0]["reason"] == "video runs 1m40s, Sonarr says the episode runs 30 min"
    assert [m["code"] for m in report.matches] == ["S30E06"] and not report.unmatched
    assert report.summary()["held"] == 1 and report.summary()["wanted"] == 2
    with session_factory() as s:
        jobs = s.query(Job).all()
        assert [j.target_label[-15:] for j in jobs] == ["Six Spicy Wings"]
        assert (jobs[0].matched_by, jobs[0].video_duration, jobs[0].target_runtime) == (
            "contains",
            100,
            None,
        )


async def test_every_source_is_listed_and_videos_are_pooled_once(deps, session_factory) -> None:
    from outriggarr.source import SourceError

    sub_id, conn_id = make_sub(
        session_factory, sources=["https://www.youtube.com/@show", "https://www.youtube.com/@extra"]
    )
    fake_client(deps, conn_id)
    # the fake lists the same videos for any URL: a video on both sources counts once
    report = await scan_subscription(deps, sub_id)
    assert report.error is None
    assert deps.source.listed == [
        ("https://www.youtube.com/@show", 50),
        ("https://www.youtube.com/@extra", 50),
    ]
    assert report.sources == 2 and len(report.videos) == len(RECENT)
    assert len(report.matches) == 2 and report.summary()["created"] == 2

    # one source failing fails the scan, names the source, and queues nothing
    deps.source.recent_error = SourceError("ERROR: [youtube:tab] @x: This channel does not exist")
    r2 = await scan_subscription(deps, sub_id)
    assert r2.error.startswith("https://www.youtube.com/@show: ERROR: [youtube:tab]")
    assert r2.created_job_ids == [] and r2.matches == []


async def test_auto_download_policy_gates_scheduled_scans_only(deps, session_factory) -> None:
    from outriggarr.db.models import Job

    # "future": only episodes airing from the subscription's creation on queue by themselves
    sub_id, conn_id = make_sub(
        session_factory, auto_download="future", created_at=NOW - timedelta(days=1)
    )
    client = fake_client(deps, conn_id)
    client.episodes_by_series[5] = [
        EpisodeRef(11, 30, 6, "Six Spicy Wings", False, True, NOW - timedelta(days=10)),  # backlog
        EpisodeRef(12, 30, 7, "Seven Spicy Wings", False, True, NOW - timedelta(hours=1)),
    ]
    report = await scan_subscription(deps, sub_id)  # a scheduled scan
    assert {m["code"]: m["auto"] for m in report.matches} == {"S30E06": False, "S30E07": True}
    assert [m["code"] for m in report.matches if m["job_id"]] == ["S30E07"]
    assert report.matches[0]["skipped"] == "not automatic (future)"
    assert report.summary()["not_auto"] == 1 and report.summary()["created"] == 1

    # a dry run reports the same policy view and queues nothing
    dry = await scan_subscription(deps, sub_id, dry_run=True)
    assert dry.created_job_ids == [] and {m["code"]: m["auto"] for m in dry.matches} == {
        "S30E06": False
    }

    # a manual download ignores the policy: only the selected episode, or all of them
    picked = await scan_subscription(deps, sub_id, manual=True, episode_ids={999})
    assert picked.created_job_ids == [] and picked.matches[0]["skipped"] == "not selected"
    picked = await scan_subscription(deps, sub_id, manual=True, episode_ids={11})
    assert len(picked.created_job_ids) == 1
    with session_factory() as s:
        assert sorted(j.episode_ids[0] for j in s.query(Job).all()) == [11, 12]

    # "none": scheduled scans queue nothing at all
    sub2, conn2 = make_sub(session_factory, auto_download="none", series_id=6)
    c2 = fake_client(deps, conn2)
    c2.episodes_by_series[6] = [EpisodeRef(21, 30, 6, "Six Spicy Wings", False, True, NOW)]
    r2 = await scan_subscription(deps, sub2)
    assert r2.created_job_ids == [] and r2.matches[0]["skipped"] == "not automatic (none)"
    everything = await scan_subscription(deps, sub2, manual=True)
    assert len(everything.created_job_ids) == 1, "Download all ignores the policy"


async def test_rate_limited_listing_pauses_the_scans(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    t = [0.0]
    deps.cooloff.clock = lambda: t[0]
    deps.source.recent_error = SourceError(
        "ERROR: [youtube] tab: This content isn't available, try again later. "
        "The current session has been rate-limited by YouTube for up to an hour."
    )
    report = await scan_subscription(deps, sub_id)
    assert "rate-limited" in report.error and deps.cooloff.active()
    # while the pause holds the scheduler leaves due subscriptions alone: no scan, no
    # scan-error stamped on each of them; once it lifts they run as usual
    with session_factory() as s:
        sub = s.get(Subscription, sub_id)
        sub.last_scan_at = None
        sub.last_scan_result = None
        s.commit()
    deps.source.recent_error = None
    stop = asyncio.Event()
    task = asyncio.create_task(run_scheduler(deps, stop))
    await asyncio.sleep(0.1)
    with session_factory() as s:
        assert s.get(Subscription, sub_id).last_scan_at is None, "paused"
    t[0] += 900
    for _ in range(200):
        await asyncio.sleep(0.01)
        with session_factory() as s:
            if s.get(Subscription, sub_id).last_scan_at is not None:
                break
    stop.set()
    await asyncio.wait_for(task, 2)
    with session_factory() as s:
        result = s.get(Subscription, sub_id).last_scan_result
    assert result is not None and result.get("error") is None and result["created"] == 2


def test_report_shows_one_held_row_per_episode() -> None:
    from datetime import date

    ep = Episode(1, 1, 1, "Alpha Beta", date(2026, 1, 8), runtime_minutes=60)
    videos = [
        Video("a", "Alpha Beta (Part 1)", "https://x/a"),
        Video("b", "Unrelated", "https://x/b", date(2026, 1, 8), duration=120),
    ]
    result = match([ep], videos, [], MatchConfig(("title", "date"), date_tolerance_days=0))
    assert len(result.held) == 2, "the matcher keeps both holds for accounting"
    report = ScanReport(subscription_id=1, scanned_at=NOW, dry_run=True)
    _fill_report(report, result, videos)
    assert [(h["video_id"], h["strategy"]) for h in report.held] == [("a", "title")], (
        "the preview shows the hold that took the video, once"
    )


def test_cached_dates_keep_the_duration_so_the_length_check_still_holds(session_factory) -> None:
    from datetime import date

    with session_factory() as s:
        _remember_date(s, "clip", "20260108")
        s.commit()
        videos = [Video("clip", "Unrelated clip", "https://x/clip", None, duration=100)]
        _apply_cached_dates(s, videos)
    assert videos[0].upload_date == date(2026, 1, 8) and videos[0].duration == 100
    ep = Episode(1, 1, 1, "Some Episode", date(2026, 1, 8), runtime_minutes=30)
    r = match([ep], videos, [], MatchConfig(("date",), date_tolerance_days=0))
    assert r.matches == () and [h.video.id for h in r.held] == ["clip"], (
        "a 100 s clip against a 30 min episode is held whether its date came from the "
        "listing or from the cache"
    )


async def test_a_series_rename_does_not_hold_the_write_lock_across_the_listing(
    deps, session_factory
) -> None:
    from sqlalchemy import text

    sub_id, conn_id = make_sub(session_factory)
    client = fake_client(deps, conn_id)
    client.series_titles[5] = "Show (Renamed)"
    blocked: list[str] = []
    real = deps.source.list_recent

    def spy(url, limit):
        # an independent writer during the listing must not hit the write lock
        try:
            with session_factory() as s:
                s.execute(text("UPDATE setting SET value=value WHERE key='x'"))
                s.commit()
        except Exception as exc:  # OperationalError: database is locked
            blocked.append(repr(exc))
        return real(url, limit)

    deps.source.list_recent = spy
    report = await scan_subscription(deps, sub_id)
    assert report.error is None and blocked == [], blocked
    with session_factory() as s:
        assert s.get(Subscription, sub_id).title == "Show (Renamed)"


async def test_scheduler_stops_its_batch_when_the_source_rate_limits(deps, session_factory) -> None:
    ids = [make_sub(session_factory)[0]]
    with session_factory() as s:
        conn_id = s.get(Subscription, ids[0]).connection_id
        for series_id in (6, 7):
            sub = Subscription(
                connection_id=conn_id,
                series_id=series_id,
                tvdb_id=series_id,
                title=f"Show {series_id}",
                sources=[f"https://www.youtube.com/@show{series_id}"],
                strategies=["title"],
                auto_download="all",
            )
            s.add(sub)
            s.commit()
            ids.append(sub.id)
    fake_client(deps, conn_id)
    t = [0.0]
    deps.cooloff.clock = lambda: t[0]
    deps.source.recent_error = SourceError(
        "ERROR: [youtube] tab: This content isn't available, try again later. "
        "The current session has been rate-limited by YouTube for up to an hour."
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_scheduler(deps, stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(deps.source.listed) == 1, "one listing hit the wall; the rest waited"
    with session_factory() as s:
        stamped = [s.get(Subscription, i).last_scan_at is not None for i in ids]
    assert stamped.count(True) == 1, "only the subscription that hit the wall carries the error"
