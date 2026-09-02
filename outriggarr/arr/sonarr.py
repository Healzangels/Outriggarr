from __future__ import annotations

from typing import Any

from outriggarr.arr.base import Wanted, WantedEpisode
from outriggarr.arr.common import ArrHttp, parse_datetime


class SonarrClient(ArrHttp):
    async def wanted(self, series_id: int | None = None) -> list[Wanted]:
        # Sonarr v4 has no seriesId filter on wanted/missing; filter client-side.
        records = await self.get_all_pages(
            "wanted/missing",
            {
                "sortKey": "airDateUtc",
                "sortDirection": "descending",
                "includeSeries": "true",
                "monitored": "true",
            },
        )
        episodes = [_episode(r) for r in records]
        if series_id is not None:
            episodes = [e for e in episodes if e.series_id == series_id]
        return list(episodes)


def _episode(r: dict[str, Any]) -> WantedEpisode:
    series = r.get("series") or {}
    return WantedEpisode(
        id=int(r["id"]),
        series_id=int(r["seriesId"]),
        season_number=int(r["seasonNumber"]),
        episode_number=int(r["episodeNumber"]),
        title=str(r.get("title") or ""),
        air_date_utc=parse_datetime(r.get("airDateUtc")),
        series_title=series.get("title"),
    )
