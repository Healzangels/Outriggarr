"""Factory: a Connection row → the right ArrClient."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from outriggarr.arr.base import ArrClient
from outriggarr.arr.radarr import RadarrClient
from outriggarr.arr.sonarr import SonarrClient
from outriggarr.db.models import Connection, ConnectionKind

ArrFactory = Callable[[Connection], ArrClient]


def make_client(connection: Connection, http: httpx.AsyncClient) -> ArrClient:
    cls = SonarrClient if connection.kind is ConnectionKind.sonarr else RadarrClient
    return cls(connection.url, connection.api_key, http)
