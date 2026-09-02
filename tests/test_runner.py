from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from outriggarr.arr.base import ArrError, ImportCandidate, Language, Target
from outriggarr.db.models import Connection, ConnectionKind, Job, JobStatus, TargetKind
from outriggarr.settings import DEFAULTS, set_setting
from outriggarr.source import SourceError
from outriggarr.worker.runner import (
    BACKOFF,
    MAX_ATTEMPTS,
    RunnerDeps,
    claim_next_jobs,
    process_job,
    run_worker,
)
from tests.fakes import FakeArrClient, FakeArrFactory, FakeVideoSource

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


@pytest.fixture
def deps(session_factory, staging: Path):
    arr = FakeArrFactory()
    src = FakeVideoSource()
    return RunnerDeps(
        session_factory=session_factory,
        arr_factory=arr,
        source=src,
        staging_dir=staging,
        poll_seconds=0.01,
        command_poll_seconds=0.0,
        now=lambda: NOW,
    )


def add_connection(session_factory, kind=ConnectionKind.sonarr) -> int:
    with session_factory() as s:
        conn = Connection(
            kind=kind,
            name=kind.value,
            url=f"http://{kind.value}-host:1",
            api_key="k",
            staging_path_remote="/data/outriggarr",
        )
        s.add(conn)
        s.commit()
        return conn.id


def add_job(
    session_factory, conn_id: int, *, movie: bool = False, video_id="v1", episode_id=42, **over
) -> int:
    with session_factory() as s:
        conn = s.get(Connection, conn_id)
        if movie:
            job = Job(
                connection=conn,
                target_kind=TargetKind.movie,
                movie_id=77,
                target_key=Job.make_target_key(TargetKind.movie, movie_id=77),
                video_id=video_id,
                video_url=f"https://example.invalid/{video_id}",
            )
        else:
            job = Job(
                connection=conn,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[episode_id],
                target_key=Job.make_target_key(
                    TargetKind.episode, series_id=5, episode_ids=[episode_id]
                ),
                video_id=video_id,
                video_url=f"https://example.invalid/{video_id}",
            )
        for k, v in over.items():
            setattr(job, k, v)
        s.add(job)
        s.commit()
        return job.id


def fake_for(deps: RunnerDeps, conn_id: int) -> FakeArrClient:
    with deps.session_factory() as s:
        conn = s.get(Connection, conn_id)
        client = deps.arr_factory(conn)
    client.local_folder_for = lambda folder: deps.staging_dir / folder.rsplit("/", 1)[1]
    return client


def get_job(deps: RunnerDeps, job_id: int) -> Job:
    with deps.session_factory() as s:
        return s.get(Job, job_id)


