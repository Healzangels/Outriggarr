from __future__ import annotations

from typing import Any

from outriggarr.arr.base import (
    ArrError,
    EpisodeRef,
    MovieRef,
    SeriesRef,
    Target,
    TargetInfo,
    Wanted,
    WantedMovie,
)
from outriggarr.arr.common import ArrHttp


class RadarrClient(ArrHttp):
    async def wanted(self, series_id: int | None = None) -> list[Wanted]:
        records = await self.get_all_pages(
            "wanted/missing",
            {"sortKey": "title", "sortDirection": "ascending", "monitored": "true"},
        )
        return [_movie(r) for r in records]

    async def target_info(self, target: Target) -> TargetInfo:
        if not target.is_movie:
            raise ArrError("Radarr cannot import an episode target")
        m = await self.get(f"movie/{target.movie_id}")
        return TargetInfo(
            title=str(m.get("title", "")),
            year=int(m["year"]) if m.get("year") else None,
            season=None,
            episode_numbers=(),
            episode_title="",
            has_file=bool(m.get("hasFile")),
            monitored=bool(m.get("monitored")),
        )

    def _import_ids(self, target: Target) -> dict[str, Any]:
        return {"movieId": target.movie_id}

    async def series(self) -> list[SeriesRef]:
        raise ArrError("Radarr has no series")

    async def episodes(self, series_id: int) -> list[EpisodeRef]:
        raise ArrError("Radarr has no episodes")

    async def series_title(self, series_id: int) -> str:
        raise ArrError("Radarr has no series")

    async def set_series_tag(self, series_id: int, tag_id: int, present: bool) -> None:
        raise ArrError("Radarr has no series")

    async def movies(self) -> list[MovieRef]:
        data = await self.get("movie")
        return [
            MovieRef(
                id=int(m["id"]),
                title=str(m.get("title") or ""),
                year=int(m["year"]) if m.get("year") else None,
                tmdb_id=int(m["tmdbId"]) if m.get("tmdbId") else None,
                has_file=bool(m.get("hasFile")),
                monitored=bool(m.get("monitored")),
            )
            for m in data
        ]


def _movie(r: dict[str, Any]) -> WantedMovie:
    return WantedMovie(
        id=int(r["id"]),
        title=str(r.get("title") or ""),
        year=int(r["year"]) if r.get("year") else None,
        tmdb_id=int(r["tmdbId"]) if r.get("tmdbId") else None,
    )
