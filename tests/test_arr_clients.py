"""Sonarr/Radarr clients against an httpx MockTransport. No network."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from outriggarr.arr.base import ArrError, WantedEpisode, WantedMovie
from outriggarr.arr.common import PAGE_SIZE
from outriggarr.arr.radarr import RadarrClient
from outriggarr.arr.sonarr import SonarrClient

pytestmark = pytest.mark.anyio

BASE = "http://arr-host:1234/base"
KEY = "secret-key"


class Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make(cls, handler):
    rec = Recorder(handler)
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    return cls(BASE, KEY, http), rec


def paged(records: list[dict], page: int, page_size: int) -> httpx.Response:
    start = (page - 1) * page_size
    return httpx.Response(
        200,
        json={
            "page": page,
            "pageSize": page_size,
            "totalRecords": len(records),
            "records": records[start : start + page_size],
        },
    )


async def test_status_uses_v3_path_base_and_api_key_header() -> None:
    client, rec = make(
        SonarrClient,
        lambda r: httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.9.2244"}),
    )
    st = await client.status()
    assert (st.app_name, st.version) == ("Sonarr", "4.0.9.2244")
    req = rec.requests[0]
    assert str(req.url) == f"{BASE}/api/v3/system/status"
    assert req.headers["X-Api-Key"] == KEY


async def test_http_error_surfaces_verbatim_body() -> None:
    client, _ = make(SonarrClient, lambda r: httpx.Response(401, text='{"error": "Unauthorized"}'))
    with pytest.raises(ArrError) as ei:
        await client.status()
    msg = str(ei.value)
    assert "HTTP 401" in msg
    assert '{"error": "Unauthorized"}' in msg
    assert f"{BASE}/api/v3/system/status" in msg


async def test_transport_error_surfaces_as_arr_error() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=r)

    client, _ = make(SonarrClient, boom)
    with pytest.raises(ArrError, match="connection refused"):
        await client.status()


async def test_non_json_response_is_an_error() -> None:
    client, _ = make(SonarrClient, lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(ArrError, match="non-JSON"):
        await client.status()


async def test_quality_definitions_parse() -> None:
    body = [
        {"id": 3, "quality": {"id": 3, "name": "WEBDL-1080p"}, "title": "WEB 1080p", "weight": 9},
        {"id": 1, "quality": {"id": 1, "name": "SDTV"}, "weight": 1},
    ]
    client, rec = make(SonarrClient, lambda r: httpx.Response(200, json=body))
    qs = await client.quality_definitions()
    assert str(rec.requests[0].url) == f"{BASE}/api/v3/qualitydefinition"
    assert [(q.id, q.quality_id, q.name, q.title, q.weight) for q in qs] == [
        (3, 3, "WEBDL-1080p", "WEB 1080p", 9),
        (1, 1, "SDTV", "SDTV", 1),
    ]


def _ep(i: int, series_id: int, **kw) -> dict:
    return {
        "id": i,
        "seriesId": series_id,
        "seasonNumber": 1,
        "episodeNumber": i,
        "title": f"Ep {i}",
        "airDateUtc": "2024-03-01T00:00:00Z",
        "series": {"title": f"Show {series_id}"},
        **kw,
    }


async def test_sonarr_wanted_pages_and_filters_by_series() -> None:
    records = [_ep(i, series_id=1 if i % 2 else 2) for i in range(1, PAGE_SIZE * 2 + 6)]

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/base/api/v3/wanted/missing"
        assert r.url.params["monitored"] == "true"
        assert r.url.params["includeSeries"] == "true"
        return paged(records, int(r.url.params["page"]), int(r.url.params["pageSize"]))

    client, rec = make(SonarrClient, handler)
    got = await client.wanted(series_id=1)
    assert len(rec.requests) == 3  # 200 + 200 + 5 records, three pages
    assert all(isinstance(e, WantedEpisode) for e in got)
    assert {e.series_id for e in got} == {1}
    assert len(got) == len([r for r in records if r["seriesId"] == 1])
    first = got[0]
    assert first.series_title == "Show 1"
    assert first.air_date_utc == datetime(2024, 3, 1, tzinfo=UTC)

    everything = await client.wanted()
    assert len(everything) == len(records)


async def test_sonarr_wanted_stops_on_empty_page_even_if_total_lies() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        page = int(r.url.params["page"])
        return httpx.Response(
            200,
            json={"totalRecords": 999, "records": [_ep(1, 1)] if page == 1 else []},
        )

    client, rec = make(SonarrClient, handler)
    got = await client.wanted()
    assert len(got) == 1
    assert len(rec.requests) == 2


async def test_sonarr_wanted_tolerates_missing_optional_fields() -> None:
    rec_body = {"id": 5, "seriesId": 9, "seasonNumber": 2, "episodeNumber": 3}
    client, _ = make(SonarrClient, lambda r: paged([rec_body], 1, PAGE_SIZE))
    (ep,) = await client.wanted()
    assert (ep.title, ep.air_date_utc, ep.series_title) == ("", None, None)


async def test_radarr_wanted_parses_movies() -> None:
    records = [
        {"id": 7, "title": "A Film", "year": 2020, "tmdbId": 123},
        {"id": 8, "title": "No Year"},
    ]

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/base/api/v3/wanted/missing"
        assert r.url.params["monitored"] == "true"
        return paged(records, int(r.url.params["page"]), int(r.url.params["pageSize"]))

    client, _ = make(RadarrClient, handler)
    got = await client.wanted()
    assert got == [
        WantedMovie(id=7, title="A Film", year=2020, tmdb_id=123),
        WantedMovie(id=8, title="No Year", year=None, tmdb_id=None),
    ]


@pytest.mark.parametrize(
    ("path", "expected_parent", "listing", "expected"),
    [
        ("/staging", "/", ["/config/", "/staging/"], True),
        ("/staging/", "/", ["/staging"], True),
        ("/mnt/user/staging", "/mnt/user/", ["/mnt/user/media/"], False),
        ("/staging", "/", [], False),
    ],
)
async def test_path_visible_lists_parent_and_looks_for_the_dir(
    path: str, expected_parent: str, listing: list[str], expected: bool
) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/base/api/v3/filesystem"
        assert r.url.params["path"] == expected_parent
        assert r.url.params["includeFiles"] == "false"
        return httpx.Response(
            200,
            json={
                "parent": None,
                "directories": [{"type": "folder", "name": p, "path": p} for p in listing],
                "files": [],
            },
        )

    client, _ = make(SonarrClient, handler)
    assert await client.path_visible(path) is expected


async def test_path_visible_rejects_root() -> None:
    client, rec = make(SonarrClient, lambda r: httpx.Response(200, json={"directories": []}))
    assert await client.path_visible("/") is False
    assert rec.requests == []


async def test_get_all_pages_passes_page_size() -> None:
    seen = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(dict(r.url.params))
        return httpx.Response(200, json={"totalRecords": 0, "records": []})

    client, _ = make(RadarrClient, handler)
    await client.wanted()
    assert seen[0]["pageSize"] == str(PAGE_SIZE)
    assert seen[0]["page"] == "1"
    assert json.loads(json.dumps(seen))  # params are plain strings
