"""In-memory fakes for the ArrClient protocol (VideoSource fake arrives with M2)."""

from __future__ import annotations

from dataclasses import dataclass, field

from outriggarr.arr.base import QualityDefinition, SystemStatus, Wanted
from outriggarr.db.models import Connection, ConnectionKind


@dataclass
class FakeArrClient:
    kind: ConnectionKind = ConnectionKind.sonarr
    status_result: SystemStatus | Exception | None = None
    qualities: list[QualityDefinition] = field(default_factory=list)
    wanted_items: list[Wanted] = field(default_factory=list)
    visible_paths: set[str] = field(default_factory=lambda: {"/staging"})
    path_error: Exception | None = None
    calls: list[tuple[str, object]] = field(default_factory=list)

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
