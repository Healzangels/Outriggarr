from __future__ import annotations

from typing import Any

from outriggarr.arr.base import (
    ArrError,
    EpisodeRef,
    MovieRef,
    SeriesRef,
    Target,
    TargetInfo,
)
from outriggarr.arr.common import ArrHttp, parse_date, parse_datetime


class SonarrClient(ArrHttp):
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
            partially_satisfied=(
                any(bool(e.get("hasFile")) for e in episodes)
                and not all(bool(e.get("hasFile")) for e in episodes)
            ),
        )

    def _import_ids(self, target: Target) -> dict[str, Any]:
        return {"seriesId": target.series_id, "episodeIds": list(target.episode_ids)}

    def _reprocess_extra(self, target: Target, season: int | None) -> dict[str, Any]:
        return {"seasonNumber": season} if season is not None else {}

    async def series(self) -> list[SeriesRef]:
        data = await self.get("series")
        return [
            SeriesRef(
                id=int(s["id"]),
                title=str(s.get("title") or ""),
                year=int(s["year"]) if s.get("year") else None,
                tvdb_id=int(s["tvdbId"]) if s.get("tvdbId") else None,
                monitored=bool(s.get("monitored")),
                episode_count=_stat(s, "episodeCount"),
                episode_file_count=_stat(s, "episodeFileCount"),
            )
            for s in data
        ]

    async def episodes(self, series_id: int) -> list[EpisodeRef]:
        data = await self.get("episode", {"seriesId": series_id})
        return sorted(
            (
                EpisodeRef(
                    id=int(e["id"]),
                    season_number=int(e["seasonNumber"]),
                    episode_number=int(e["episodeNumber"]),
                    title=str(e.get("title") or ""),
                    has_file=bool(e.get("hasFile")),
                    monitored=bool(e.get("monitored")),
                    air_date_utc=parse_datetime(e.get("airDateUtc")),
                    air_date=parse_date(e.get("airDate")),
                    runtime=int(e["runtime"]) if e.get("runtime") else None,
                )
                for e in data
            ),
            key=lambda e: (e.season_number, e.episode_number),
        )

    async def movies(self) -> list[MovieRef]:
        raise ArrError("Sonarr has no movies")

    async def series_title(self, series_id: int) -> str:
        return str((await self.get(f"series/{series_id}")).get("title") or "")

    async def set_series_tag(self, series_id: int, tag_id: int, present: bool) -> None:
        # Sonarr's PUT wants the whole series resource back; only `tags` changes.
        series = await self.get(f"series/{series_id}")
        tags = {int(t) for t in series.get("tags", [])}
        if present:
            if tag_id in tags:
                return
            tags.add(tag_id)
        else:
            if tag_id not in tags:
                return
            tags.discard(tag_id)
        series["tags"] = sorted(tags)
        await self.put(f"series/{series_id}", series)


def _stat(series: dict[str, Any], key: str) -> int | None:
    stats = series.get("statistics") or {}
    return int(stats[key]) if stats.get(key) is not None else None
