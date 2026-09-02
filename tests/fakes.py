"""In-memory fakes for the ArrClient and VideoSource protocols."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from outriggarr.arr.base import (
    ArrError,
    CommandStatus,
    ImportCandidate,
    ImportFile,
    Language,
    QualityDefinition,
    SystemStatus,
    Target,
    TargetInfo,
    Wanted,
)
from outriggarr.db.models import Connection, ConnectionKind
from outriggarr.source import DownloadAborted, DownloadResult

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
    import_error: Exception | None = None
    command_statuses: list[str] = field(default_factory=lambda: ["queued", "started", "completed"])
    import_sets_has_file: bool = True
    has_file: dict[Target, bool] = field(default_factory=dict)  # per-target override
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
                path=f"{folder}/{n}",
                relative_path=n,
                name=n.rsplit(".", 1)[0],
                size=1,
                rejections=self.candidate_rejections,
                languages=self.candidate_languages,
            )
            for n in names
        ]

    local_folder_for: Callable[[str], Path] | None = None

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

    def download(self, url, dest_dir: Path, *, fmt, merge_container, progress, should_abort):
        self.calls.append({"url": url, "dest": dest_dir, "fmt": fmt, "container": merge_container})
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
        progress(100.0)
        return DownloadResult(
            path=path, height=self.height, ext=self.ext, title=self.title, video_id="vid123"
        )


__all__ = ["ArrError", "FakeArrClient", "FakeArrFactory", "FakeVideoSource"]
