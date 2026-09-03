from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from outriggarr.arr.base import EpisodeRef, SeriesRef
from outriggarr.db.models import Job
from outriggarr.source import VideoRef
from tests.fakes import FakeArrClient, FakeArrFactory, FakeVideoSource

SONARR = {
    "kind": "sonarr",
    "name": "Sonarr",
    "url": "http://sonarr-host:1234",
    "api_key": "k1",
    "staging_path_remote": "/data/outriggarr",
}
RADARR = {**SONARR, "kind": "radarr", "name": "Radarr", "url": "http://radarr-host:1234"}
NOW = datetime.now(UTC)


def seed(client: TestClient, arr: FakeArrFactory, source: FakeVideoSource) -> int:
    fake = FakeArrClient(
        series_list=[
            SeriesRef(5, "Show", 2020, 777, True),
            SeriesRef(6, "Other", None, None, True),
        ],
        episodes_by_series={
            5: [
                EpisodeRef(11, 1, 1, "One Long Title", False, True, NOW - timedelta(days=2)),
                EpisodeRef(12, 1, 2, "Two Long Title", False, True, NOW - timedelta(days=1)),
            ]
        },
    )
    arr.by_url["http://sonarr-host:1234"] = fake
    source.recent = [
        VideoRef("a", "One Long Title | Show", "https://y/a", 1, 1, None),
        VideoRef("b", "Bee", "https://y/b", 1, 2, None),
    ]
    return client.post("/api/connections", json=SONARR).json()["id"]


def body(conn_id: int, **over) -> dict:
    return {
        "connection_id": conn_id,
        "series_id": 5,
        "source_url": "https://www.youtube.com/@show",
        "auto_download": "all",
        **over,
    }


