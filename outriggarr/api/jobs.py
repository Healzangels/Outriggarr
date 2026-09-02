from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from outriggarr.api.deps import DbSession, RunnerDepsDep
from outriggarr.db.models import Connection, ConnectionKind, Job, JobStatus, TargetKind, utcnow

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class TargetIn(BaseModel):
    kind: TargetKind
    series_id: int | None = None
    episode_ids: list[int] | None = None
    movie_id: int | None = None
    label: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _shape(self) -> TargetIn:
        if self.kind is TargetKind.episode:
            if self.series_id is None or not self.episode_ids:
                raise ValueError("an episode target needs series_id and a non-empty episode_ids")
            if self.movie_id is not None:
                raise ValueError("an episode target must not carry movie_id")
            if len(set(self.episode_ids)) != len(self.episode_ids):
                raise ValueError("episode_ids must not repeat")
        else:
            if self.movie_id is None:
                raise ValueError("a movie target needs movie_id")
            if self.series_id is not None or self.episode_ids:
                raise ValueError("a movie target must not carry series_id/episode_ids")
        return self


class VideoIn(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=500)

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("video url must start with http:// or https://")
        return v


class JobIn(BaseModel):
    connection_id: int
    target: TargetIn
    video: VideoIn


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    connection_id: int
    target_kind: TargetKind
    series_id: int | None
    episode_ids: list[int] | None
    movie_id: int | None
    video_id: str
    video_url: str
    video_title: str
    target_label: str | None
    subscription_id: int | None
    format: str | None
    status: JobStatus
    progress_pct: int
    staged_path: str | None
    error: str | None
    attempts: int
    next_retry_at: datetime | None
    created_at: datetime
    finished_at: datetime | None


_KIND_FOR_CONNECTION = {
    ConnectionKind.sonarr: TargetKind.episode,
    ConnectionKind.radarr: TargetKind.movie,
}


@router.post("", response_model=list[JobOut], status_code=status.HTTP_201_CREATED)
def create_jobs(body: list[JobIn], session: DbSession) -> list[Job]:
    if not body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no jobs given")
    jobs: list[Job] = []
    for i, item in enumerate(body):
        conn = session.get(Connection, item.connection_id)
        if conn is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"jobs[{i}]: connection {item.connection_id} not found"
            )
        if not conn.enabled:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"jobs[{i}]: connection {conn.name!r} is disabled",
            )
        if _KIND_FOR_CONNECTION[conn.kind] is not item.target.kind:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"jobs[{i}]: a {conn.kind.value} connection takes "
                f"{_KIND_FOR_CONNECTION[conn.kind].value} targets, not {item.target.kind.value}",
            )
        t = item.target
        job = Job(
            connection=conn,
            target_kind=t.kind,
            series_id=t.series_id,
            episode_ids=t.episode_ids,
            movie_id=t.movie_id,
            target_key=Job.make_target_key(
                t.kind, series_id=t.series_id, episode_ids=t.episode_ids, movie_id=t.movie_id
            ),
            video_id=item.video.id,
            video_url=item.video.url,
            video_title=item.video.title,
            target_label=t.label,
        )
        session.add(job)
        jobs.append(job)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = _find_duplicates(session, body)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"duplicate job(s): {existing}; a job that is not done already exists for the "
            "same connection, target and video (retry or cancel it instead)",
        ) from None
    return jobs


def _find_duplicates(session: DbSession, body: list[JobIn]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    seen: set[tuple[int, str, str]] = set()
    for item in body:
        t = item.target
        key = (
            item.connection_id,
            Job.make_target_key(
                t.kind, series_id=t.series_id, episode_ids=t.episode_ids, movie_id=t.movie_id
            ),
            item.video.id,
        )
        row = session.scalar(
            select(Job).where(
                Job.connection_id == key[0],
                Job.target_key == key[1],
                Job.video_id == key[2],
                Job.status != JobStatus.done,
            )
        )
        if row is not None or key in seen:
            out.append(
                {
                    "connection_id": key[0],
                    "target_key": key[1],
                    "video_id": key[2],
                    "existing_job_id": row.id if row is not None else None,
                }
            )
        seen.add(key)
    return out


@router.get("", response_model=list[JobOut])
def list_jobs(
    session: DbSession,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> list[Job]:
    q = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
    if status_filter is not None:
        q = q.where(Job.status == status_filter)
    return list(session.scalars(q))


def _get_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
    return job


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: DbSession) -> Job:
    return _get_or_404(session, job_id)


RETRYABLE = (JobStatus.failed, JobStatus.cancelled)
CANCELLABLE = (JobStatus.queued, JobStatus.downloading, JobStatus.failed)


def retry_job(session: Session, job_id: int) -> Job:
    """failed | cancelled → queued. Plain function so the web layer can call it too."""
    job = _get_or_404(session, job_id)
    if job.status not in RETRYABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"job {job_id} is {job.status.value}; only failed or cancelled jobs can be retried",
        )
    job.status = JobStatus.queued
    job.next_retry_at = None
    job.error = None
    job.finished_at = None
    job.progress_pct = 0
    job.attempts = 0  # a manual retry starts the backoff ladder afresh
    session.commit()
    return job


DELETABLE = (JobStatus.done, JobStatus.failed, JobStatus.cancelled)


def delete_job(session: Session, job_id: int, staging_dir: Path | None = None) -> None:
    """Remove a finished job (done, terminally failed, cancelled) and its staging folder."""
    job = _get_or_404(session, job_id)
    if job.status not in DELETABLE or (
        job.status is JobStatus.failed and job.next_retry_at is not None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"job {job_id} is {job.status.value}; only done, cancelled or terminally failed "
            "jobs can be deleted",
        )
    if staging_dir is not None:
        shutil.rmtree(staging_dir / str(job.id), ignore_errors=True)
    session.delete(job)
    session.commit()


def cancel_job(session: Session, job_id: int, now: datetime | None = None) -> Job:
    """queued | downloading | failed → cancelled. A running download notices via the
    runner's abort check; staged files are swept by the worker."""
    job = _get_or_404(session, job_id)
    if job.status not in CANCELLABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"job {job_id} is {job.status.value}; only queued, downloading or failed jobs "
            "can be cancelled",
        )
    job.status = JobStatus.cancelled
    job.next_retry_at = None
    job.error = job.error or "cancelled"
    job.finished_at = now or utcnow()
    session.commit()
    return job


@router.post("/{job_id}/retry", response_model=JobOut)
def retry(job_id: int, session: DbSession) -> Job:
    return retry_job(session, job_id)


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel(job_id: int, session: DbSession) -> Job:
    return cancel_job(session, job_id)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(job_id: int, session: DbSession, deps: RunnerDepsDep) -> None:
    delete_job(session, job_id, deps.staging_dir)
