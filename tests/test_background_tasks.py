"""The background fetches (rechecks, date fetches) are owned tasks: a consumer failure
cancels what is still running, and shutdown keeps what was fetched."""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from outriggarr.db.models import Connection, ConnectionKind, Job, Subscription, TargetKind
from outriggarr.source import VideoRef


def _seed(client: TestClient, n: int) -> list[int]:
    with client.app.state.session_factory() as s:
        conn = Connection(
            kind=ConnectionKind.sonarr,
            name="s",
            url="http://sonarr-host:1234",
            api_key="k",
            staging_path_remote="/data/outriggarr",
        )
        sub = Subscription(
            connection=conn,
            series_id=5,
            title="Show",
            sources=["https://www.youtube.com/@x"],
            strategies=["title"],
        )
        s.add(sub)
        s.flush()
        ids = []
        for i in range(n):
            job = Job(
                connection=conn,
                subscription_id=sub.id,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[10 + i],
                target_key=f"episode:5:{10 + i}",
                video_id=f"m{i}",
                video_url=f"https://y/m{i}",
                video_title=f"Match {i}",
                target_label=f"Show S31E{i:02d}",
                matched_by="contains",
                target_runtime=25,
            )
            s.add(job)
            s.flush()
            ids.append(job.id)
        s.commit()
    return ids


def _wait(client: TestClient, path: str) -> dict:
    for _ in range(200):
        st = client.get(path).json()
        if not st["running"]:
            return st
        time.sleep(0.02)
    raise AssertionError("still running")


def test_a_consumer_failure_cancels_the_fetches_still_running(
    client: TestClient, monkeypatch
) -> None:
    from outriggarr.api import matches

    monkeypatch.setattr(matches, "RECHECK_PARALLEL", 1)
    ids = _seed(client, 6)
    source = client.app.state.source
    real = source.fetch_info

    def slow(url):  # a real fetch takes time; an instant fake would finish before the failure
        time.sleep(0.05)
        return real(url)

    source.fetch_info = slow
    # the first answer (the newest job, m5, is fetched first) breaks the consumer with a
    # duration that is not a number; the rest must be cancelled, not left running unwatched
    source.infos = {
        f"https://y/m{i}": VideoRef(
            f"m{i}", "t", f"https://y/m{i}", "oops" if i == 5 else 1500, 1, None
        )
        for i in range(6)
    }
    client.post("/api/matches/recheck")
    st = _wait(client, "/api/matches/recheck")
    assert st["failure"] and "ValueError" in st["failure"]
    time.sleep(0.2)
    assert len(source.fetched) <= 2, f"the remaining fetches were cancelled: {source.fetched}"
    with client.app.state.session_factory() as s:
        assert s.get(Job, ids[5]).video_duration is None


def test_track_task_keeps_a_reference_until_done() -> None:
    from types import SimpleNamespace

    from outriggarr.api.deps import track_task

    app = SimpleNamespace(state=SimpleNamespace())

    async def run() -> None:
        async def nap() -> None:
            await asyncio.sleep(0)

        task = asyncio.create_task(nap())
        track_task(app, task)
        assert task in app.state.tasks
        await task
        await asyncio.sleep(0)  # the done callback runs on the next tick
        assert task not in app.state.tasks

    asyncio.run(run())


def test_shutdown_awaits_a_running_recheck_and_keeps_what_it_fetched(
    settings, arr, source, notifier, monkeypatch
) -> None:
    from outriggarr.api import matches
    from outriggarr.main import create_app

    monkeypatch.setattr(matches, "RECHECK_PARALLEL", 1)
    monkeypatch.setattr(matches, "COMMIT_EVERY", 100)  # nothing committed mid-run by cadence
    real = source.fetch_info

    def slow(url):
        time.sleep(0.25)
        return real(url)

    source.fetch_info = slow
    app = create_app(
        settings, start_worker=False, arr_factory=arr, source=source, notifier=notifier
    )
    with TestClient(app) as client:
        ids = _seed(client, 3)
        source.infos = {
            f"https://y/m{i}": VideoRef(f"m{i}", "t", f"https://y/m{i}", 1500, 1, None)
            for i in range(3)
        }
        client.post("/api/matches/recheck")
        time.sleep(0.4)  # one answer in, the second in flight
        factory = client.app.state.session_factory
        tasks = list(client.app.state.tasks)
        assert tasks and not tasks[0].done(), "a recheck is running when the app stops"
    assert all(t.done() for t in tasks), "shutdown awaited it"
    with factory() as s:
        durations = [s.get(Job, i).video_duration for i in ids]
    assert 1500 in durations, f"what was fetched before the stop is kept: {durations}"
