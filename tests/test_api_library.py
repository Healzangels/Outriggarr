from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from outriggarr.api.library import TTLCache, library_cache
from outriggarr.arr.base import EpisodeRef, MovieRef, SeriesRef
from outriggarr.db.models import ConnectionKind
from outriggarr.source import SourceError, VideoRef
from tests.fakes import FakeArrClient, FakeArrFactory, FakeVideoSource

SONARR = {
    "kind": "sonarr",
    "name": "Sonarr",
    "url": "http://sonarr-host:1234",
    "api_key": "k1",
    "staging_path_remote": "/data/outriggarr",
}
RADARR = {**SONARR, "kind": "radarr", "name": "Radarr", "url": "http://radarr-host:1234"}


def test_resolve_returns_videos(client: TestClient, source: FakeVideoSource) -> None:
    source.videos = [VideoRef("a", "A", "https://x/a", 10, 1, "20240101")]
    r = client.post("/api/resolve", json={"url": "https://x/playlist"})
    assert r.status_code == 200
    assert r.json() == [
        {
            "id": "a",
            "title": "A",
            "url": "https://x/a",
            "duration": 10,
            "playlist_index": 1,
            "upload_date": "20240101",
        }
    ]
    assert source.resolved == ["https://x/playlist"]


def test_resolve_error_is_502_verbatim(client: TestClient, source: FakeVideoSource) -> None:
    source.resolve_error = SourceError("ERROR: [youtube] nope: Video unavailable")
    r = client.post("/api/resolve", json={"url": "https://x/bad"})
    assert r.status_code == 502
    assert r.json()["detail"] == "ERROR: [youtube] nope: Video unavailable"
    assert client.post("/api/resolve", json={"url": ""}).status_code == 422


def _seed(arr: FakeArrFactory) -> FakeArrClient:
    fake = FakeArrClient(
        series_list=[
            SeriesRef(1, "Hot Ones", 2015, 327172, True),
            SeriesRef(2, "Monstrum", 2019, 363839, True),
            SeriesRef(3, "The Hot Zone", 2019, 1, False),
        ],
        episodes_by_series={
            1: [
                EpisodeRef(11, 1, 2, "Two", False, True, datetime(2024, 1, 2, tzinfo=UTC)),
                EpisodeRef(10, 1, 1, "One", True, True, None),
            ]
        },
    )
    arr.by_url["http://sonarr-host:1234"] = fake
    return fake


def test_series_search_filters_ranks_and_caches(client: TestClient, arr: FakeArrFactory) -> None:
    fake = _seed(arr)
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    r = client.get(f"/api/connections/{conn_id}/series?q=hot")
    assert r.status_code == 200
    assert [s["title"] for s in r.json()] == ["Hot Ones", "The Hot Zone"]  # prefix match first
    assert r.json()[0] == {
        "id": 1,
        "title": "Hot Ones",
        "year": 2015,
        "tvdb_id": 327172,
        "monitored": True,
        "episode_count": None,
        "episode_file_count": None,
    }
    assert [s["title"] for s in client.get(f"/api/connections/{conn_id}/series").json()] == [
        "Hot Ones",
        "Monstrum",
        "The Hot Zone",
    ]
    assert len(client.get(f"/api/connections/{conn_id}/series?limit=1").json()) == 1
    assert client.get(f"/api/connections/{conn_id}/series?limit=0").status_code == 422
    assert fake.library_loads == 1, "the listing is cached across searches"


def test_series_cache_expires(client: TestClient, arr: FakeArrFactory, monkeypatch) -> None:
    fake = _seed(arr)
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    t = [0.0]
    monkeypatch.setattr(library_cache, "now", lambda: t[0])
    client.get(f"/api/connections/{conn_id}/series")
    t[0] = 59.0
    client.get(f"/api/connections/{conn_id}/series")
    assert fake.library_loads == 1
    t[0] = 61.0
    client.get(f"/api/connections/{conn_id}/series")
    assert fake.library_loads == 2


def test_episodes(client: TestClient, arr: FakeArrFactory) -> None:
    _seed(arr)
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    r = client.get(f"/api/connections/{conn_id}/series/1/episodes")
    assert r.status_code == 200
    assert r.json()[0] == {
        "id": 11,
        "season_number": 1,
        "episode_number": 2,
        "title": "Two",
        "has_file": False,
        "monitored": True,
        "air_date_utc": "2024-01-02T00:00:00Z",
    }
    assert client.get(f"/api/connections/{conn_id}/series/999/episodes").json() == []


def test_movies_search(client: TestClient, arr: FakeArrFactory) -> None:
    arr.by_url["http://radarr-host:1234"] = FakeArrClient(
        kind=ConnectionKind.radarr,
        movies_list=[
            MovieRef(7, "Big Buck Bunny", 2008, 10378, False, True),
            MovieRef(8, "Bunny Lake Is Missing", 1965, 2, True, True),
        ],
    )
    conn_id = client.post("/api/connections", json=RADARR).json()["id"]
    r = client.get(f"/api/connections/{conn_id}/movies?q=bunny")
    assert [m["title"] for m in r.json()] == ["Bunny Lake Is Missing", "Big Buck Bunny"]
    assert r.json()[1]["tmdb_id"] == 10378


def test_kind_mismatch_and_unknown(client: TestClient, arr: FakeArrFactory) -> None:
    _seed(arr)
    s = client.post("/api/connections", json=SONARR).json()["id"]
    m = client.post("/api/connections", json=RADARR).json()["id"]
    assert client.get(f"/api/connections/{m}/series").status_code == 422
    assert client.get(f"/api/connections/{s}/movies").status_code == 422
    assert client.get(f"/api/connections/{m}/series/1/episodes").status_code == 422
    assert client.get("/api/connections/99/series").status_code == 404


def test_arr_error_is_502(client: TestClient, arr: FakeArrFactory) -> None:
    from outriggarr.arr.base import ArrError

    fake = _seed(arr)
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]

    async def boom():
        raise ArrError("GET http://sonarr-host:1234/api/v3/series -> HTTP 500: kaboom")

    fake.series = boom
    r = client.get(f"/api/connections/{conn_id}/series")
    assert r.status_code == 502
    assert r.json()["detail"].endswith("HTTP 500: kaboom")


def test_ttlcache_unit() -> None:
    import asyncio

    t = [0.0]
    c = TTLCache(10, now=lambda: t[0])
    n = [0]

    async def load():
        n[0] += 1
        return n[0]

    assert asyncio.run(c.get("k", load)) == 1
    t[0] = 9
    assert asyncio.run(c.get("k", load)) == 1
    t[0] = 10
    assert asyncio.run(c.get("k", load)) == 2
    c.clear()
    assert asyncio.run(c.get("k", load)) == 3
