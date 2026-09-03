from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from outriggarr.api.deps import ArrFactoryDep, DbSession, RunnerDepsDep, SourceDep
from outriggarr.api.library import library_cache
from outriggarr.arr import ArrFactory
from outriggarr.arr.base import ArrError
from outriggarr.db.models import Connection, ConnectionKind, Override, Subscription
from outriggarr.matcher import OPTIONAL_STRATEGIES, compile_title_regex
from outriggarr.settings import get_setting
from outriggarr.source import SourceError, VideoSource
from outriggarr.worker.scheduler import (
    AUTO_DOWNLOAD,
    ScanReport,
    SubscriptionNotFound,
    scan_subscription,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


MAX_SOURCES = 10  # channels/playlists per subscription; each is listed on every scan
MAX_VIDEO_LIMIT = 5000  # a flat listing of ~1200 entries takes ~13 s; this bounds a scan


class SubscriptionIn(BaseModel):
    connection_id: int
    series_id: int
    sources: list[str] = Field(default_factory=list)
    # legacy single-URL form of `sources`; folded in by the validator, never stored
    source_url: str | None = Field(default=None, exclude=True)
    format: str | None = Field(default=None, max_length=500)
    video_limit: int | None = Field(default=None, ge=1, le=MAX_VIDEO_LIMIT)
    audio_language: str | None = None  # ISO 639-2; None → source-declared, then global
    # what the scheduler queues by itself: "future" (default), "all", or "none"
    auto_download: str = "future"
    strategies: list[str] = Field(default_factory=lambda: ["title"])
    date_tolerance_days: int = Field(default=2, ge=0, le=60)
    date_offset_days: int = Field(default=0, ge=-60, le=60)
    title_regex: str | None = Field(default=None, max_length=500)
    # a phrase every candidate's title must contain (case-insensitive); pins are exempt
    title_require: str | None = Field(default=None, max_length=100)
    enabled: bool = True

    @field_validator("auto_download")
    @classmethod
    def _auto_download(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in AUTO_DOWNLOAD:
            raise ValueError(f"auto_download must be one of {', '.join(AUTO_DOWNLOAD)}")
        return v

    @field_validator("audio_language")
    @classmethod
    def _audio_language(cls, v: str | None) -> str | None:
        v = (v or "").strip().lower()
        if not v:
            return None
        if not (len(v) == 3 and v.isalpha()):
            raise ValueError("audio_language must be a 3-letter ISO 639-2 code (e.g. jpn) or blank")
        return v

    @model_validator(mode="after")
    def _sources(self) -> SubscriptionIn:
        cleaned: list[str] = []
        for raw in [*self.sources, *([self.source_url] if self.source_url else [])]:
            url = raw.strip()
            if not url:
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError(f"source {url!r} must start with http:// or https://")
            if len(url) > 1000:
                raise ValueError("a source URL may be at most 1000 characters")
            if url not in cleaned:
                cleaned.append(url)
        if not cleaned:
            raise ValueError("at least one source URL is needed")
        if len(cleaned) > MAX_SOURCES:
            raise ValueError(f"at most {MAX_SOURCES} sources per subscription")
        self.sources, self.source_url = cleaned, None
        return self

    @field_validator("strategies")
    @classmethod
    def _strategies(cls, v: list[str]) -> list[str]:
        unknown = [s for s in v if s not in OPTIONAL_STRATEGIES]
        if unknown:
            raise ValueError(
                f"unknown strategies {unknown}; allowed: {sorted(OPTIONAL_STRATEGIES)}"
            )
        return list(dict.fromkeys(v))

    @field_validator("format", "title_regex", "title_require")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None

    @field_validator("title_require")
    @classmethod
    def _require(cls, v: str | None) -> str | None:
        if v is not None and not any(c.isalnum() for c in v):
            raise ValueError("title_require needs at least one letter or digit")
        return v

    @field_validator("title_regex")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v:
            try:
                compile_title_regex(v)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"title_regex: {exc}") from exc
            except Exception as exc:  # re.error
                raise ValueError(f"title_regex does not compile: {exc}") from exc
        return v


class SubscriptionOut(BaseModel):
    """A stored row as it is: no input validators, so a cap lowered or a strategy renamed
    in a later release never makes the listing of existing rows a 500."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    connection_id: int
    series_id: int
    tvdb_id: int | None
    title: str
    sources: list[str]
    format: str | None
    video_limit: int | None
    audio_language: str | None
    auto_download: str
    strategies: list[str]
    date_tolerance_days: int
    date_offset_days: int
    title_regex: str | None
    title_require: str | None
    enabled: bool
    created_at: datetime
    last_scan_at: datetime | None
    last_scan_result: dict[str, Any] | None


class OverrideIn(BaseModel):
    season: int = Field(ge=0)
    episode: int = Field(ge=0)


class OverrideOut(OverrideIn):
    model_config = ConfigDict(from_attributes=True)
    video_id: str
    video_url: str | None = None
    video_title: str | None = None


class OverrideByUrlIn(OverrideIn):
    url: str = Field(min_length=1, max_length=1000)

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


def _get_or_404(session: Session, subscription_id: int) -> Subscription:
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"subscription {subscription_id} not found")
    return sub


async def _series_lookup(
    session: Session, arr_factory: ArrFactory, connection_id: int, series_id: int
) -> tuple[Connection, str, int | None]:
    conn = session.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"connection {connection_id} not found")
    if conn.kind is not ConnectionKind.sonarr:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "subscriptions need a Sonarr connection"
        )
    client = arr_factory(conn)
    try:
        series = await library_cache.get((conn.id, "series"), client.series)
    except ArrError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    hit = next((s for s in series if s.id == series_id), None)
    if hit is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"series {series_id} is not in Sonarr"
        )
    return conn, hit.title, hit.tvdb_id


async def create_subscription(
    session: Session, arr_factory: ArrFactory, body: SubscriptionIn
) -> Subscription:
    conn, title, tvdb_id = await _series_lookup(
        session, arr_factory, body.connection_id, body.series_id
    )
    sub = Subscription(**body.model_dump(), title=title, tvdb_id=tvdb_id)
    session.add(sub)
    try:
        session.commit()
        await _apply_tag(session, arr_factory, conn, body.series_id, present=True)
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(Subscription).where(
                Subscription.connection_id == conn.id, Subscription.series_id == body.series_id
            )
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"series {body.series_id} is already subscribed (subscription "
            f"{existing.id if existing else '?'})",
        ) from None
    return sub


async def update_subscription(
    session: Session, arr_factory: ArrFactory, subscription_id: int, body: SubscriptionIn
) -> Subscription:
    sub = _get_or_404(session, subscription_id)
    moved = (body.connection_id, body.series_id) != (sub.connection_id, sub.series_id)
    old_conn, old_series = sub.connection, sub.series_id
    if moved:
        _, title, tvdb_id = await _series_lookup(
            session, arr_factory, body.connection_id, body.series_id
        )
        sub.title, sub.tvdb_id = title, tvdb_id
    for k, v in body.model_dump().items():
        setattr(sub, k, v)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"series {body.series_id} is already subscribed"
        ) from None
    if moved:  # the courtesy tag follows the subscription to its new series
        await _apply_tag(session, arr_factory, old_conn, old_series, present=False)
        await _apply_tag(session, arr_factory, sub.connection, sub.series_id, present=True)
    return sub


async def _apply_tag(
    session: Session, arr_factory: ArrFactory, conn: Connection, series_id: int, *, present: bool
) -> None:
    """Optional: mirror the subscription as a tag on the Sonarr series. Failures are
    logged, never fatal — the tag is a courtesy, the subscription is the truth."""
    label = get_setting(session, "sonarr_tag")
    if not label:
        return
    client = arr_factory(conn)
    try:
        tag_id = await client.ensure_tag(label)
        await client.set_series_tag(series_id, tag_id, present)
    except ArrError as exc:
        log.warning("sonarr tag %r on series %d not applied: %s", label, series_id, exc)


async def delete_subscription(
    session: Session, arr_factory: ArrFactory, subscription_id: int
) -> None:
    sub = _get_or_404(session, subscription_id)
    conn, series_id = sub.connection, sub.series_id
    for job in sub.jobs:
        job.subscription_id = None
    session.delete(sub)
    session.commit()
    await _apply_tag(session, arr_factory, conn, series_id, present=False)


def set_override(
    session: Session, subscription_id: int, video_id: str, body: OverrideIn
) -> Override:
    sub = _get_or_404(session, subscription_id)
    row = next((o for o in sub.overrides if o.video_id == video_id), None)
    if row is None:
        row = Override(
            subscription=sub, video_id=video_id, season=body.season, episode=body.episode
        )
        session.add(row)
    else:
        row.season, row.episode = body.season, body.episode
    session.commit()
    return row


async def set_override_by_url(
    session: Session, source: VideoSource, subscription_id: int, body: OverrideByUrlIn
) -> Override:
    """Resolve a pasted URL to one video and pin it; works for videos outside the listing."""
    sub = _get_or_404(session, subscription_id)
    try:
        # fetch_info is a single-video extract (noplaylist), so a watch URL copied from a
        # playlist view (`watch?v=…&list=…`) resolves to that video, not the playlist.
        video = await asyncio.to_thread(source.fetch_info, body.url)
    except SourceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    row = next((o for o in sub.overrides if o.video_id == video.id), None)
    if row is None:
        row = Override(
            subscription=sub, video_id=video.id, season=body.season, episode=body.episode
        )
        session.add(row)
    row.season, row.episode = body.season, body.episode
    row.video_url, row.video_title = video.url, video.title
    session.commit()
    return row


def delete_override(session: Session, subscription_id: int, video_id: str) -> None:
    sub = _get_or_404(session, subscription_id)
    row = next((o for o in sub.overrides if o.video_id == video_id), None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no override for video {video_id!r}")
    session.delete(row)
    session.commit()


class DownloadIn(BaseModel):
    episode_ids: list[int] | None = None  # None → every current match


async def run_scan(
    deps,
    subscription_id: int,
    *,
    dry_run: bool,
    manual: bool = False,
    episode_ids: set[int] | None = None,
) -> ScanReport:
    try:
        return await scan_subscription(
            deps, subscription_id, dry_run=dry_run, manual=manual, episode_ids=episode_ids
        )
    except SubscriptionNotFound:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"subscription {subscription_id} not found"
        ) from None


# ---- routes -------------------------------------------------------------------------


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(session: DbSession) -> list[Subscription]:
    return list(session.scalars(select(Subscription).order_by(Subscription.title, Subscription.id)))


@router.post("", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
async def create(
    body: SubscriptionIn, session: DbSession, arr_factory: ArrFactoryDep
) -> Subscription:
    return await create_subscription(session, arr_factory, body)


@router.get("/{subscription_id}", response_model=SubscriptionOut)
def get_subscription(subscription_id: int, session: DbSession) -> Subscription:
    return _get_or_404(session, subscription_id)


@router.put("/{subscription_id}", response_model=SubscriptionOut)
async def update(
    subscription_id: int, body: SubscriptionIn, session: DbSession, arr_factory: ArrFactoryDep
) -> Subscription:
    return await update_subscription(session, arr_factory, subscription_id, body)


@router.delete("/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(subscription_id: int, session: DbSession, arr_factory: ArrFactoryDep) -> None:
    await delete_subscription(session, arr_factory, subscription_id)


@router.get("/{subscription_id}/overrides", response_model=list[OverrideOut])
def list_overrides(subscription_id: int, session: DbSession) -> list[Override]:
    return list(_get_or_404(session, subscription_id).overrides)


@router.put("/{subscription_id}/overrides/{video_id}", response_model=OverrideOut)
def put_override(
    subscription_id: int, video_id: str, body: OverrideIn, session: DbSession
) -> Override:
    return set_override(session, subscription_id, video_id, body)


@router.post("/{subscription_id}/overrides", response_model=OverrideOut)
async def post_override_by_url(
    subscription_id: int, body: OverrideByUrlIn, session: DbSession, source: SourceDep
) -> Override:
    return await set_override_by_url(session, source, subscription_id, body)


@router.delete("/{subscription_id}/overrides/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_override(subscription_id: int, video_id: str, session: DbSession) -> None:
    delete_override(session, subscription_id, video_id)


def _report_or_502(report) -> dict:
    """A listing or matching failure is folded into `report.error` for the pages; the
    JSON contract says it with the status code too."""
    if report.error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=report.as_dict())
    return report.as_dict()


@router.post("/{subscription_id}/scan")
async def scan(subscription_id: int, deps: RunnerDepsDep) -> dict:
    return _report_or_502(await run_scan(deps, subscription_id, dry_run=False))


@router.post("/{subscription_id}/download")
async def download(
    subscription_id: int, deps: RunnerDepsDep, body: DownloadIn | None = None
) -> dict:
    """A manual download: every current match, or just the given episodes, whatever
    the subscription's auto-download policy says."""
    ids = set(body.episode_ids) if body and body.episode_ids is not None else None
    return _report_or_502(
        await run_scan(deps, subscription_id, dry_run=False, manual=True, episode_ids=ids)
    )


@router.get("/{subscription_id}/preview")
async def preview(subscription_id: int, deps: RunnerDepsDep) -> dict:
    return _report_or_502(await run_scan(deps, subscription_id, dry_run=True))
