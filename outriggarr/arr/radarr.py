from __future__ import annotations

from typing import Any

from outriggarr.arr.base import Wanted, WantedMovie
from outriggarr.arr.common import ArrHttp


class RadarrClient(ArrHttp):
    async def wanted(self, series_id: int | None = None) -> list[Wanted]:
        records = await self.get_all_pages(
            "wanted/missing",
            {"sortKey": "title", "sortDirection": "ascending", "monitored": "true"},
        )
        return [_movie(r) for r in records]


def _movie(r: dict[str, Any]) -> WantedMovie:
    return WantedMovie(
        id=int(r["id"]),
        title=str(r.get("title") or ""),
        year=int(r["year"]) if r.get("year") else None,
        tmdb_id=int(r["tmdbId"]) if r.get("tmdbId") else None,
    )
