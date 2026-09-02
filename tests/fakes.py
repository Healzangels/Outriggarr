"""In-memory fakes for the ArrClient and VideoSource protocols."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from outriggarr.arr.base import (
    ArrError,
    CommandStatus,
    EpisodeRef,
    ExtraFilesConfig,
    ImportCandidate,
    ImportFile,
    Language,
    MovieRef,
    QualityDefinition,
    SeriesRef,
    SystemStatus,
    Target,
    TargetInfo,
    Wanted,
)
from outriggarr.db.models import Connection, ConnectionKind
from outriggarr.source import DownloadAborted, DownloadResult, SourceError, VideoRef

DEFAULT_QUALITIES = [
    QualityDefinition(id=i, quality_id=i, name=n, title=n, weight=i)
    for i, n in enumerate(["WEBDL-480p", "WEBDL-720p", "WEBDL-1080p", "WEBDL-2160p"], start=1)
]

EPISODE_INFO = TargetInfo(
    title="Show: Name",
    year=None,
    season=2,
    episode_numbers=(3,),
    episode_title="The/Title",
    has_file=False,
    monitored=True,
)
MOVIE_INFO = TargetInfo(
    title="A Movie",
    year=2020,
    season=None,
    episode_numbers=(),
    episode_title="",
    has_file=False,
    monitored=True,
)


@dataclass
class FakeArrClient:
    kind: ConnectionKind = ConnectionKind.sonarr
    status_result: SystemStatus | Exception | None = None
    qualities: list[QualityDefinition] = field(default_factory=lambda: list(DEFAULT_QUALITIES))
    wanted_items: list[Wanted] = field(default_factory=list)
    visible_paths: set[str] = field(default_factory=lambda: {"/staging"})
    path_error: Exception | None = None
    # manual-import behaviour
    info: TargetInfo | None = None
    info_error: Exception | None = None
    candidate_rejections: tuple[str, ...] = ()
    candidate_languages: tuple[Language, ...] = (Language(0, "Unknown"),)
    candidates_override: list[ImportCandidate] | None = None
    candidates_error: Exception | None = None
    # rejections after reprocessing with explicit ids; None = same as the candidate's
    reprocessed_rejections: tuple[str, ...] | None = None
    reprocess_error: Exception | None = None
    import_error: Exception | None = None
    command_statuses: list[str] = field(default_factory=lambda: ["queued", "started", "completed"])
    import_sets_has_file: bool = True
    has_file: dict[Target, bool] = field(default_factory=dict)  # per-target override
    # library
    series_list: list[SeriesRef] = field(default_factory=list)
    episodes_by_series: dict[int, list[EpisodeRef]] = field(default_factory=dict)
    movies_list: list[MovieRef] = field(default_factory=list)
    library_loads: int = 0
    tags: dict[str, int] = field(default_factory=dict)
    series_tags: dict[int, set[int]] = field(default_factory=dict)
    extra_files: ExtraFilesConfig = ExtraFilesConfig(True, ("srt", "sub"))
    # recording
    calls: list[tuple[str, object]] = field(default_factory=list)
    imports: list[list[ImportFile]] = field(default_factory=list)

    def _info(self, target: Target) -> TargetInfo:
        base = self.info or (MOVIE_INFO if self.kind is ConnectionKind.radarr else EPISODE_INFO)
        if target not in self.has_file:
            return base
        return TargetInfo(**{**base.__dict__, "has_file": self.has_file[target]})

    async def status(self) -> SystemStatus:
        self.calls.append(("status", None))
        if isinstance(self.status_result, Exception):
            raise self.status_result
        return self.status_result or SystemStatus(app_name=self.kind.value.title(), version="0.0.0")

    async def quality_definitions(self) -> list[QualityDefinition]:
        self.calls.append(("quality_definitions", None))
        return list(self.qualities)

    async def wanted(self, series_id: int | None = None) -> list[Wanted]:
        self.calls.append(("wanted", series_id))
        return [
            w
            for w in self.wanted_items
            if series_id is None or getattr(w, "series_id", None) == series_id
        ]

    async def path_visible(self, path: str) -> bool:
        self.calls.append(("path_visible", path))
        if self.path_error is not None:
            raise self.path_error
        return path.rstrip("/") in self.visible_paths

    async def series(self) -> list[SeriesRef]:
        self.calls.append(("series", None))
        if self.kind is not ConnectionKind.sonarr:
            raise ArrError("Radarr has no series")
        self.library_loads += 1
        return list(self.series_list)

    async def episodes(self, series_id: int) -> list[EpisodeRef]:
        self.calls.append(("episodes", series_id))
        if self.kind is not ConnectionKind.sonarr:
            raise ArrError("Radarr has no episodes")
        return list(self.episodes_by_series.get(series_id, []))

    async def movies(self) -> list[MovieRef]:
        self.calls.append(("movies", None))
        if self.kind is not ConnectionKind.radarr:
            raise ArrError("Sonarr has no movies")
        self.library_loads += 1
        return list(self.movies_list)

    async def extra_files_config(self) -> ExtraFilesConfig:
        self.calls.append(("extra_files_config", None))
        return self.extra_files

    async def ensure_tag(self, label: str) -> int:
        self.calls.append(("ensure_tag", label))
        return self.tags.setdefault(label, 100 + len(self.tags))

    async def set_series_tag(self, series_id: int, tag_id: int, present: bool) -> None:
        self.calls.append(("set_series_tag", (series_id, tag_id, present)))
        if self.kind is not ConnectionKind.sonarr:
            raise ArrError("Radarr has no series")
        tags = self.series_tags.setdefault(series_id, set())
        (tags.add if present else tags.discard)(tag_id)

    async def target_info(self, target: Target) -> TargetInfo:
        self.calls.append(("target_info", target))
        if self.info_error is not None:
            raise self.info_error
        return self._info(target)

    async def manual_import_candidates(self, folder: str) -> list[ImportCandidate]:
        self.calls.append(("manual_import_candidates", folder))
        if self.candidates_error is not None:
            raise self.candidates_error
        if self.candidates_override is not None:
            return list(self.candidates_override)
        # Mirror what the *arr would list: the file(s) the test staged under the local
        # folder that corresponds to `folder`.
        local = self.local_folder_for(folder) if self.local_folder_for else None
        names = sorted(p.name for p in local.iterdir()) if local and local.exists() else []
        return [
            ImportCandidate(
                id=i + 1,
                path=f"{folder}/{n}",
                relative_path=n,
                name=n.rsplit(".", 1)[0],
                size=1,
                rejections=self.candidate_rejections,
                languages=self.candidate_languages,
            )
            for i, n in enumerate(names)
        ]

    local_folder_for: Callable[[str], Path] | None = None

    async def reprocess(self, candidate, target, quality_name, languages, season):
        self.calls.append(("reprocess", (candidate.id, target, quality_name, season)))
        if self.reprocess_error is not None:
            raise self.reprocess_error
        if self.reprocessed_rejections is not None:
            return tuple(self.reprocessed_rejections)
        return tuple(candidate.rejections)

    async def manual_import(self, files: list[ImportFile]) -> int:
        self.calls.append(("manual_import", files))
        if self.import_error is not None:
            raise self.import_error
        self.imports.append(list(files))
        if self.import_sets_has_file:
            for f in files:
                self.has_file[f.target] = True
        return 1000 + len(self.imports)

    async def command(self, command_id: int) -> CommandStatus:
        self.calls.append(("command", command_id))
        st = (
            self.command_statuses.pop(0)
            if len(self.command_statuses) > 1
            else self.command_statuses[0]
        )
        return CommandStatus(id=command_id, name="ManualImport", status=st, message=f"msg:{st}")


class FakeArrFactory:
    """Callable[[Connection], ArrClient]. Register a client per connection URL; unknown
    URLs get a healthy default client of the connection's kind."""

    def __init__(self) -> None:
        self.by_url: dict[str, FakeArrClient] = {}
        self.made: list[Connection] = []

    def __call__(self, connection: Connection) -> FakeArrClient:
        self.made.append(connection)
        if connection.url not in self.by_url:
            self.by_url[connection.url] = FakeArrClient(kind=connection.kind)
        return self.by_url[connection.url]


