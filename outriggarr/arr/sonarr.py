from __future__ import annotations

from typing import Any

from outriggarr.arr.base import ArrError, Target, TargetInfo, Wanted, WantedEpisode
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

    async def target_info(self, target: Target) -> TargetInfo:
        if target.is_movie:
            raise ArrError("Sonarr cannot import a movie target")
        episodes = [await self.get(f"episode/{eid}") for eid in target.episode_ids]
        wrong = [e for e in episodes if int(e.get("seriesId", -1)) != target.series_id]
        if wrong:
            raise ArrError(
                f"episode ids {[e['id'] for e in wrong]} do not belong to series {target.series_id}"
            )
        series = episodes[0].get("series") or await self.get(f"series/{target.series_id}")
        seasons = {int(e["seasonNumber"]) for e in episodes}
        if len(seasons) != 1:
            raise ArrError(f"episodes span several seasons {sorted(seasons)}; one job per season")
        ordered = sorted(episodes, key=lambda e: int(e["episodeNumber"]))
        return TargetInfo(
            title=str(series.get("title", "")),
            year=None,
            season=seasons.pop(),
            episode_numbers=tuple(int(e["episodeNumber"]) for e in ordered),
            episode_title=" + ".join(str(e.get("title") or "") for e in ordered).strip(" +"),
            has_file=all(bool(e.get("hasFile")) for e in episodes),
            monitored=all(bool(e.get("monitored")) for e in episodes),
        )

    def _import_ids(self, target: Target) -> dict[str, Any]:
        return {"seriesId": target.series_id, "episodeIds": list(target.episode_ids)}


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