def test_create_fills_title_and_tvdb(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    r = client.post("/api/subscriptions", json=body(conn_id))
    assert r.status_code == 201, r.text
    sub = r.json()
    assert (sub["title"], sub["tvdb_id"]) == ("Show", 777)
    assert sub["strategies"] == ["title"] and sub["enabled"] is True
    assert sub["last_scan_at"] is None
    assert client.get("/api/subscriptions").json() == [sub]
    assert client.get(f"/api/subscriptions/{sub['id']}").json() == sub
    assert client.post("/api/subscriptions", json=body(conn_id)).status_code == 409


def test_validation(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    radarr = client.post("/api/connections", json=RADARR).json()["id"]
    assert (
        client.post("/api/subscriptions", json=body(conn_id, strategies=["magic"])).status_code
        == 422
    )
    assert (
        client.post(
            "/api/subscriptions", json=body(conn_id, title_regex="(?P<season>\\d+)")
        ).status_code
        == 422
    )
    assert client.post("/api/subscriptions", json=body(conn_id, title_regex="(")).status_code == 422
    assert (
        client.post(
            "/api/subscriptions", json=body(conn_id, source_url="youtube.com/@x")
        ).status_code
        == 422
    )
    assert client.post("/api/subscriptions", json=body(conn_id, series_id=999)).status_code == 422
    assert client.post("/api/subscriptions", json=body(radarr)).status_code == 422
    assert client.post("/api/subscriptions", json=body(999)).status_code == 404
    r = client.post(
        "/api/subscriptions", json=body(conn_id, strategies=["date", "title", "title"], format="  ")
    )
    assert r.status_code == 201
    assert r.json()["strategies"] == ["date", "title"] and r.json()["format"] is None


def test_update_and_delete_keep_jobs(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    sub = client.post("/api/subscriptions", json=body(conn_id)).json()
    r = client.put(
        f"/api/subscriptions/{sub['id']}",
        json=body(conn_id, series_id=6, strategies=[], enabled=False),
    )
    assert r.status_code == 200 and r.json()["title"] == "Other" and r.json()["enabled"] is False
    assert client.put("/api/subscriptions/999", json=body(conn_id)).status_code == 404

    # a scan creates a job linked to the subscription; deleting keeps the job
    client.put(f"/api/subscriptions/{sub['id']}", json=body(conn_id, strategies=["title"]))
    scan = client.post(f"/api/subscriptions/{sub['id']}/scan").json()
    assert scan["created_job_ids"], scan
    assert client.delete(f"/api/subscriptions/{sub['id']}").status_code == 204
    assert client.get(f"/api/subscriptions/{sub['id']}").status_code == 404
    with client.app.state.session_factory() as s:
        job = s.get(Job, scan["created_job_ids"][0])
        assert job is not None and job.subscription_id is None


def test_preview_scan_and_overrides(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    sub_id = client.post("/api/subscriptions", json=body(conn_id)).json()["id"]
    p = client.get(f"/api/subscriptions/{sub_id}/preview").json()
    assert p["dry_run"] is True
    assert [m["code"] for m in p["matches"]] == ["S01E01"]
    assert [u["code"] for u in p["unmatched"]] == ["S01E02"]
    assert p["unmatched"][0]["candidates"] == {"override": [], "title": []}
    assert client.get("/api/jobs").json() == []

    r = client.put(f"/api/subscriptions/{sub_id}/overrides/b", json={"season": 1, "episode": 2})
    assert r.status_code == 200 and r.json() == {
        "video_id": "b",
        "season": 1,
        "episode": 2,
        "video_url": None,
        "video_title": None,
    }
    assert (
        client.put(
            f"/api/subscriptions/{sub_id}/overrides/b", json={"season": 1, "episode": 2}
        ).status_code
        == 200
    )
    assert client.get(f"/api/subscriptions/{sub_id}/overrides").json() == [
        {"video_id": "b", "season": 1, "episode": 2, "video_url": None, "video_title": None}
    ]

    s = client.post(f"/api/subscriptions/{sub_id}/scan").json()
    assert s["dry_run"] is False
    assert {(m["code"], m["strategy"]) for m in s["matches"]} == {
        ("S01E01", "title"),
        ("S01E02", "override"),
    }
    assert len(s["created_job_ids"]) == 2
    jobs = client.get("/api/jobs").json()
    assert {j["target_label"] for j in jobs} == {
        "Show S01E01 - One Long Title",
        "Show S01E02 - Two Long Title",
    }
    assert {j["subscription_id"] for j in jobs} == {sub_id}
    assert client.get(f"/api/subscriptions/{sub_id}").json()["last_scan_result"]["created"] == 2

    assert client.delete(f"/api/subscriptions/{sub_id}/overrides/b").status_code == 204
    assert client.delete(f"/api/subscriptions/{sub_id}/overrides/b").status_code == 404
    assert client.get("/api/subscriptions/999/preview").status_code == 404
    assert client.post("/api/subscriptions/999/scan").status_code == 404


def test_sonarr_tag_applied_on_subscribe_and_removed_on_delete(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    fake = arr.by_url["http://sonarr-host:1234"]
    # off by default: no tag calls
    sub_id = client.post("/api/subscriptions", json=body(conn_id)).json()["id"]
    assert not [c for c in fake.calls if c[0] in ("ensure_tag", "set_series_tag")]
    client.delete(f"/api/subscriptions/{sub_id}")

    client.put("/api/settings", json={"sonarr_tag": "outriggarr"})
    sub_id = client.post("/api/subscriptions", json=body(conn_id)).json()["id"]
    assert fake.series_tags[5] == {100}
    assert ("set_series_tag", (5, 100, True)) in fake.calls
    assert client.delete(f"/api/subscriptions/{sub_id}").status_code == 204
    assert fake.series_tags[5] == set()

    # a tag failure never blocks the subscription
    async def boom(label):
        raise ArrError("POST tag -> HTTP 500: nope")

    fake.ensure_tag = boom
    r = client.post("/api/subscriptions", json=body(conn_id))
    assert r.status_code == 201


from outriggarr.arr.base import ArrError  # noqa: E402


def test_override_by_url(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    sub_id = client.post("/api/subscriptions", json=body(conn_id)).json()["id"]
    source.infos["https://y/old1?list=PLx"] = VideoRef(
        "old1", "Two Long Title (older upload)", "https://y/old1", 1, None, None
    )
    r = client.post(
        f"/api/subscriptions/{sub_id}/overrides",
        json={"url": "https://y/old1?list=PLx", "season": 1, "episode": 2},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "video_id": "old1",
        "season": 1,
        "episode": 2,
        "video_url": "https://y/old1",
        "video_title": "Two Long Title (older upload)",
    }
    assert source.fetched == ["https://y/old1?list=PLx"], "single-video fetch; playlist ignored"
    s = client.post(f"/api/subscriptions/{sub_id}/scan").json()
    assert {(m["code"], m["video_id"], m["strategy"]) for m in s["matches"]} == {
        ("S01E01", "a", "title"),
        ("S01E02", "old1", "override"),
    }
    r = client.post(
        f"/api/subscriptions/{sub_id}/overrides",
        json={"url": "https://y/bad", "season": 1, "episode": 3},
    )
    assert r.status_code == 502 and r.json()["detail"].endswith("Video unavailable")
    r = client.post(
        f"/api/subscriptions/{sub_id}/overrides",
        json={"url": "javascript:x", "season": 1, "episode": 3},
    )
    assert r.status_code == 422
    assert (
        client.post(
            "/api/subscriptions/999/overrides",
            json={"url": "https://y/x", "season": 1, "episode": 1},
        ).status_code
        == 404
    )


def test_video_limit_round_trip_and_range(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    assert client.post("/api/subscriptions", json=body(conn_id, video_limit=0)).status_code == 422
    assert (
        client.post("/api/subscriptions", json=body(conn_id, video_limit=5001)).status_code == 422
    )
    r = client.post("/api/subscriptions", json=body(conn_id, video_limit=1200))
    assert r.status_code < 300 and r.json()["video_limit"] == 1200
    assert client.get(f"/api/subscriptions/{r.json()['id']}").json()["video_limit"] == 1200


def test_sources_list_dedupes_validates_and_accepts_legacy_source_url(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    three = [
        "https://www.youtube.com/@show",
        " https://www.youtube.com/@show ",
        "https://www.youtube.com/@extra",
    ]
    r = client.post(
        "/api/subscriptions", json={"connection_id": conn_id, "series_id": 5, "sources": three}
    )
    assert r.status_code == 201, r.text
    assert r.json()["sources"] == [
        "https://www.youtube.com/@show",
        "https://www.youtube.com/@extra",
    ]
    assert "source_url" not in r.json()
    sid = r.json()["id"]
    for bad in (["https://a", "ftp://b"], [], [f"https://c/{i}" for i in range(11)]):
        r = client.put(
            f"/api/subscriptions/{sid}",
            json={"connection_id": conn_id, "series_id": 5, "sources": bad},
        )
        assert r.status_code == 422, bad
    # the pre-list JSON shape still works
    r = client.post(
        "/api/subscriptions",
        json={
            "connection_id": conn_id,
            "series_id": 6,
            "source_url": "https://www.youtube.com/@legacy",
        },
    )
    assert r.status_code == 201 and r.json()["sources"] == ["https://www.youtube.com/@legacy"]


def test_audio_language_override_round_trip_and_validation(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    r = client.post("/api/subscriptions", json=body(conn_id, audio_language=" JPN "))
    assert r.status_code == 201 and r.json()["audio_language"] == "jpn"
    sid = r.json()["id"]
    r = client.put(f"/api/subscriptions/{sid}", json=body(conn_id, audio_language="japanese"))
    assert r.status_code == 422
    r = client.put(f"/api/subscriptions/{sid}", json=body(conn_id, audio_language=""))
    assert r.status_code == 200 and r.json()["audio_language"] is None


def test_auto_download_default_validation_and_manual_download_endpoint(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    r = client.post(
        "/api/subscriptions",
        json={
            "connection_id": conn_id,
            "series_id": 5,
            "sources": ["https://www.youtube.com/@show"],
        },
    )
    assert r.status_code == 201 and r.json()["auto_download"] == "future", (
        "new subscriptions do not dump the backlog"
    )
    sid = r.json()["id"]
    assert (
        client.put(
            f"/api/subscriptions/{sid}", json=body(conn_id, auto_download="sometimes")
        ).status_code
        == 422
    )
    # the backlog episode is matched but not automatic; a manual download takes it
    scan = client.post(f"/api/subscriptions/{sid}/scan").json()
    assert (
        scan["created_job_ids"] == [] and scan["matches"][0]["skipped"] == "not automatic (future)"
    )
    picked = client.post(f"/api/subscriptions/{sid}/download", json={"episode_ids": [11]}).json()
    assert len(picked["created_job_ids"]) == 1


def test_title_require_round_trip_and_validation(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    r = client.post("/api/subscriptions", json=body(conn_id, title_require="  Scam School "))
    assert r.status_code == 201 and r.json()["title_require"] == "Scam School"
    sid = r.json()["id"]
    r = client.put(f"/api/subscriptions/{sid}", json=body(conn_id, title_require="x" * 101))
    assert r.status_code == 422
    r = client.put(f"/api/subscriptions/{sid}", json=body(conn_id, title_require="   "))
    assert r.status_code == 200 and r.json()["title_require"] is None
    r = client.post("/api/subscriptions", json={**body(conn_id), "series_id": 6})
    assert r.status_code == 201 and r.json()["title_require"] is None, "off by default"


def test_title_require_must_carry_a_letter_or_digit(client, arr, source) -> None:
    conn_id = seed(client, arr, source)
    r = client.post("/api/subscriptions", json=body(conn_id, title_require="!!!"))
    assert r.status_code == 422 and "letter or digit" in r.text
    r = client.post("/api/subscriptions", json=body(conn_id, title_require=" - Scam School - "))
    assert r.status_code == 201 and r.json()["title_require"] == "- Scam School -"
