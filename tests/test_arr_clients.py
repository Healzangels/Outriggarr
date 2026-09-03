"""Sonarr/Radarr clients against an httpx MockTransport. No network."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from outriggarr.arr.base import ArrError, CommandStatus
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


# ---- M2: manual import ------------------------------------------------------------

from outriggarr.arr.base import ImportFile, Language, Target  # noqa: E402

QUALITY_DEFS = [
    {"id": 10, "quality": {"id": 3, "name": "WEBDL-1080p"}, "title": "WEB 1080p", "weight": 9},
    {"id": 11, "quality": {"id": 5, "name": "WEBDL-720p"}, "title": "WEB 720p", "weight": 7},
]


async def test_sonarr_manual_import_candidates_folder_only_and_parse() -> None:
    body = [
        {
            "id": 1,
            "path": "/data/outriggarr/7/Show - S01E02 [WEBDL-1080p].mkv",
            "relativePath": "Show - S01E02 [WEBDL-1080p].mkv",
            "name": "Show - S01E02 [WEBDL-1080p]",
            "size": 12345,
            "rejections": [{"reason": "Not an upgrade", "type": "permanent"}],
            "languages": [{"id": 0, "name": "Unknown"}, {"id": 1, "name": "English"}],
        }
    ]

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/base/api/v3/manualimport"
        assert r.url.params["folder"] == "/data/outriggarr/7"
        assert r.url.params["filterExistingFiles"] == "true"
        # seriesId would make Sonarr list the series folder instead of ours (seen live)
        assert "seriesId" not in r.url.params
        assert "movieId" not in r.url.params
        assert set(r.url.params.keys()) == {"folder", "filterExistingFiles"}
        return httpx.Response(200, json=body)

    client, _ = make(SonarrClient, handler)
    (c,) = await client.manual_import_candidates("/data/outriggarr/7")
    assert c.path == "/data/outriggarr/7/Show - S01E02 [WEBDL-1080p].mkv"
    assert c.relative_path == "Show - S01E02 [WEBDL-1080p].mkv"
    assert c.size == 12345
    assert c.rejections == ("Not an upgrade",)
    assert c.languages == (Language(0, "Unknown"), Language(1, "English"))


async def test_radarr_manual_import_candidates_folder_only() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        assert set(r.url.params.keys()) == {"folder", "filterExistingFiles"}
        assert r.url.params["folder"] == "/f"
        return httpx.Response(200, json=[])

    client, _ = make(RadarrClient, handler)
    assert await client.manual_import_candidates("/f") == []


async def test_sonarr_manual_import_posts_command_with_explicit_ids() -> None:
    posted: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/qualitydefinition"):
            return httpx.Response(200, json=QUALITY_DEFS)
        assert r.method == "POST"
        assert r.url.path == "/base/api/v3/command"
        assert r.headers["X-Api-Key"] == KEY
        posted.append(json.loads(r.content))
        return httpx.Response(201, json={"id": 555, "name": "ManualImport", "status": "queued"})

    client, _ = make(SonarrClient, handler)
    cmd = await client.manual_import(
        [
            ImportFile(
                path="/data/outriggarr/7/x.mkv",
                quality_name="WEBDL-1080p",
                languages=(Language(1, "English"),),
                target=Target(series_id=5, episode_ids=(42, 43)),
            )
        ]
    )
    assert cmd == 555
    assert posted == [
        {
            "name": "ManualImport",
            "importMode": "move",
            "files": [
                {
                    "path": "/data/outriggarr/7/x.mkv",
                    "quality": {
                        "quality": {"id": 3, "name": "WEBDL-1080p"},
                        "revision": {"version": 1, "real": 0, "isRepack": False},
                    },
                    "languages": [{"id": 1, "name": "English"}],
                    "seriesId": 5,
                    "episodeIds": [42, 43],
                }
            ],
        }
    ]


async def test_radarr_manual_import_uses_movie_id() -> None:
    posted: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/qualitydefinition"):
            return httpx.Response(200, json=QUALITY_DEFS)
        posted.append(json.loads(r.content))
        return httpx.Response(201, json={"id": 9})

    client, _ = make(RadarrClient, handler)
    await client.manual_import(
        [ImportFile("/p.mkv", "WEBDL-720p", (Language(1, "English"),), Target(movie_id=77))]
    )
    f = posted[0]["files"][0]
    assert f["movieId"] == 77
    assert "seriesId" not in f and "episodeIds" not in f
    assert f["quality"]["quality"] == {"id": 5, "name": "WEBDL-720p"}


async def test_manual_import_unknown_quality_is_an_error_before_posting() -> None:
    posts = 0

    def handler(r: httpx.Request) -> httpx.Response:
        nonlocal posts
        if r.method == "POST":
            posts += 1
        return httpx.Response(200, json=QUALITY_DEFS)

    client, _ = make(SonarrClient, handler)
    with pytest.raises(ArrError, match="WEBDL-2160p.*not defined"):
        await client.manual_import(
            [ImportFile("/p", "WEBDL-2160p", (), Target(series_id=1, episode_ids=(1,)))]
        )
    assert posts == 0


async def test_command_status_parse() -> None:
    client, rec = make(
        SonarrClient,
        lambda r: httpx.Response(
            200, json={"id": 555, "name": "ManualImport", "status": "completed", "message": "ok"}
        ),
    )
    st = await client.command(555)
    assert str(rec.requests[0].url) == f"{BASE}/api/v3/command/555"
    assert (st.id, st.status, st.message, st.finished, st.ok) == (
        555,
        "completed",
        "ok",
        True,
        True,
    )
    for s in ("queued", "started"):
        assert not CommandStatus(1, "x", s).finished
    for s in ("failed", "aborted", "cancelled", "orphaned"):
        assert CommandStatus(1, "x", s).finished and not CommandStatus(1, "x", s).ok


async def test_sonarr_target_info_from_episode_endpoint() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/base/api/v3/episode/42":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "seriesId": 5,
                    "seasonNumber": 2,
                    "episodeNumber": 4,
                    "title": "Four",
                    "hasFile": False,
                    "monitored": True,
                    "series": {"id": 5, "title": "Show"},
                },
            )
        if r.url.path == "/base/api/v3/episode/43":
            return httpx.Response(
                200,
                json={
                    "id": 43,
                    "seriesId": 5,
                    "seasonNumber": 2,
                    "episodeNumber": 3,
                    "title": "Three",
                    "hasFile": True,
                    "monitored": True,
                },
            )
        raise AssertionError(r.url)

    client, _ = make(SonarrClient, handler)
    info = await client.target_info(Target(series_id=5, episode_ids=(42, 43)))
    assert info.title == "Show"
    assert info.season == 2
    assert info.episode_numbers == (3, 4)
    assert info.episode_title == "Three + Four"
    assert info.has_file is False  # not ALL have a file
    assert info.monitored is True


async def test_sonarr_target_info_falls_back_to_series_endpoint() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/base/api/v3/episode/42":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "seriesId": 5,
                    "seasonNumber": 1,
                    "episodeNumber": 1,
                    "title": "",
                    "hasFile": True,
                    "monitored": False,
                },
            )
        if r.url.path == "/base/api/v3/series/5":
            return httpx.Response(200, json={"id": 5, "title": "From Series"})
        raise AssertionError(r.url)

    client, _ = make(SonarrClient, handler)
    info = await client.target_info(Target(series_id=5, episode_ids=(42,)))
    assert info.title == "From Series"
    assert info.has_file is True and info.monitored is False


async def test_sonarr_target_info_rejects_foreign_episode_and_mixed_seasons() -> None:
    eps = {
        "1": {
            "id": 1,
            "seriesId": 5,
            "seasonNumber": 1,
            "episodeNumber": 1,
            "series": {"title": "S"},
        },
        "2": {
            "id": 2,
            "seriesId": 6,
            "seasonNumber": 1,
            "episodeNumber": 2,
            "series": {"title": "S"},
        },
        "3": {
            "id": 3,
            "seriesId": 5,
            "seasonNumber": 2,
            "episodeNumber": 1,
            "series": {"title": "S"},
        },
    }
    client, _ = make(
        SonarrClient, lambda r: httpx.Response(200, json=eps[r.url.path.rsplit("/", 1)[1]])
    )
    with pytest.raises(ArrError, match="do not belong to series 5"):
        await client.target_info(Target(series_id=5, episode_ids=(1, 2)))
    with pytest.raises(ArrError, match="several seasons"):
        await client.target_info(Target(series_id=5, episode_ids=(1, 3)))
    with pytest.raises(ArrError, match="cannot import a movie"):
        await client.target_info(Target(movie_id=1))


async def test_radarr_target_info() -> None:
    client, rec = make(
        RadarrClient,
        lambda r: httpx.Response(
            200, json={"id": 77, "title": "Film", "year": 2001, "hasFile": True, "monitored": True}
        ),
    )
    info = await client.target_info(Target(movie_id=77))
    assert str(rec.requests[0].url) == f"{BASE}/api/v3/movie/77"
    assert (info.title, info.year, info.has_file, info.season) == ("Film", 2001, True, None)
    with pytest.raises(ArrError, match="cannot import an episode"):
        await client.target_info(Target(series_id=1, episode_ids=(1,)))


def test_languages_for_import_defaults_to_english() -> None:
    from outriggarr.arr.base import ImportCandidate, languages_for_import

    unknown = ImportCandidate(1, "/p", "p", "p", 1, (), (Language(0, "Unknown"),))
    assert languages_for_import(unknown) == (Language(1, "English"),)
    none = ImportCandidate(1, "/p", "p", "p", 1, (), ())
    assert languages_for_import(none) == (Language(1, "English"),)
    known = ImportCandidate(
        1, "/p", "p", "p", 1, (), (Language(0, "Unknown"), Language(4, "French"))
    )
    assert languages_for_import(known) == (Language(4, "French"),)


def test_target_validation() -> None:
    with pytest.raises(ValueError):
        Target()
    with pytest.raises(ValueError):
        Target(series_id=1)
    with pytest.raises(ValueError):
        Target(movie_id=1, series_id=2)
    assert Target(movie_id=1).is_movie
    assert not Target(series_id=1, episode_ids=(2,)).is_movie


# ---- M3: library lookups ----------------------------------------------------------


async def test_sonarr_series_and_episodes_parse() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/base/api/v3/series":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 5,
                        "title": "Show",
                        "year": 2020,
                        "tvdbId": 77,
                        "monitored": True,
                        "statistics": {"episodeCount": 13, "episodeFileCount": 12},
                    },
                    {"id": 6, "title": "Bare"},
                ],
            )
        if r.url.path == "/base/api/v3/episode":
            assert r.url.params["seriesId"] == "5"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 2,
                        "seasonNumber": 1,
                        "episodeNumber": 2,
                        "title": "B",
                        "hasFile": True,
                        "monitored": False,
                        "airDateUtc": "2024-01-02T00:00:00Z",
                        "runtime": 24,
                    },
                    {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "runtime": 0},
                ],
            )
        raise AssertionError(r.url)

    from outriggarr.arr.base import EpisodeRef, SeriesRef

    client, _ = make(SonarrClient, handler)
    assert await client.series() == [
        SeriesRef(5, "Show", 2020, 77, True, 13, 12),
        SeriesRef(6, "Bare", None, None, False),
    ]
    eps = await client.episodes(5)
    assert eps == [
        EpisodeRef(1, 1, 1, "", False, False, None),
        EpisodeRef(2, 1, 2, "B", True, False, datetime(2024, 1, 2, tzinfo=UTC), runtime=24),
    ]
    with pytest.raises(ArrError, match="no movies"):
        await client.movies()


async def test_radarr_movies_parse() -> None:
    from outriggarr.arr.base import MovieRef

    client, rec = make(
        RadarrClient,
        lambda r: httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "title": "Film",
                    "year": 2001,
                    "tmdbId": 9,
                    "hasFile": False,
                    "monitored": True,
                },
                {"id": 8, "title": "Bare"},
            ],
        ),
    )
    assert await client.movies() == [
        MovieRef(7, "Film", 2001, 9, False, True),
        MovieRef(8, "Bare", None, None, False, False),
    ]
    assert str(rec.requests[0].url) == f"{BASE}/api/v3/movie"
    with pytest.raises(ArrError, match="no series"):
        await client.series()
    with pytest.raises(ArrError, match="no episodes"):
        await client.episodes(1)


# ---- M5: tags ---------------------------------------------------------------------


async def test_sonarr_ensure_tag_and_set_series_tag() -> None:
    posted: list[tuple[str, dict]] = []
    series = {"id": 5, "title": "Show", "tags": [1], "path": "/data/media/tv/Show", "seasons": []}

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/base/api/v3/tag" and r.method == "GET":
            return httpx.Response(
                200, json=[{"id": 1, "label": "anime"}, {"id": 7, "label": "Outriggarr"}]
            )
        if r.url.path == "/base/api/v3/tag" and r.method == "POST":
            posted.append(("tag", json.loads(r.content)))
            return httpx.Response(201, json={"id": 9, "label": "new"})
        if r.url.path == "/base/api/v3/series/5" and r.method == "GET":
            return httpx.Response(200, json=series)
        if r.url.path == "/base/api/v3/series/5" and r.method == "PUT":
            posted.append(("series", json.loads(r.content)))
            return httpx.Response(202, json={})
        raise AssertionError((r.method, r.url))

    client, _ = make(SonarrClient, handler)
    assert await client.ensure_tag("outriggarr") == 7  # case-insensitive hit, no POST
    assert await client.ensure_tag("new") == 9
    assert posted[-1] == ("tag", {"label": "new"})

    await client.set_series_tag(5, 7, True)
    body = posted[-1][1]
    assert body["tags"] == [1, 7] and body["path"] == "/data/media/tv/Show", "full resource kept"
    n = len(posted)
    await client.set_series_tag(5, 1, True)  # already present → no PUT
    assert len(posted) == n
    await client.set_series_tag(5, 1, False)
    assert posted[-1][1]["tags"] == []
    n = len(posted)
    await client.set_series_tag(5, 42, False)  # absent → no PUT
    assert len(posted) == n

    radarr, _ = make(RadarrClient, handler)
    with pytest.raises(ArrError, match="no series"):
        await radarr.set_series_tag(5, 7, True)


async def test_extra_files_config_parse() -> None:
    from outriggarr.arr.base import ExtraFilesConfig

    client, rec = make(
        SonarrClient,
        lambda r: httpx.Response(
            200, json={"importExtraFiles": True, "extraFileExtensions": "srt, Sub,.nfo"}
        ),
    )
    cfg = await client.extra_files_config()
    assert str(rec.requests[0].url) == f"{BASE}/api/v3/config/mediamanagement"
    assert cfg == ExtraFilesConfig(True, ("srt", "sub", "nfo"))
    assert cfg.imports("SRT") and cfg.imports(".nfo") and not cfg.imports("ass")
    assert not ExtraFilesConfig(False, ("srt",)).imports("srt")


async def test_arr_error_retryable_classification() -> None:
    client, _ = make(SonarrClient, lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(ArrError) as ei:
        await client.status()
    assert ei.value.retryable is False
    client, _ = make(SonarrClient, lambda r: httpx.Response(503, text="busy"))
    with pytest.raises(ArrError) as ei:
        await client.status()
    assert ei.value.retryable is True

    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=r)

    client, _ = make(SonarrClient, boom)
    with pytest.raises(ArrError) as ei:
        await client.status()
    assert ei.value.retryable is True


async def test_sonarr_reprocess_posts_ids_and_returns_remaining_rejections() -> None:
    from outriggarr.arr.base import ImportCandidate, Language, Target

    posted: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/qualitydefinition"):
            return httpx.Response(200, json=QUALITY_DEFS)
        assert r.method == "POST" and r.url.path == "/base/api/v3/manualimport"
        posted.append(json.loads(r.content)[0])
        return httpx.Response(
            202, json=[{"id": 7, "rejections": [{"reason": "Not an upgrade", "type": "permanent"}]}]
        )

    client, _ = make(SonarrClient, handler)
    cand = ImportCandidate(7, "/data/outriggarr/1/x.mkv", "x.mkv", "x", 1, ("Unknown Series",), ())
    out = await client.reprocess(
        cand, Target(series_id=5, episode_ids=(42,)), "WEBDL-1080p", (Language(1, "English"),), 30
    )
    assert out == ("Not an upgrade",)
    body = posted[0]
    assert body["id"] == 7 and body["path"] == "/data/outriggarr/1/x.mkv"
    assert body["seriesId"] == 5 and body["episodeIds"] == [42] and body["seasonNumber"] == 30
    assert body["quality"]["quality"] == {"id": 3, "name": "WEBDL-1080p"} and body["languages"] == [
        {"id": 1, "name": "English"}
    ]

    radarr, _ = make(RadarrClient, handler)
    posted.clear()
    await radarr.reprocess(cand, Target(movie_id=77), "WEBDL-720p", (), None)
    assert posted[0]["movieId"] == 77 and "seasonNumber" not in posted[0]


async def test_redirects_and_non_json_are_not_retryable() -> None:
    client, _ = make(
        SonarrClient, lambda r: httpx.Response(301, headers={"location": "https://x/"})
    )
    with pytest.raises(ArrError) as ei:
        await client.status()
    assert ei.value.retryable is False and "redirect" in str(ei.value)
    client, _ = make(SonarrClient, lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(ArrError) as ei:
        await client.status()
    assert ei.value.retryable is False


async def test_sonarr_series_title_and_partial_satisfaction() -> None:
    from outriggarr.arr.base import Target

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/base/api/v3/series/5":
            return httpx.Response(200, json={"id": 5, "title": "Renamed Show"})
        if r.url.path == "/base/api/v3/series/9":
            return httpx.Response(404, json={"message": "NotFound"})
        eid = r.url.path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            json={
                "id": int(eid),
                "seriesId": 5,
                "seasonNumber": 1,
                "episodeNumber": int(eid),
                "hasFile": eid == "1",
                "monitored": True,
                "series": {"title": "S"},
            },
        )

    client, _ = make(SonarrClient, handler)
    assert await client.series_title(5) == "Renamed Show"
    with pytest.raises(ArrError) as ei:
        await client.series_title(9)
    assert ei.value.retryable is False
    info = await client.target_info(Target(series_id=5, episode_ids=(1, 2)))
    assert info.has_file is False and info.partially_satisfied is True
    info = await client.target_info(Target(series_id=5, episode_ids=(2, 3)))
    assert info.partially_satisfied is False


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(408, True), (425, True), (429, True), (404, False), (400, False), (503, True)],
)
async def test_come_back_later_answers_are_retryable(status: int, retryable: bool) -> None:
    from outriggarr.arr.base import ArrError

    client, _rec = make(SonarrClient, lambda r: httpx.Response(status, json={"message": "later"}))
    with pytest.raises(ArrError) as info:
        await client.status()
    assert info.value.retryable is retryable


async def test_manual_import_listing_gets_a_long_read_timeout() -> None:
    from outriggarr.arr.common import MANUAL_IMPORT_LIST_TIMEOUT

    client, rec = make(SonarrClient, lambda r: httpx.Response(200, json=[]))
    await client.manual_import_candidates("/data/outriggarr/1")
    timeout = rec.requests[-1].extensions["timeout"]
    assert timeout["read"] == MANUAL_IMPORT_LIST_TIMEOUT == 180.0 and timeout["connect"] == 10.0
