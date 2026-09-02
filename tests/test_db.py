from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from outriggarr.db.models import Connection, ConnectionKind, Job, JobStatus, TargetKind
from outriggarr.settings import DEFAULTS, all_settings, get_setting, set_setting


def test_migration_creates_all_model_tables(session_factory) -> None:
    from outriggarr.db.models import Base

    with session_factory() as s:
        tables = set(inspect(s.get_bind()).get_table_names())
    assert set(Base.metadata.tables) <= tables


def _connection() -> Connection:
    return Connection(
        kind=ConnectionKind.sonarr,
        name="sonarr",
        url="http://sonarr-host:1234",
        api_key="k",
        staging_path_remote="/staging",
    )


def _job(conn: Connection, video_id: str = "abc") -> Job:
    return Job(
        connection=conn,
        target_kind=TargetKind.episode,
        series_id=1,
        episode_ids=[10, 11],
        target_key=Job.make_target_key(TargetKind.episode, series_id=1, episode_ids=[11, 10]),
        video_id=video_id,
        video_url=f"https://example.invalid/{video_id}",
        video_title="t",
    )


def test_job_roundtrip_defaults(session_factory) -> None:
    with session_factory() as s:
        conn = _connection()
        s.add(_job(conn))
        s.commit()
        job = s.query(Job).one()
        assert job.status is JobStatus.queued
        assert job.progress_pct == 0
        assert job.attempts == 0
        assert job.episode_ids == [10, 11]
        assert job.target_key == "episode:1:10,11"
        assert job.created_at.tzinfo is not None
        assert job.created_at <= datetime.now(UTC)


def test_job_dedupe_on_connection_target_video(session_factory) -> None:
    with session_factory() as s:
        conn = _connection()
        s.add(_job(conn))
        s.commit()
        s.add(_job(conn))
        with pytest.raises(IntegrityError):
            s.commit()


def test_job_same_video_different_target_allowed(session_factory) -> None:
    with session_factory() as s:
        conn = _connection()
        s.add(_job(conn))
        other = _job(conn)
        other.episode_ids = [12]
        other.target_key = Job.make_target_key(TargetKind.episode, series_id=1, episode_ids=[12])
        s.add(other)
        s.commit()
        assert s.query(Job).count() == 2


def test_make_target_key_validates() -> None:
    assert Job.make_target_key(TargetKind.movie, movie_id=7) == "movie:7"
    with pytest.raises(ValueError):
        Job.make_target_key(TargetKind.episode, series_id=1, episode_ids=[])
    with pytest.raises(ValueError):
        Job.make_target_key(TargetKind.movie)


def test_job_status_terminal() -> None:
    assert JobStatus.done.is_terminal
    assert JobStatus.failed.is_terminal
    assert JobStatus.cancelled.is_terminal
    assert not JobStatus.queued.is_terminal
    assert not JobStatus.downloading.is_terminal
    assert not JobStatus.importing.is_terminal


def test_settings_default_then_override(session_factory) -> None:
    with session_factory() as s:
        assert get_setting(s, "concurrency") == DEFAULTS["concurrency"]
        set_setting(s, "concurrency", "2")
        s.commit()
    with session_factory() as s:
        assert get_setting(s, "concurrency") == "2"
        assert all_settings(s)["concurrency"] == "2"
        assert set(all_settings(s)) == set(DEFAULTS)
        with pytest.raises(KeyError):
            get_setting(s, "not_a_setting")
        with pytest.raises(KeyError):
            set_setting(s, "not_a_setting", "x")


def test_in_process_migrations_leave_logging_alone(settings) -> None:
    import logging

    from outriggarr.db.session import run_migrations

    root = logging.getLogger()
    before_level, before_handlers = root.level, list(root.handlers)
    root.setLevel(logging.INFO)
    try:
        settings.config_dir.mkdir(parents=True)
        run_migrations(settings.database_url)
        assert root.level == logging.INFO
        assert root.handlers == before_handlers
        for name in ("outriggarr.worker.runner", "outriggarr.main", "uvicorn.error"):
            assert not logging.getLogger(name).disabled, name
    finally:
        root.setLevel(before_level)


def test_datetimes_come_back_utc_aware_from_a_fresh_session(session_factory) -> None:
    from datetime import timedelta, timezone

    plus_two = timezone(timedelta(hours=2))
    with session_factory() as s:
        conn = _connection()
        job = _job(conn)
        job.next_retry_at = datetime(2026, 9, 1, 14, 0, tzinfo=plus_two)  # 12:00 UTC
        s.add(job)
        s.commit()
        job_id = job.id
    with session_factory() as s:
        job = s.get(Job, job_id)
        assert job.created_at.tzinfo is not None
        assert job.created_at.utcoffset().total_seconds() == 0
        assert job.next_retry_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        assert job.next_retry_at.tzinfo is not None


def test_naive_datetime_is_refused(session_factory) -> None:
    with session_factory() as s:
        job = _job(_connection())
        job.next_retry_at = datetime(2026, 9, 1, 12, 0)
        s.add(job)
        with pytest.raises(Exception, match="naive datetime"):
            s.commit()
