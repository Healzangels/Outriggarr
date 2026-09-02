"""ArrClient protocol and the value types it returns.

Everything the rest of the app knows about Sonarr/Radarr is here. Sonarr/Radarr
specifics live in sonarr.py / radarr.py only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class ArrError(Exception):
    """Any failed *arr call. `str(err)` carries the request and the verbatim response text."""


@dataclass(frozen=True)
class SystemStatus:
    app_name: str
    version: str


@dataclass(frozen=True)
class QualityDefinition:
    id: int
    quality_id: int
    name: str  # canonical quality name, e.g. "WEBDL-1080p"
    title: str  # user-editable display title
    weight: int


@dataclass(frozen=True)
class WantedEpisode:
    id: int
    series_id: int
    season_number: int
    episode_number: int
    title: str
    air_date_utc: datetime | None
    series_title: str | None


@dataclass(frozen=True)
class WantedMovie:
    id: int
    title: str
    year: int | None
    tmdb_id: int | None


Wanted = WantedEpisode | WantedMovie


class ArrClient(Protocol):
    async def status(self) -> SystemStatus: ...

    async def quality_definitions(self) -> list[QualityDefinition]: ...

    async def wanted(self, series_id: int | None = None) -> list[Wanted]:
        """Monitored, missing items. `series_id` filters Sonarr; Radarr ignores it."""
        ...

    async def path_visible(self, path: str) -> bool:
        """True if the *arr instance can see `path` (a directory) on its own filesystem."""
        ...
