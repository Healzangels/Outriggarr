from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from outriggarr.arr.base import ArrError, EpisodeRef
from outriggarr.db.models import Connection, ConnectionKind, Job, JobStatus, Override, Subscription
from outriggarr.settings import set_setting
from outriggarr.source import SourceError, VideoRef
from outriggarr.worker.runner import RunnerDeps
from outriggarr.worker.scheduler import (
    SubscriptionNotFound,
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
            source_url="https://www.youtube.com/@show",
            strategies=["title"],
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


async def test_scan_skips_episodes_that_already_have_jobs(deps, session_factory) -> None:
    sub_id, conn_id = make_sub(session_factory)
    fake_client(deps, conn_id)
    await scan_subscription(deps, sub_id)
    with session_factory() as s:
        done = s.query(Job).filter(Job.video_id == "v6").one()
        done.status = JobStatus.done
        failed_terminal = s.query(Job).filter(Job.video_id == "v7").one()
        failed_terminal.status = JobStatus.failed
        failed_terminal.next_retry_at = None
        s.commit()

    report = await scan_subscription(deps, sub_id)
    assert [s_["code"] for s_ in report.skipped_existing] == ["S30E06"]
    # the terminally failed one is re-matched; same video → duplicate → reported, not created
    (m,) = [m for m in report.matches if m["code"] == "S30E07"]
    assert m["job_id"] is None and "already exists" in m["skipped"]
    assert report.created_job_ids == []
    with session_factory() as s:
        assert s.query(Job).count() == 2


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
            source_url="https://x",
            strategies=[],
            last_scan_at=NOW - timedelta(minutes=5),
        )
        stale = Subscription(
            connection=conn,
            series_id=7,
            title="S",
            source_url="https://x",
            strategies=[],
            last_scan_at=NOW - timedelta(minutes=45),
        )
        off = Subscription(
            connection=conn,
            series_id=8,
            title="O",
            source_url="https://x",
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
