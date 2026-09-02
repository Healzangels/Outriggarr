"""Live lookups the Grab screen needs: resolve a URL, search series/movies, list episodes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from outriggarr.api.deps import ArrFactoryDep, DbSession, SourceDep
from outriggarr.arr.base import ArrError, EpisodeRef, MovieRef, SeriesRef
from outriggarr.db.models import Connection, ConnectionKind
from outriggarr.source import SourceError

router = APIRouter(prefix="/api", tags=["library"])

SEARCH_LIMIT_MAX = 100


class TTLCache:
    """Tiny per-process cache for the big *arr listings (series/movies)."""

    def __init__(self, ttl_seconds: float, now: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl_seconds
        self.now = now
        self._items: dict[Any, tuple[float, Any]] = {}

    async def get(self, key: Any, loader: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._items.get(key)
        if hit is not None and self.now() - hit[0] < self.ttl:
            return hit[1]
        value = await loader()
        self._items[key] = (self.now(), value)
        return value

    def clear(self) -> None:
        self._items.clear()


library_cache = TTLCache(ttl_seconds=60.0)


class ResolveIn(BaseModel):
    url: str = Field(min_length=1, max_length=1000)


class VideoOut(BaseModel):
    id: str
    title: str
    url: str
    duration: int | None
    playlist_index: int | None
    upload_date: str | None


class SeriesOut(BaseModel):
    id: int
    title: str
    year: int | None
    tvdb_id: int | None
    monitored: bool
    episode_count: int | None
    episode_file_count: int | None


class EpisodeOut(BaseModel):
    id: int
    season_number: int
    episode_number: int
    title: str
    has_file: bool
    monitored: bool
    air_date_utc: datetime | None


class MovieOut(BaseModel):
    id: int
    title: str
    year: int | None
    tmdb_id: int | None
    has_file: bool
    monitored: bool


def _arr_502(exc: ArrError) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


def _connection(session: DbSession, connection_id: int, kind: ConnectionKind) -> Connection:
    conn = session.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"connection {connection_id} not found")
    if conn.kind is not kind:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"connection {connection_id} is {conn.kind.value}; this lookup needs {kind.value}",
        )
    return conn


def _search(items: list[Any], q: str, limit: int) -> list[Any]:
    needle = q.strip().lower()
    hits = [i for i in items if needle in i.title.lower()] if needle else list(items)
    hits.sort(key=lambda i: (not i.title.lower().startswith(needle), i.title.lower()))
    return hits[:limit]


@router.post("/resolve", response_model=list[VideoOut])
async def resolve(body: ResolveIn, source: SourceDep) -> list[VideoOut]:
    try:
        videos = await asyncio.to_thread(source.resolve, body.url)
    except SourceError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return [VideoOut(**v.__dict__) for v in videos]


@router.get("/connections/{connection_id}/series", response_model=list[SeriesOut])
async def search_series(
    connection_id: int,
    session: DbSession,
    arr_factory: ArrFactoryDep,
    q: str = "",
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT_MAX)] = 25,
) -> list[SeriesRef]:
    conn = _connection(session, connection_id, ConnectionKind.sonarr)
    client = arr_factory(conn)
    try:
        items = await library_cache.get((conn.id, "series"), client.series)
    except ArrError as exc:
        raise _arr_502(exc) from exc
    return _search(items, q, limit)


@router.get(
    "/connections/{connection_id}/series/{series_id}/episodes", response_model=list[EpisodeOut]
)
async def list_episodes(
    connection_id: int, series_id: int, session: DbSession, arr_factory: ArrFactoryDep
) -> list[EpisodeRef]:
    conn = _connection(session, connection_id, ConnectionKind.sonarr)
    try:
        return await arr_factory(conn).episodes(series_id)
    except ArrError as exc:
        raise _arr_502(exc) from exc


@router.get("/connections/{connection_id}/movies", response_model=list[MovieOut])
async def search_movies(
    connection_id: int,
    session: DbSession,
    arr_factory: ArrFactoryDep,
    q: str = "",
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT_MAX)] = 25,
) -> list[MovieRef]:
    conn = _connection(session, connection_id, ConnectionKind.radarr)
    client = arr_factory(conn)
    try:
        items = await library_cache.get((conn.id, "movies"), client.movies)
    except ArrError as exc:
        raise _arr_502(exc) from exc
    return _search(items, q, limit)
