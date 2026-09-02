"""SQLAlchemy models. See DESIGN.md → Domain model."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on SQLite, which stores none of the tz info itself.

    Writes convert to UTC and strip the tz; reads re-attach UTC. Naive input is refused
    so a local-time value can never be stored as if it were UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


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


class Subscription(Base):
    """A Sonarr series with a video source attached. Which episodes are wanted still
    comes from Sonarr; overrides pin a video to an episode when matching cannot."""

    __tablename__ = "subscription"
    __table_args__ = (
        UniqueConstraint("connection_id", "series_id", name="uq_subscription_series"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connection.id"), nullable=False)
    series_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tvdb_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # One or more channels/playlists; a scan lists every one and matches against the
    # union, so a series whose episodes are split across channels needs one subscription.
    sources: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    format: Mapped[str | None] = mapped_column(String(500))  # None → global default
    # Newest videos listed per scan; None → the global scan_video_limit. A channel whose
    # episodes sit behind hundreds of newer uploads needs a deeper listing than the rest.
    video_limit: Mapped[int | None] = mapped_column(Integer)
    # Which optional strategies run, in the matcher's fixed order. Override always runs.
    strategies: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    date_tolerance_days: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    date_offset_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_regex: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_scan_result: Mapped[dict | None] = mapped_column(JSON)

    connection: Mapped[Connection] = relationship()
    overrides: Mapped[list[Override]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="subscription")


class Override(Base):
    __tablename__ = "override"
    __table_args__ = (UniqueConstraint("subscription_id", "video_id", name="uq_override_video"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscription.id", ondelete="CASCADE"), nullable=False
    )
    video_id: Mapped[str] = mapped_column(String(100), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set when the override was given as a URL: lets a scan use a video that is not in
    # the source's newest-N listing (older upload, another channel).
    video_url: Mapped[str | None] = mapped_column(String(1000))
    video_title: Mapped[str | None] = mapped_column(String(500))

    subscription: Mapped[Subscription] = relationship(back_populates="overrides")


class VideoMeta(Base):
    """Per-video facts that cost a full yt-dlp extract to learn (upload date). Cached so
    the date strategy never re-fetches the same video on every scan."""

    __tablename__ = "video_meta"

    video_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    upload_date: Mapped[str | None] = mapped_column(String(8))  # YYYYMMDD or NULL
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        # Dedupe: at most one job that is not `done` per (connection, target, video). A
        # done job is history; if Sonarr loses the file the same video can be queued again.
        Index(
            "ux_job_live_target_video",
            "connection_id",
            "target_key",
            "video_id",
            unique=True,
            sqlite_where=text("status != 'done'"),
        ),
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
    # Human label for the target ("Hot Ones S30E09 - …"), supplied by whoever creates the
    # job (the GUI knows it); purely for display.
    target_label: Mapped[str | None] = mapped_column(String(300))
    # Set by the scheduler; NULL for manual grabs. SET NULL when the subscription goes.
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscription.id", ondelete="SET NULL")
    )
    format: Mapped[str | None] = mapped_column(String(500))  # None → global default
    # How the scheduler paired video and episode (override/regex/exact/contains/date;
    # NULL for a manual grab) and the length evidence it had, for the Matches review page.
    matched_by: Mapped[str | None] = mapped_column(String(20))
    video_duration: Mapped[int | None] = mapped_column(Integer)  # seconds, from the listing
    target_runtime: Mapped[int | None] = mapped_column(Integer)  # minutes, from the *arr
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # "looks right" clicked
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), nullable=False, default=JobStatus.queued, index=True
    )
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    staged_path: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    connection: Mapped[Connection] = relationship(back_populates="jobs")
    subscription: Mapped[Subscription | None] = relationship(back_populates="jobs")

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
            return f"episode:{series_id}:{','.join(str(i) for i in sorted(set(episode_ids)))}"
        if movie_id is None:
            raise ValueError("movie target needs movie_id")
        return f"movie:{movie_id}"
