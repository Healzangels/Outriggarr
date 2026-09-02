"""ArrClient protocol and the value types it returns.

Everything the rest of the app knows about Sonarr/Radarr is here. Sonarr/Radarr
specifics live in sonarr.py / radarr.py only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Target:
    """What a job imports into: Sonarr episodes of one series, or one Radarr movie."""

    series_id: int | None = None
    episode_ids: tuple[int, ...] = ()
    movie_id: int | None = None

    @property
    def is_movie(self) -> bool:
        return self.movie_id is not None

    def __post_init__(self) -> None:
        if self.is_movie:
            if self.series_id is not None or self.episode_ids:
                raise ValueError("a movie target carries only movie_id")
        elif self.series_id is None or not self.episode_ids:
            raise ValueError("an episode target needs series_id and episode_ids")


@dataclass(frozen=True)
class TargetInfo:
    """Current *arr view of a target: naming inputs plus whether it already has a file."""

    title: str  # series title or movie title
    year: int | None
    season: int | None
    episode_numbers: tuple[int, ...]
    episode_title: str
    has_file: bool
    monitored: bool


@dataclass(frozen=True)
class Language:
    id: int
    name: str


ENGLISH = Language(1, "English")


@dataclass(frozen=True)
class ImportCandidate:
    path: str  # as the *arr sees it
    relative_path: str
    name: str
    size: int
    rejections: tuple[str, ...]
    languages: tuple[Language, ...]


@dataclass(frozen=True)
class ImportFile:
    path: str
    quality_name: str
    languages: tuple[Language, ...]
    target: Target


@dataclass(frozen=True)
class CommandStatus:
    id: int
    name: str
    status: str
    message: str | None = None
    _finished: tuple[str, ...] = field(
        default=("completed", "failed", "aborted", "cancelled", "orphaned"), repr=False
    )

    @property
    def finished(self) -> bool:
        return self.status in self._finished

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def languages_for_import(candidate: ImportCandidate) -> tuple[Language, ...]:
    """The *arr's parsed languages, or English when it could only say 'Unknown' (id 0)."""
    known = tuple(lang for lang in candidate.languages if lang.id > 0)
    return known or (ENGLISH,)


class ArrClient(Protocol):
    async def status(self) -> SystemStatus: ...

    async def quality_definitions(self) -> list[QualityDefinition]: ...

    async def wanted(self, series_id: int | None = None) -> list[Wanted]:
        """Monitored, missing items. `series_id` filters Sonarr; Radarr ignores it."""
        ...

    async def path_visible(self, path: str) -> bool:
        """True if the *arr instance can see `path` (a directory) on its own filesystem."""
        ...

    async def target_info(self, target: Target) -> TargetInfo: ...

    async def manual_import_candidates(self, folder: str) -> list[ImportCandidate]:
        """GET /manualimport?folder=<folder as the *arr sees it>. Never pass seriesId /
        movieId here: that flips the *arr into listing the SERIES/MOVIE folder instead."""
        ...

    async def manual_import(self, files: list[ImportFile]) -> int:
        """POST the ManualImport command (importMode=move). Returns the command id."""
        ...

    async def command(self, command_id: int) -> CommandStatus: ...
