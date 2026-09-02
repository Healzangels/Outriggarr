"""SQLAlchemy models. See DESIGN.md → Domain model."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ConnectionKind(enum.StrEnum):
    sonarr = "sonarr"
    radarr = "radarr"


class TargetKind(enum.StrEnum):
    episode = "episode"
    movie = "movie"


class JobStatus(enum.StrEnum):
    queued = "queued"
    downloading = "downloading"
    importing = "importing"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.done, JobStatus.failed, JobStatus.cancelled)


class Connection(Base):
    __tablename__ = "connection"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ConnectionKind] = mapped_column(Enum(ConnectionKind), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # How this *arr instance sees the app's staging directory.
    staging_path_remote: Mapped[str] = mapped_column(String(500), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    jobs: Mapped[list[Job]] = relationship(back_populates="connection")


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("connection_id", "target_key", "video_id", name="uq_job_target_video"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connection.id"), nullable=False)
    target_kind: Mapped[TargetKind] = mapped_column(Enum(TargetKind), nullable=False)
    series_id: Mapped[int | None] = mapped_column(Integer)
    episode_ids: Mapped[list[int] | None] = mapped_column(JSON)
    movie_id: Mapped[int | None] = mapped_column(Integer)
    # Derived from target_kind + ids so the dedupe constraint has a scalar to bind to.
    target_key: Mapped[str] = mapped_column(String(200), nullable=False)
    video_id: Mapped[str] = mapped_column(String(100), nullable=False)
    video_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    video_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), nullable=False, default=JobStatus.queued, index=True
    )
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    staged_path: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    connection: Mapped[Connection] = relationship(back_populates="jobs")

    @staticmethod
    def make_target_key(
        kind: TargetKind,
        *,
        series_id: int | None = None,
        episode_ids: list[int] | None = None,
        movie_id: int | None = None,
    ) -> str:
        if kind is TargetKind.episode:
            if series_id is None or not episode_ids:
                raise ValueError("episode target needs series_id and episode_ids")
            return f"episode:{series_id}:{','.join(str(i) for i in sorted(episode_ids))}"
        if movie_id is None:
            raise ValueError("movie target needs movie_id")
        return f"movie:{movie_id}"