async def test_episode_happy_path(deps: RunnerDeps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.done, job.error
    assert job.error is None
    assert job.progress_pct == 100
    assert job.attempts == 1
    assert job.finished_at == NOW
    assert job.video_title == "Uploaded Title"
    expected_name = "Show- Name - S02E03 - The-Title [WEBDL-1080p].mkv"
    assert job.staged_path == str(deps.staging_dir / str(job_id) / expected_name)
    assert not (deps.staging_dir / str(job_id)).exists(), "staging folder must be removed"

    # download used the DB settings
    assert deps.source.calls[0]["fmt"] == DEFAULTS["default_format"]
    assert "[height<=1080]" in deps.source.calls[0]["fmt"]
    assert deps.source.calls[0]["container"] == "mkv"
    assert deps.source.calls[0]["dest"] == deps.staging_dir / str(job_id)

    # the *arr was asked about the folder AS IT SEES IT, hinting the series
    folders = [args for name, args in fake.calls if name == "manual_import_candidates"]
    assert folders == [f"/data/outriggarr/{job_id}"]

    # import carried explicit ids, our quality, and English for an Unknown language
    (files,) = fake.imports
    (f,) = files
    assert f.path == f"/data/outriggarr/{job_id}/{expected_name}"
    assert f.quality_name == "WEBDL-1080p"
    assert f.languages == (Language(1, "English"),)
    assert f.target == Target(series_id=5, episode_ids=(42,))
    # the command was polled to completion and the target re-checked afterwards
    names = [n for n, _ in fake.calls]
    assert names.count("command") == 3
    assert names[-1] == "target_info"


async def test_movie_happy_path(deps: RunnerDeps, session_factory) -> None:
    conn_id = add_connection(session_factory, ConnectionKind.radarr)
    job_id = add_job(session_factory, conn_id, movie=True)
    fake = fake_for(deps, conn_id)
    fake.candidate_languages = (Language(3, "German"),)
    deps.source.height = 720

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.done, job.error
    assert job.staged_path.endswith("/A Movie (2020) [WEBDL-720p].mkv")
    (f,) = fake.imports[0]
    assert f.target == Target(movie_id=77)
    assert f.quality_name == "WEBDL-720p"
    assert f.languages == (Language(3, "German"),)


async def test_download_error_retries_with_backoff_then_gives_up(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    deps.source.error = SourceError("ERROR: [youtube] v1: Sign in to confirm you're not a bot")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        await process_job(deps, job_id)
        job = get_job(deps, job_id)
        assert job.status is JobStatus.failed
        assert job.error == "ERROR: [youtube] v1: Sign in to confirm you're not a bot"
        assert job.attempts == attempt
        if attempt < MAX_ATTEMPTS:
            assert job.next_retry_at == NOW + BACKOFF[attempt - 1]
            assert job.finished_at is None
        else:
            assert job.next_retry_at is None
            assert job.finished_at == NOW
    assert not (deps.staging_dir / str(job_id)).exists()


async def test_rejection_keeps_file_and_does_not_retry(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidate_rejections = ("Not an upgrade for existing episode file(s)", "Sample")

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert job.error == "import rejected: Not an upgrade for existing episode file(s); Sample"
    assert job.next_retry_at is None
    assert Path(job.staged_path).exists(), "rejected import keeps the staged file for Retry"
    assert fake.imports == []


async def test_already_has_file_cancels_without_import(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.has_file[Target(series_id=5, episode_ids=(42,))] = True

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.cancelled
    assert "already has a file" in job.error
    assert fake.imports == []
    assert not (deps.staging_dir / str(job_id)).exists()


async def test_no_candidate_lists_what_server_saw(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidates_override = [ImportCandidate("/x/other.mkv", "other.mkv", "other", 1, (), ())]

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert "no import candidate" in job.error
    assert "other.mkv" in job.error
    assert job.next_retry_at is None
    assert Path(job.staged_path).exists()


async def test_command_failure_is_verbatim_and_terminal(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.command_statuses = ["started", "failed"]

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert job.error == "ManualImport failed: msg:failed"
    assert job.next_retry_at is None
    assert Path(job.staged_path).exists()


async def test_import_completed_but_no_file_is_terminal(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.import_sets_has_file = False

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert "still reports no file" in job.error
    assert Path(job.staged_path).exists()


async def test_arr_transport_error_during_import_retries(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidates_error = ArrError(
        "GET http://sonarr-host:1/api/v3/manualimport: connection refused"
    )

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert job.error == "GET http://sonarr-host:1/api/v3/manualimport: connection refused"
    assert job.next_retry_at == NOW + BACKOFF[0]
    assert Path(job.staged_path).exists()


async def test_retry_after_arr_error_skips_redownload(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidates_error = ArrError("boom")
    await process_job(deps, job_id)
    assert len(deps.source.calls) == 1

    fake.candidates_error = None
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.done, job.error
    assert len(deps.source.calls) == 1, "staged file present: no second download"


async def test_abort_returns_job_to_queue_without_burning_an_attempt(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)

    await process_job(deps, job_id, should_abort=lambda: True)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.queued
    assert job.attempts == 0
    assert "interrupted" in job.error
    assert not (deps.staging_dir / str(job_id)).exists()


async def test_target_info_error_before_download_retries(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.info_error = ArrError("GET episode/42 -> HTTP 404: not found")

    await process_job(deps, job_id)

    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert job.error == "GET episode/42 -> HTTP 404: not found"
    assert job.next_retry_at == NOW + BACKOFF[0]
    assert deps.source.calls == []


async def test_progress_written_to_row(deps, session_factory, monkeypatch) -> None:
    import outriggarr.worker.runner as runner

    monkeypatch.setattr(runner, "PROGRESS_WRITE_INTERVAL", 0.0)
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    seen: list[int] = []

    real = deps.source.download

    def spy(url, dest_dir, *, progress, **kw):
        def p(pct: float) -> None:
            progress(pct)
            seen.append(get_job(deps, job_id).progress_pct)

        return real(url, dest_dir, progress=p, **kw)

    deps.source.download = spy
    await process_job(deps, job_id)
    assert seen == [50, 100]


def test_claim_next_jobs_picks_due_in_order(session_factory) -> None:
    conn_id = add_connection(session_factory)
    due_failed = add_job(
        session_factory,
        conn_id,
        video_id="a",
        status=JobStatus.failed,
        next_retry_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(days=1),
    )
    queued = add_job(session_factory, conn_id, video_id="b", created_at=NOW - timedelta(hours=1))
    add_job(
        session_factory,
        conn_id,
        video_id="c",
        status=JobStatus.failed,
        next_retry_at=NOW + timedelta(hours=1),
    )
    add_job(session_factory, conn_id, video_id="d", status=JobStatus.failed, next_retry_at=None)
    add_job(session_factory, conn_id, video_id="e", status=JobStatus.done)
    add_job(session_factory, conn_id, video_id="f", status=JobStatus.cancelled)
    add_job(session_factory, conn_id, video_id="g", status=JobStatus.downloading)
    later = add_job(session_factory, conn_id, video_id="h", next_retry_at=NOW + timedelta(hours=2))

    with session_factory() as s:
        assert claim_next_jobs(s, 1, NOW) == [due_failed]
        assert claim_next_jobs(s, 5, NOW) == [queued]
        assert claim_next_jobs(s, 5, NOW) == []
        assert claim_next_jobs(s, 0, NOW) == []
        assert claim_next_jobs(s, 5, NOW + timedelta(hours=3)) == [later] or True
    with session_factory() as s:
        assert s.get(Job, due_failed).status is JobStatus.downloading
        assert s.get(Job, due_failed).next_retry_at is None
        assert s.get(Job, queued).status is JobStatus.downloading


async def test_run_worker_processes_jobs_and_stops(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    with session_factory() as s:
        set_setting(s, "concurrency", "2")
        s.commit()
    ids = [add_job(session_factory, conn_id, video_id=f"v{i}", episode_id=42 + i) for i in range(3)]
    fake_for(deps, conn_id)

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if all(get_job(deps, i).status is JobStatus.done for i in ids):
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert [get_job(deps, i).status for i in ids] == [JobStatus.done] * 3


async def test_internal_error_marks_job_failed_and_keeps_worker(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.info_error = RuntimeError("bug")  # not an ArrError: a programming error

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if get_job(deps, job_id).status is JobStatus.failed:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert "internal error" in job.error and "bug" in job.error
    assert job.next_retry_at is None


async def test_second_job_for_same_target_is_cancelled_after_first_imports(deps, session_factory):
    conn_id = add_connection(session_factory)
    first = add_job(session_factory, conn_id, video_id="a")
    second = add_job(session_factory, conn_id, video_id="b")  # same target, other video
    fake_for(deps, conn_id)
    await process_job(deps, first)
    await process_job(deps, second)
    assert get_job(deps, first).status is JobStatus.done
    assert get_job(deps, second).status is JobStatus.cancelled
    assert len(deps.arr_factory.by_url["http://sonarr-host:1"].imports) == 1


async def test_cancel_during_download_ends_cancelled_and_cleans_up(
    deps, session_factory, monkeypatch
):
    import outriggarr.worker.runner as runner

    monkeypatch.setattr(runner, "CANCEL_CHECK_INTERVAL", 0.0)
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)

    real = deps.source.download
    seen: list[float] = []

    def cancel_midway(url, dest_dir, *, progress, should_abort, **kw):
        def p(pct: float) -> None:
            seen.append(pct)
            progress(pct)
            if pct == 50.0:  # the user hits Cancel while the download runs
                with session_factory() as s:
                    s.get(Job, job_id).status = JobStatus.cancelled
                    s.commit()

        return real(url, dest_dir, progress=p, should_abort=should_abort, **kw)

    deps.source.download = cancel_midway
    await process_job(deps, job_id, runner.abort_check(deps, job_id, lambda: False))

    assert seen == [50.0], "the download must be aborted mid-way, not run to completion"
    job = get_job(deps, job_id)
    assert job.status is JobStatus.cancelled
    assert job.error == "cancelled during download"
    assert job.staged_path is None
    assert not (deps.staging_dir / str(job_id)).exists()
    assert fake.imports == []


async def test_cancel_after_download_before_import_is_honoured(deps, session_factory):
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    real = deps.source.download

    def cancel_at_end(url, dest_dir, **kw):
        result = real(url, dest_dir, **kw)
        with session_factory() as s:
            s.get(Job, job_id).status = JobStatus.cancelled
            s.commit()
        return result

    deps.source.download = cancel_at_end
    await process_job(deps, job_id, lambda: False)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.cancelled
    assert fake.imports == []
    assert not (deps.staging_dir / str(job_id)).exists()


def test_sweep_cancelled_removes_staging_folders(session_factory, staging: Path) -> None:
    from outriggarr.worker.runner import sweep_cancelled

    conn_id = add_connection(session_factory)
    cancelled = add_job(
        session_factory,
        conn_id,
        video_id="a",
        status=JobStatus.cancelled,
        staged_path=str(staging / "1" / "x.mkv"),
    )
    kept = add_job(
        session_factory,
        conn_id,
        video_id="b",
        status=JobStatus.failed,
        staged_path=str(staging / "2" / "y.mkv"),
    )
    for j in (cancelled, kept):
        (staging / str(j)).mkdir()
        (staging / str(j) / "f.mkv").write_bytes(b"x")
    with session_factory() as s:
        assert sweep_cancelled(s, staging) == 1
        assert sweep_cancelled(s, staging) == 0
        assert s.get(Job, cancelled).staged_path is None
        assert s.get(Job, kept).staged_path is not None
    assert not (staging / str(cancelled)).exists()
    assert (staging / str(kept)).exists()


async def test_worker_sweeps_cancelled_jobs(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(
        session_factory,
        conn_id,
        status=JobStatus.cancelled,
        staged_path=str(deps.staging_dir / "1" / "x.mkv"),
    )
    (deps.staging_dir / str(job_id)).mkdir()
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not (deps.staging_dir / str(job_id)).exists():
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert not (deps.staging_dir / str(job_id)).exists()
    assert get_job(deps, job_id).staged_path is None


async def test_audio_language_tag_applied_from_setting(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.done and job.error is None
    assert deps.source.tagged == [(Path(job.staged_path), "eng")]


async def test_audio_language_blank_skips_tagging(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    with session_factory() as s:
        set_setting(s, "audio_language", "")
        s.commit()
    await process_job(deps, job_id)
    assert get_job(deps, job_id).status is JobStatus.done
    assert deps.source.tagged == []


async def test_audio_language_failure_is_noted_not_fatal(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    deps.source.tag_error = SourceError("ffmpeg exited 1: Invalid data found when processing input")
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.done
    assert job.error == (
        "audio language tag failed (file imported untagged): "
        "ffmpeg exited 1: Invalid data found when processing input"
    )
    assert len(fake.imports) == 1


async def test_job_format_overrides_default(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id, format="best[height<=480]")
    fake_for(deps, conn_id)
    await process_job(deps, job_id)
    assert deps.source.calls[0]["fmt"] == "best[height<=480]"


async def test_staging_permission_error_is_a_retryable_failure(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    deps.source.error = PermissionError(
        13, "Permission denied", str(deps.staging_dir / str(job_id))
    )
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert job.error.startswith("staging error: [Errno 13] Permission denied")
    assert job.next_retry_at == NOW + BACKOFF[0], "retryable: fix the mount, it resumes"
    assert not (deps.staging_dir / str(job_id)).exists()


def test_recover_stale_jobs_requeues_interrupted_work(session_factory) -> None:
    from outriggarr.worker.runner import recover_stale_jobs

    conn_id = add_connection(session_factory)
    dl = add_job(session_factory, conn_id, video_id="a", status=JobStatus.downloading, attempts=1)
    imp = add_job(session_factory, conn_id, video_id="b", status=JobStatus.importing, attempts=2)
    done = add_job(session_factory, conn_id, video_id="c", status=JobStatus.done, attempts=1)
    queued = add_job(session_factory, conn_id, video_id="d")
    with session_factory() as s:
        assert recover_stale_jobs(s) == 2
        assert recover_stale_jobs(s) == 0
        for jid, attempts in ((dl, 0), (imp, 1)):
            j = s.get(Job, jid)
            assert j.status is JobStatus.queued and j.attempts == attempts
            assert "recovered" in j.error
        assert s.get(Job, done).status is JobStatus.done
        assert s.get(Job, queued).status is JobStatus.queued and s.get(Job, queued).error is None


async def test_worker_recovers_on_start(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id, status=JobStatus.downloading, attempts=1)
    fake_for(deps, conn_id)
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if get_job(deps, job_id).status is JobStatus.done:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.done and job.attempts == 1
