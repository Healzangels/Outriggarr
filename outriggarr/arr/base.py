"""ArrClient protocol and the value types it returns.

Everything the rest of the app knows about Sonarr/Radarr is here. Sonarr/Radarr
specifics live in sonarr.py / radarr.py only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


class ArrError(Exception):
    """Any failed *arr call. `str(err)` carries the request and the verbatim response text.
    `retryable` is True for transport errors and 5xx (try again later) and False for a
    4xx/validation answer that will not change on its own."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


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
class SeriesRef:
    id: int
    title: str
    year: int | None
    tvdb_id: int | None
    monitored: bool
    episode_count: int | None = None  # aired episodes Sonarr tracks (its `statistics`)
    episode_file_count: int | None = None


@dataclass(frozen=True)
class EpisodeRef:
    id: int
    season_number: int
    episode_number: int
    title: str
    has_file: bool
    monitored: bool
    air_date_utc: datetime | None
    air_date: date | None = None  # Sonarr's local calendar day (`airDate`), for date matching
    runtime: int | None = None  # minutes (TVDB via Sonarr); 0 or absent → None


@dataclass(frozen=True)
class MovieRef:
    id: int
    title: str
    year: int | None
    tmdb_id: int | None
    has_file: bool
    monitored: bool


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
    # a multi-episode target where SOME episodes already have a file: importing would
    # replace those, so the runner refuses instead of guessing
    partially_satisfied: bool = False


@dataclass(frozen=True)
class ExtraFilesConfig:
    import_extra_files: bool
    extensions: tuple[str, ...]  # e.g. ("srt", "sub")

    def imports(self, ext: str) -> bool:
        return self.import_extra_files and ext.lower().lstrip(".") in self.extensions


@dataclass(frozen=True)
class Language:
    id: int
    name: str


ENGLISH = Language(1, "English")


@dataclass(frozen=True)
class ImportCandidate:
    id: int  # the *arr's transient candidate id (needed by its reprocess call)
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

    async def series(self) -> list[SeriesRef]:
        """All series (Sonarr). Radarr raises ArrError."""
        ...

    async def series_title(self, series_id: int) -> str:
        """Current title of one series (Sonarr); a 404 (deleted/re-added) raises a
        non-retryable ArrError. Radarr raises ArrError."""
        ...

    async def episodes(self, series_id: int) -> list[EpisodeRef]:
        """All episodes of one series (Sonarr). Radarr raises ArrError."""
        ...

    async def movies(self) -> list[MovieRef]:
        """All movies (Radarr). Sonarr raises ArrError."""
        ...

    async def ensure_tag(self, label: str) -> int:
        """Id of the tag with this label, creating it if needed."""
        ...

    async def extra_files_config(self) -> ExtraFilesConfig:
        """Whether the *arr imports sidecars (subtitles) next to a video, and which."""
        ...

    async def set_series_tag(self, series_id: int, tag_id: int, present: bool) -> None:
        """Add or remove one tag on a series (Sonarr). Radarr raises ArrError."""
        ...

    async def manual_import_candidates(self, folder: str) -> list[ImportCandidate]:
        """GET /manualimport?folder=<folder as the *arr sees it>. Never pass seriesId /
        movieId here: that flips the *arr into listing the SERIES/MOVIE folder instead."""
        ...

    async def manual_import(self, files: list[ImportFile]) -> int:
        """POST the ManualImport command (importMode=move). Returns the command id."""
        ...

    async def command(self, command_id: int) -> CommandStatus: ...

    async def reprocess(
        self,
        candidate: ImportCandidate,
        target: Target,
        quality_name: str,
        languages: tuple[Language, ...],
        season: int | None,
    ) -> tuple[str, ...]:
        """Re-evaluate one manual-import candidate WITH explicit ids (what the *arr's own
        UI does after you pick the series/movie) and return the rejections that remain."""
        ...
