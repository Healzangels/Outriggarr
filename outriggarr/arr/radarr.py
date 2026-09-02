from __future__ import annotations

from typing import Any

from outriggarr.arr.base import ArrError, Target, TargetInfo, Wanted, WantedMovie
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


def _movie(r: dict[str, Any]) -> WantedMovie:
    return WantedMovie(
        id=int(r["id"]),
        title=str(r.get("title") or ""),
        year=int(r["year"]) if r.get("year") else None,
        tmdb_id=int(r["tmdbId"]) if r.get("tmdbId") else None,
    )
