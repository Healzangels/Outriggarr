from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from outriggarr.arr.base import ArrError, ImportCandidate, Language, Target
from outriggarr.db.models import Connection, ConnectionKind, Job, JobStatus, TargetKind
from outriggarr.settings import DEFAULTS, set_setting
from outriggarr.source import DownloadAborted, SourceError
from outriggarr.worker.runner import (
    BACKOFF,
    MAX_ATTEMPTS,
    RunnerDeps,
    claim_next_jobs,
    process_job,
    run_worker,
)
from tests.fakes import FakeArrClient, FakeArrFactory, FakeNotifier, FakeVideoSource

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
        notifier=FakeNotifier(),
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
    assert job.status is JobStatus.done, "satisfied target = finished, not a user cancel"
    assert "already had a file" in job.error
    assert fake.imports == []
    assert deps.source.calls == [], "a satisfied target is detected BEFORE the download"
    assert not (deps.staging_dir / str(job_id)).exists()


async def test_no_candidate_lists_what_server_saw(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidates_override = [ImportCandidate(9, "/x/other.mkv", "other.mkv", "other", 1, (), ())]

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
        # at NOW+3h both the once-failed "c" (retry at +1h) and "later" (+2h) are due
        assert claim_next_jobs(s, 5, NOW + timedelta(hours=3)) == [3, later]
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
    assert get_job(deps, second).status is JobStatus.done  # nothing to import: finished
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


async def test_subtitle_sidecars_are_staged_with_the_video_stem(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    deps.source.subtitle_langs_available = ("en", "es")
    with session_factory() as s:
        set_setting(s, "subtitles_langs", "en,es,fr")
        s.commit()
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.done, job.error
    assert deps.source.calls[0]["subtitle_langs"] == ("en", "es", "fr")
    assert deps.source.calls[0]["auto_subtitles"] is False
    # the sidecars were renamed to the staged stem before import (then swept with the folder)
    (imp,) = fake.imports
    stem = Path(imp[0].path).stem
    # the fake never removes files, so inspect what the runner renamed: it logs and the
    # folder was rmtree'd on done — assert via the candidates listing captured earlier
    listing = [c for name, c in fake.calls if name == "manual_import_candidates"]
    assert listing, "import happened"
    assert not (deps.staging_dir / str(job_id)).exists()
    assert stem.endswith("[WEBDL-1080p]")


async def test_subtitle_sidecar_names_before_import(deps, session_factory, monkeypatch) -> None:
    """Freeze the staging folder right before import to see the sidecar names."""
    import outriggarr.worker.runner as runner

    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    with session_factory() as s:
        set_setting(s, "subtitles_langs", "en")
        set_setting(s, "subtitles_auto", "1")
        s.commit()
    seen: list[str] = []
    real = runner._import_stage

    async def spy(deps_, session, job, client, target, remote_folder, staged, *rest):
        seen.extend(sorted(p.name for p in staged.parent.iterdir()))
        return await real(deps_, session, job, client, target, remote_folder, staged, *rest)

    monkeypatch.setattr(runner, "_import_stage", spy)
    await process_job(deps, job_id)
    assert deps.source.calls[0]["auto_subtitles"] is True
    assert seen == [
        "Show- Name - S02E03 - The-Title [WEBDL-1080p].en.srt",
        "Show- Name - S02E03 - The-Title [WEBDL-1080p].mkv",
    ]


async def test_no_subtitles_when_setting_blank(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    with session_factory() as s:
        set_setting(s, "subtitles_langs", "")
        s.commit()
    await process_job(deps, job_id)
    assert deps.source.calls[0]["subtitle_langs"] == ()


async def test_notify_on_terminal_failure_only(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    deps.source.error = SourceError(
        "ERROR: [youtube] v1: Unable to download webpage: <urlopen error timed out>"
    )
    for _ in range(MAX_ATTEMPTS - 1):
        await process_job(deps, job_id)
        assert deps.notifier.sent == [], "retryable failures stay quiet"
    await process_job(deps, job_id)  # attempts exhausted
    (title, body) = deps.notifier.sent[0]
    assert title == "Outriggarr: job failed"
    assert f"#{job_id}" in body and "Unable to download webpage: <urlopen error timed out>" in body


async def test_notify_on_rejection_and_not_on_done_by_default(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidate_rejections = ("Not an upgrade",)
    await process_job(deps, job_id)
    assert [t for t, _ in deps.notifier.sent] == ["Outriggarr: job failed"]
    assert "Not an upgrade" in deps.notifier.sent[0][1]

    deps.notifier.sent.clear()
    fake.candidate_rejections = ()
    with session_factory() as s:
        set_setting(s, "notify_on_failed", "0")
        s.commit()
    job2 = add_job(session_factory, conn_id, video_id="v2", episode_id=43)
    await process_job(deps, job2)
    assert get_job(deps, job2).status is JobStatus.done
    assert deps.notifier.sent == [], "done is quiet unless notify_on_done"
    with session_factory() as s:
        set_setting(s, "notify_on_done", "1")
        s.commit()
    job3 = add_job(session_factory, conn_id, video_id="v3", episode_id=44)
    await process_job(deps, job3)
    assert [t for t, _ in deps.notifier.sent] == ["Outriggarr: imported"]


async def test_notifier_crash_never_touches_the_job(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidate_rejections = ("Sample",)
    deps.notifier.error = RuntimeError("discord is down")
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.error == "import rejected: Sample"


async def test_failed_notification_can_be_switched_off(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidate_rejections = ("Sample",)
    with session_factory() as s:
        set_setting(s, "notify_on_failed", "0")
        s.commit()
    await process_job(deps, job_id)
    assert get_job(deps, job_id).status is JobStatus.failed
    assert deps.notifier.sent == []


# ---- audit fixes ----------------------------------------------------------------


def test_claim_excludes_jobs_a_running_task_still_owns(session_factory) -> None:
    conn_id = add_connection(session_factory)
    a = add_job(session_factory, conn_id, video_id="a")
    b = add_job(session_factory, conn_id, video_id="b", episode_id=43)
    with session_factory() as s:
        assert claim_next_jobs(s, 5, NOW, exclude={a}) == [b]
        assert claim_next_jobs(s, 5, NOW, exclude={a}) == []
        assert claim_next_jobs(s, 5, NOW) == [a]


async def test_cancel_outranks_a_failure_that_lands_later(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)

    def cancel_then_fail(url, dest_dir, **kw):
        with session_factory() as s:  # the user cancels during extraction…
            j = s.get(Job, job_id)
            j.status = JobStatus.cancelled
            j.error = "cancelled"
            j.finished_at = NOW
            s.commit()
        raise SourceError("ERROR: HTTP Error 403")  # …then yt-dlp fails

    deps.source.download = cancel_then_fail
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.cancelled and job.next_retry_at is None
    assert job.error == "cancelled", "the API's row wins; no retry scheduled"
    assert deps.notifier.sent == []


async def test_cancel_before_start_leaves_the_row_alone(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(
        session_factory, conn_id, status=JobStatus.cancelled, error="cancelled", attempts=0
    )
    fake_for(deps, conn_id)
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.cancelled and job.error == "cancelled" and job.attempts == 0
    assert deps.source.calls == []


async def test_cancel_racing_the_importing_transition_wins(deps, session_factory) -> None:
    import outriggarr.worker.runner as runner

    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    real = runner._enter_importing

    def cancel_just_before(session, job):
        with session_factory() as s:
            s.get(Job, job_id).status = JobStatus.cancelled
            s.commit()
        return real(session, job)

    deps.source.download  # noqa: B018 (keep the fake)
    import unittest.mock as um

    with um.patch.object(runner, "_enter_importing", cancel_just_before):
        await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.cancelled and fake.imports == []
    assert not (deps.staging_dir / str(job_id)).exists()


async def test_satisfied_target_is_detected_before_download(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.has_file[Target(series_id=5, episode_ids=(42,))] = True
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.done and "already had a file" in job.error
    assert deps.source.calls == [] and fake.imports == []


async def test_parse_only_rejection_is_reprocessed_with_ids(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidate_rejections = ("Unknown Series",)
    fake.reprocessed_rejections = ()  # with explicit ids Sonarr is happy
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.done, job.error
    (rp,) = [c for n, c in fake.calls if n == "reprocess"]
    assert rp[1] == Target(series_id=5, episode_ids=(42,)) and rp[2] == "WEBDL-1080p" and rp[3] == 2


async def test_real_rejection_survives_reprocess_and_is_terminal(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidate_rejections = ("Unknown Series", "Not an upgrade for existing episode file(s)")
    fake.reprocessed_rejections = ("Not an upgrade for existing episode file(s)",)
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.next_retry_at is None
    assert job.error == "import rejected: Not an upgrade for existing episode file(s)"


async def test_reprocess_unavailable_falls_back_to_dropping_parse_only_rejections(
    deps, session_factory
) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.candidate_rejections = ("Unknown Series",)
    fake.reprocess_error = ArrError("POST manualimport -> HTTP 404: no", retryable=False)
    await process_job(deps, job_id)
    assert get_job(deps, job_id).status is JobStatus.done


async def test_deterministic_arr_error_is_terminal_transport_error_retries(
    deps, session_factory
) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.info_error = ArrError("GET episode/42 -> HTTP 404: not found", retryable=False)
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.next_retry_at is None
    assert deps.notifier.sent and "job failed" in deps.notifier.sent[-1][0]
    job2 = add_job(session_factory, conn_id, video_id="v2", episode_id=43)
    fake.info_error = ArrError("GET episode/43: connection refused", retryable=True)
    await process_job(deps, job2)
    assert get_job(deps, job2).next_retry_at == NOW + BACKOFF[0]


def test_sweep_removes_orphan_folder_without_staged_path(session_factory, staging: Path) -> None:
    from outriggarr.worker.runner import sweep_cancelled

    conn_id = add_connection(session_factory)
    orphan = add_job(
        session_factory, conn_id, video_id="a", status=JobStatus.cancelled, staged_path=None
    )
    (staging / str(orphan)).mkdir()
    (staging / str(orphan) / "big.mkv").write_bytes(b"x" * 10)
    with session_factory() as s:
        assert sweep_cancelled(s, staging) == 0, "ticks only look at rows with a staged_path"
        assert sweep_cancelled(s, staging, full=True) == 1
    assert not (staging / str(orphan)).exists()


async def test_internal_error_after_download_does_not_leave_the_folder(
    deps, session_factory
) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.info_error = None
    real = deps.source.download

    def then_crash(url, dest_dir, **kw):
        real(url, dest_dir, **kw)
        raise RuntimeError("boom after download")  # not a SourceError: a bug

    deps.source.download = then_crash
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if get_job(deps, job_id).status is JobStatus.failed:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert "internal error" in get_job(deps, job_id).error
    assert not (deps.staging_dir / str(job_id)).exists()


async def test_rename_error_is_a_retryable_staging_error(
    deps, session_factory, monkeypatch
) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    real_rename = Path.rename

    def enametoolong(self, target):
        raise OSError(63, "File name too long", str(target))

    monkeypatch.setattr(Path, "rename", enametoolong)
    await process_job(deps, job_id)
    monkeypatch.setattr(Path, "rename", real_rename)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.error.startswith("staging error: [Errno 63]")
    assert job.next_retry_at == NOW + BACKOFF[0]
    assert not (deps.staging_dir / str(job_id)).exists()


async def test_concurrency_limit_is_enforced(deps, session_factory) -> None:
    import threading

    conn_id = add_connection(session_factory)
    ids = [add_job(session_factory, conn_id, video_id=f"v{i}", episode_id=42 + i) for i in range(4)]
    fake_for(deps, conn_id)
    with session_factory() as s:
        set_setting(s, "concurrency", "2")
        s.commit()
    gate = threading.Event()
    in_flight, peak = [0], [0]
    lock = threading.Lock()
    real = deps.source.download

    def slow(url, dest_dir, **kw):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        gate.wait(2)
        try:
            return real(url, dest_dir, **kw)
        finally:
            with lock:
                in_flight[0] -= 1

    deps.source.download = slow
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    await asyncio.sleep(0.3)
    assert peak[0] == 2 and in_flight[0] == 2
    gate.set()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if all(get_job(deps, i).status is JobStatus.done for i in ids):
            break
    stop.set()
    await asyncio.wait_for(task, 3)
    assert peak[0] == 2


async def test_shutdown_aborts_a_running_download_and_requeues(deps, session_factory) -> None:
    import threading

    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    started = threading.Event()
    real = deps.source.download

    def slow(url, dest_dir, *, progress, should_abort, **kw):
        started.set()
        for _ in range(200):
            if should_abort():
                raise DownloadAborted("stop")
            time.sleep(0.01)
        return real(url, dest_dir, progress=progress, should_abort=should_abort, **kw)

    deps.source.download = slow
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    await asyncio.to_thread(started.wait, 2)
    stop.set()
    await asyncio.wait_for(task, 3)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.queued and job.attempts == 0
    assert "interrupted" in job.error
    assert not (deps.staging_dir / str(job_id)).exists()


# ---- discovery fixes -------------------------------------------------------------


async def test_our_own_earlier_import_is_recorded_as_done(deps, session_factory) -> None:
    """A blip on the command poll left the job failed with a staged file; the *arr then
    moved that file. The retry must end `done`, never `cancelled`."""
    conn_id = add_connection(session_factory)
    job_id = add_job(
        session_factory,
        conn_id,
        status=JobStatus.failed,
        staged_path=str(deps.staging_dir / "1" / "gone.mkv"),
        attempts=1,
    )
    fake = fake_for(deps, conn_id)
    fake.has_file[Target(series_id=5, episode_ids=(42,))] = True
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.done and job.staged_path is None
    assert deps.source.calls == [] and fake.imports == []


async def test_command_poll_timeout_is_retryable(deps, session_factory, monkeypatch) -> None:
    import outriggarr.worker.runner as runner

    monkeypatch.setattr(runner, "COMMAND_TIMEOUT_SECONDS", 0.0)
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.command_statuses = ["started"]  # never finishes
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.next_retry_at == NOW + BACKOFF[0]
    assert Path(job.staged_path).exists(), "the *arr may still be moving it: keep the file"


async def test_shutdown_during_import_poll_keeps_the_file_and_requeues(
    deps, session_factory
) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    # finite: a runner that ignored the abort would finish the import and fail the
    # assertions below, instead of polling for the whole command timeout
    fake.command_statuses = ["started", "started", "started", "completed"]
    calls = {"n": 0}

    def stop_after_first_poll() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # the download hook calls first; abort once we are polling

    await process_job(deps, job_id, stop_after_first_poll)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.queued and job.attempts == 0
    assert "interrupted during import" in job.error
    assert Path(job.staged_path).exists(), "no re-download needed after the restart"


async def test_partially_satisfied_multi_episode_target_is_refused(deps, session_factory) -> None:
    from outriggarr.arr.base import TargetInfo
    from tests.fakes import EPISODE_INFO

    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake = fake_for(deps, conn_id)
    fake.info = TargetInfo(**{**EPISODE_INFO.__dict__, "partially_satisfied": True})
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.next_retry_at is None
    assert "already have a file" in job.error and deps.source.calls == []


def test_disabled_connection_jobs_are_not_claimed(session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    with session_factory() as s:
        s.get(Connection, conn_id).enabled = False
        s.commit()
        assert claim_next_jobs(s, 5, NOW) == []
        s.get(Connection, conn_id).enabled = True
        s.commit()
        assert claim_next_jobs(s, 5, NOW) == [job_id]


def test_instance_lock_is_exclusive(tmp_path: Path) -> None:
    from outriggarr.worker.runner import acquire_instance_lock

    first = acquire_instance_lock(tmp_path)
    assert first is not None and (tmp_path / ".outriggarr.lock").exists()
    assert acquire_instance_lock(tmp_path) is None, "a second instance must not run the worker"
    first.close()
    second = acquire_instance_lock(tmp_path)
    assert second is not None
    second.close()


async def test_second_worker_stays_idle_when_locked(deps, session_factory, tmp_path: Path) -> None:
    from outriggarr.worker.runner import acquire_instance_lock

    deps.lock_dir = tmp_path
    holder = acquire_instance_lock(tmp_path)
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    await asyncio.sleep(0.1)
    assert get_job(deps, job_id).status is JobStatus.queued, (
        "nothing claimed while another instance holds the lock"
    )
    stop.set()
    await asyncio.wait_for(task, 2)
    holder.close()


async def test_audio_language_precedence_subscription_then_source_then_default(
    deps, session_factory
) -> None:
    from outriggarr.db.models import Subscription

    conn_id = add_connection(session_factory)
    # the source declares Japanese: tagged jpn, not the global default
    deps.source.audio_language = "jpn"
    job1 = add_job(session_factory, conn_id, video_id="v1", episode_id=101)
    await process_job(deps, job1)
    assert deps.source.tagged[-1][1] == "jpn"
    # a subscription's own setting is the operator's word and beats what the source says
    job2 = add_job(session_factory, conn_id, video_id="v2", episode_id=102)
    with session_factory() as s:
        sub = Subscription(
            connection_id=conn_id,
            series_id=5,
            title="Anime",
            sources=["https://x"],
            strategies=["title"],
            audio_language="kor",
        )
        s.add(sub)
        s.flush()
        s.get(Job, job2).subscription_id = sub.id
        s.commit()
    await process_job(deps, job2)
    assert deps.source.tagged[-1][1] == "kor"
    # nothing declared and no override: the global default, as before
    deps.source.audio_language = None
    job3 = add_job(session_factory, conn_id, video_id="v3", episode_id=103)
    await process_job(deps, job3)
    assert deps.source.tagged[-1][1] == "eng"
    assert [t[1] for t in deps.source.tagged] == ["jpn", "kor", "eng"]


def scripted_clock(*values: float):
    """A monotonic clock that answers the given values in turn, then repeats the last."""
    it = iter(values)
    last = [values[-1]]

    def clock() -> float:
        for v in it:
            last[0] = v
            return v
        return last[0]

    return clock


async def test_stalled_download_is_a_retryable_failure(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    # guard created at 0; first abort check at 10 (fine); progress 50% at 20; the next
    # check finds nothing has advanced for 380 s, past the 300 s idle limit
    deps.clock = scripted_clock(0, 10, 20, 400)
    deps.stall_idle_seconds = 300.0
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.attempts == 1, "a stall spends the attempt"
    assert job.error == "no download progress for 5 min (stuck at 50%); abandoned, will retry"
    assert job.next_retry_at == NOW + BACKOFF[0]
    assert not (deps.staging_dir / str(job_id)).exists(), "the partial download is gone"


async def test_download_running_past_the_cap_is_abandoned(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    deps.clock = scripted_clock(0, 10, 20, 7300)
    deps.stall_idle_seconds = 100_000.0  # progress keeps coming, it just never ends
    deps.stall_cap_seconds = 7200.0
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed
    assert job.error == "download still running after 2 h; abandoned, will retry"
    assert job.next_retry_at == NOW + BACKOFF[0]


async def test_progress_resets_the_stall_clock(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    # checks at 100 and 300 with the 50% progress at 200 and a 150 s idle limit: each
    # check sees a 100 s gap only because progress resets the clock; a guard that ignored
    # progress would see 200 s at its second look and trip
    deps.clock = scripted_clock(0, 100, 200, 300, 400)
    deps.stall_idle_seconds = 150.0
    await process_job(deps, job_id)
    assert get_job(deps, job_id).status is JobStatus.done


async def test_rate_limited_download_pauses_the_queue_without_spending_an_attempt(
    deps, session_factory
) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    other = add_job(session_factory, conn_id, video_id="v2", episode_id=43)
    fake_for(deps, conn_id)
    t = [0.0]
    deps.cooloff.clock = lambda: t[0]
    deps.source.error = SourceError(
        "ERROR: [youtube] v1: This content isn't available, try again later. "
        "The current session has been rate-limited by YouTube for up to an hour."
    )
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.queued and job.attempts == 0, "the wall was the source's"
    assert job.next_retry_at == NOW + timedelta(minutes=15)
    assert job.error.startswith(
        "rate-limited by the source; all downloads paused for 15 min: ERROR: [youtube] v1"
    )
    assert deps.cooloff.active() and deps.cooloff.remaining() == 900
    assert not (deps.staging_dir / str(job_id)).exists()

    # the worker starts nothing while the pause holds, then takes the next job up
    deps.source.error = None
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(deps, stop))
    await asyncio.sleep(0.1)
    assert get_job(deps, other).status is JobStatus.queued and len(deps.source.calls) == 1
    t[0] += 900
    for _ in range(200):
        await asyncio.sleep(0.01)
        if get_job(deps, other).status is JobStatus.done:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert get_job(deps, other).status is JobStatus.done
    assert not deps.cooloff.active() and deps.cooloff.strikes == 0, "a success clears the ladder"
    assert get_job(deps, job_id).status is JobStatus.queued, "still waiting for its own retry time"


async def test_permanent_download_failure_is_not_retried(deps, session_factory) -> None:
    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    with session_factory() as s:
        set_setting(s, "notify_on_failed", "1")
        s.commit()
    deps.source.error = SourceError("ERROR: [youtube] v1: Video unavailable")
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.error == "ERROR: [youtube] v1: Video unavailable"
    assert job.next_retry_at is None and job.finished_at == NOW, "gone is gone: no retry ladder"
    assert not (deps.staging_dir / str(job_id)).exists()
    assert len(deps.notifier.sent) == 1 and "Video unavailable" in deps.notifier.sent[0][1], (
        "a terminal failure is announced at once"
    )
    # a bot check is the address being busy, not the video: still on the ladder
    other = add_job(session_factory, conn_id, video_id="v2", episode_id=43)
    deps.source.error = SourceError("ERROR: [youtube] v2: Sign in to confirm you're not a bot")
    await process_job(deps, other)
    assert get_job(deps, other).next_retry_at == NOW + BACKOFF[0]


def test_stall_guard_counts_a_restart_at_zero_as_progress() -> None:
    from outriggarr.worker.runner import _StallGuard

    guard = _StallGuard(idle=250.0, cap=100_000.0, clock=scripted_clock(0, 100, 200, 300, 400))
    guard.advanced(100.0)  # the video stream finished (clock 100)
    guard.advanced(0.0)  # the audio stream starts over (clock 200): bytes are still moving
    assert guard.last_advance == 200, "a drop to 0% is a new stream, not a stall"
    guard.advanced(0.0)  # the same value again is not progress (no clock call)
    assert guard.last_advance == 200
    assert not guard.tripped(), "300 - 200 < 250"  # clock 300
    guard.advanced(1.0)  # clock 400
    assert guard.last_advance == 400


async def test_abort_check_treats_a_vanished_row_as_cancelled(deps, session_factory) -> None:
    from outriggarr.worker.runner import abort_check

    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    check = abort_check(deps, job_id, lambda: False)
    assert check() is False
    with session_factory() as s:
        s.delete(s.get(Job, job_id))
        s.commit()
    check = abort_check(deps, job_id, lambda: False)
    assert check() is True, "a deleted job must stop writing into its folder"


async def test_internal_error_on_a_job_cancelled_meanwhile_sends_nothing(
    deps, session_factory
) -> None:
    from outriggarr.worker.runner import _guarded

    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    fake_for(deps, conn_id)
    with session_factory() as s:
        set_setting(s, "notify_on_failed", "1")
        s.commit()

    def download_then_crash(*a, **k):
        with session_factory() as s:  # the user cancels while the download runs
            s.get(Job, job_id).status = JobStatus.cancelled
            s.commit()
        raise RuntimeError("boom")

    deps.source.download = download_then_crash
    await _guarded(deps, job_id, lambda: False)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.cancelled, "the cancel outranks a failure that landed later"
    assert deps.notifier.sent == [], "nothing to announce: the user cancelled it"


async def test_partially_satisfied_target_is_refused_on_the_staged_retry_path(
    deps, session_factory
) -> None:
    from outriggarr.arr.base import TargetInfo

    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id, episode_ids=[42, 43])
    client = fake_for(deps, conn_id)
    # attempt 1 staged the file; a later attempt must not import over a target that has
    # since gained one of its files elsewhere
    dest = deps.staging_dir / str(job_id)
    dest.mkdir(parents=True)
    staged = dest / "Show - S01E42-E43 - Two [WEBDL-1080p].mkv"
    staged.write_bytes(b"x")
    with session_factory() as s:
        s.get(Job, job_id).staged_path = str(staged)
        s.commit()
    from tests.fakes import EPISODE_INFO

    client.info = TargetInfo(
        **{**EPISODE_INFO.__dict__, "has_file": False, "partially_satisfied": True}
    )
    await process_job(deps, job_id)
    job = get_job(deps, job_id)
    assert job.status is JobStatus.failed and job.next_retry_at is None
    assert "already have a file" in job.error and staged.exists(), "refused; the file is kept"
    assert not any(c[0] == "manual_import" for c in client.calls)


def test_claim_loses_to_a_cancel_that_lands_between_its_select_and_update(
    session_factory, monkeypatch
) -> None:
    from outriggarr.worker import runner

    conn_id = add_connection(session_factory)
    job_id = add_job(session_factory, conn_id)
    real_update = runner.update

    def update_after_a_cancel(*args, **kwargs):
        # runs when the UPDATE is built, i.e. after the SELECT chose the row
        with session_factory() as s:
            s.get(Job, job_id).status = JobStatus.cancelled
            s.commit()
        return real_update(*args, **kwargs)

    monkeypatch.setattr(runner, "update", update_after_a_cancel)
    with session_factory() as s:
        assert claim_next_jobs(s, 1, NOW) == [], "the cancel is the user's word"
    with session_factory() as s:
        assert s.get(Job, job_id).status is JobStatus.cancelled