@dataclass
class FakeVideoSource:
    height: int | None = 1080
    ext: str = "mkv"
    title: str = "Uploaded Title"
    error: Exception | None = None
    payload: bytes = b"\x00" * 16
    calls: list[dict[str, object]] = field(default_factory=list)
    videos: list[VideoRef] = field(default_factory=list)
    resolve_error: Exception | None = None
    resolved: list[str] = field(default_factory=list)
    recent: list[VideoRef] = field(default_factory=list)
    recent_error: Exception | None = None
    listed: list[tuple[str, int]] = field(default_factory=list)
    infos: dict[str, VideoRef] = field(default_factory=dict)  # url → full ref
    fetched: list[str] = field(default_factory=list)
    tagged: list[tuple[Path, str]] = field(default_factory=list)
    tag_error: Exception | None = None

    def tag_audio_language(self, path: Path, language: str) -> None:
        self.tagged.append((path, language))
        if self.tag_error is not None:
            raise self.tag_error

    def resolve(self, url: str) -> list[VideoRef]:
        self.resolved.append(url)
        if self.resolve_error is not None:
            raise self.resolve_error
        return list(self.videos)

    def list_recent(self, url: str, limit: int) -> list[VideoRef]:
        self.listed.append((url, limit))
        if self.recent_error is not None:
            raise self.recent_error
        return list(self.recent[:limit])

    def fetch_info(self, url: str) -> VideoRef:
        self.fetched.append(url)
        if url not in self.infos:
            raise SourceError(f"ERROR: [youtube] {url}: Video unavailable")
        return self.infos[url]

    subtitle_langs_available: tuple[str, ...] = ("en",)  # what the fake "upload" carries

    def download(
        self,
        url,
        dest_dir: Path,
        *,
        fmt,
        merge_container,
        progress,
        should_abort,
        subtitle_langs=(),
        auto_subtitles=False,
    ):
        self.calls.append(
            {
                "url": url,
                "dest": dest_dir,
                "fmt": fmt,
                "container": merge_container,
                "subtitle_langs": tuple(subtitle_langs),
                "auto_subtitles": auto_subtitles,
            }
        )
        if should_abort():
            raise DownloadAborted("aborted before start")
        if self.error is not None:
            raise self.error
        dest_dir.mkdir(parents=True, exist_ok=True)
        progress(50.0)
        if should_abort():
            raise DownloadAborted("aborted mid-way")
        path = dest_dir / f"vid123.{self.ext}"
        path.write_bytes(self.payload)
        subs = []
        for lang in subtitle_langs:
            if lang in self.subtitle_langs_available:
                sub = dest_dir / f"vid123.{lang}.srt"
                sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
                subs.append(sub)
        progress(100.0)
        return DownloadResult(
            path=path,
            height=self.height,
            ext=self.ext,
            title=self.title,
            video_id="vid123",
            subtitles=tuple(subs),
        )


@dataclass
class FakeNotifier:
    sent: list[tuple[str, str]] = field(default_factory=list)
    result: bool = True
    error: Exception | None = None

    def send(self, title: str, body: str) -> bool:
        if self.error is not None:
            raise self.error
        self.sent.append((title, body))
        return self.result


__all__ = [
    "ArrError",
    "FakeArrClient",
    "FakeArrFactory",
    "FakeNotifier",
    "FakeVideoSource",
    "SourceError",
]
