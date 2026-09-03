from __future__ import annotations

from fastapi.testclient import TestClient

from outriggarr.arr.base import ArrError, SystemStatus
from outriggarr.db.models import Connection, Job, TargetKind
from tests.fakes import FakeArrClient, FakeArrFactory

SONARR = {
    "kind": "sonarr",
    "name": "Sonarr",
    "url": "http://sonarr-host:1234/",
    "api_key": "k1",
    "staging_path_remote": "/staging/",
}


def test_crud_roundtrip(client: TestClient) -> None:
    r = client.post("/api/connections", json=SONARR)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["id"] == 1
    assert created["url"] == "http://sonarr-host:1234"  # trailing slash stripped
    assert created["staging_path_remote"] == "/staging"
    assert created["enabled"] is True

    assert client.get("/api/connections").json() == [created]
    assert client.get("/api/connections/1").json() == created
    assert client.get("/api/connections/2").status_code == 404

    r = client.put("/api/connections/1", json={**SONARR, "name": "Renamed", "enabled": False})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"
    assert r.json()["enabled"] is False
    assert client.get("/api/connections/1").json()["name"] == "Renamed"

    assert client.delete("/api/connections/1").status_code == 204
    assert client.get("/api/connections").json() == []
    assert client.delete("/api/connections/1").status_code == 404


def test_validation(client: TestClient) -> None:
    assert client.post("/api/connections", json={**SONARR, "kind": "lidarr"}).status_code == 422
    assert client.post("/api/connections", json={**SONARR, "url": "sonarr:8989"}).status_code == 422
    assert client.post("/api/connections", json={**SONARR, "api_key": ""}).status_code == 422
    r = client.post("/api/connections", json={**SONARR, "staging_path_remote": "staging"})
    assert r.status_code == 422
    assert "absolute" in r.text


def test_delete_refused_while_jobs_reference_it(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    with client.app.state.session_factory() as s:
        conn = s.get(Connection, conn_id)
        s.add(
            Job(
                connection=conn,
                target_kind=TargetKind.episode,
                series_id=1,
                episode_ids=[1],
                target_key=Job.make_target_key(TargetKind.episode, series_id=1, episode_ids=[1]),
                video_id="v",
                video_url="https://example.invalid/v",
            )
        )
        s.commit()
    r = client.delete(f"/api/connections/{conn_id}")
    assert r.status_code == 409
    assert "1 job" in r.json()["detail"]
    assert client.get(f"/api/connections/{conn_id}").status_code == 200


def test_test_ok(client: TestClient, arr: FakeArrFactory) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    arr.by_url["http://sonarr-host:1234"] = FakeArrClient(
        status_result=SystemStatus("Sonarr", "4.0.9"), visible_paths={"/staging"}
    )
    r = client.post(f"/api/connections/{conn_id}/test")
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "app_name": "Sonarr",
        "version": "4.0.9",
        "staging_visible": True,
        "error": None,
        "warning": None,
    }
    fake = arr.by_url["http://sonarr-host:1234"]
    assert fake.calls == [
        ("status", None),
        ("path_visible", "/staging"),
        ("extra_files_config", None),  # subtitles are on by default → srt check
    ]
    assert arr.made[0].api_key == "k1"


def test_test_status_failure_is_verbatim(client: TestClient, arr: FakeArrFactory) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    arr.by_url["http://sonarr-host:1234"] = FakeArrClient(
        status_result=ArrError("GET http://sonarr-host:1234/api/v3/system/status -> HTTP 401: nope")
    )
    body = client.post(f"/api/connections/{conn_id}/test").json()
    assert body["ok"] is False
    assert body["error"] == "GET http://sonarr-host:1234/api/v3/system/status -> HTTP 401: nope"
    assert body["staging_visible"] is None


def test_test_staging_not_visible(client: TestClient, arr: FakeArrFactory) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    arr.by_url["http://sonarr-host:1234"] = FakeArrClient(visible_paths=set())
    body = client.post(f"/api/connections/{conn_id}/test").json()
    assert body["ok"] is False
    assert body["staging_visible"] is False
    assert body["app_name"] == "Sonarr"
    assert "/staging" in body["error"]


def test_test_filesystem_error_is_verbatim(client: TestClient, arr: FakeArrFactory) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    arr.by_url["http://sonarr-host:1234"] = FakeArrClient(path_error=ArrError("HTTP 500: boom"))
    body = client.post(f"/api/connections/{conn_id}/test").json()
    assert body["ok"] is False
    assert body["error"] == "HTTP 500: boom"
    assert body["version"] == "0.0.0"


def test_test_detects_kind_mismatch(client: TestClient, arr: FakeArrFactory) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    arr.by_url["http://sonarr-host:1234"] = FakeArrClient(
        status_result=SystemStatus("Radarr", "5.0")
    )
    body = client.post(f"/api/connections/{conn_id}/test").json()
    assert body["ok"] is False
    assert "sonarr" in body["error"] and "Radarr" in body["error"]
    assert arr.by_url["http://sonarr-host:1234"].calls == [("status", None)]


def test_test_unknown_connection(client: TestClient) -> None:
    assert client.post("/api/connections/99/test").status_code == 404


def test_test_warns_when_arr_will_not_import_srt(client: TestClient, arr: FakeArrFactory) -> None:
    from outriggarr.arr.base import ExtraFilesConfig

    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    fake = arr.by_url["http://sonarr-host:1234"] = FakeArrClient(visible_paths={"/staging"})
    assert client.post(f"/api/connections/{conn_id}/test").json()["warning"] is None
    fake.extra_files = ExtraFilesConfig(True, ("nfo",))
    body = client.post(f"/api/connections/{conn_id}/test").json()
    assert body["ok"] is True and "Import Extra Files" in body["warning"]
    fake.extra_files = ExtraFilesConfig(False, ("srt",))
    assert "Import Extra Files" in client.post(f"/api/connections/{conn_id}/test").json()["warning"]
    # subtitles off → no check at all
    client.put("/api/settings", json={"subtitles_langs": ""})
    body = client.post(f"/api/connections/{conn_id}/test").json()
    assert body["warning"] is None
    assert ("extra_files_config", None) not in fake.calls[-2:]


def test_api_key_is_stripped_and_blank_refused(client: TestClient) -> None:
    from outriggarr.db.models import Connection

    r = client.post("/api/connections", json={**SONARR, "api_key": "  k1  "})
    assert r.status_code == 201 and "api_key" not in r.json() and r.json()["has_api_key"] is True
    conn_id = r.json()["id"]
    with client.app.state.session_factory() as s:
        assert s.get(Connection, conn_id).api_key == "k1", "stripped, stored"
    # the secret never comes back out
    assert "k1" not in client.get("/api/connections").text
    assert "k1" not in client.get(f"/api/connections/{conn_id}").text
    # an update with a blank key keeps the stored one; a client need never hold the secret
    r = client.put(f"/api/connections/{conn_id}", json={**SONARR, "name": "Renamed", "api_key": ""})
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and "api_key" not in r.json()
    with client.app.state.session_factory() as s:
        assert s.get(Connection, conn_id).api_key == "k1"
    r = client.put(
        f"/api/connections/{conn_id}", json={**SONARR, "name": "Renamed", "api_key": "k2"}
    )
    with client.app.state.session_factory() as s:
        assert s.get(Connection, conn_id).api_key == "k2", "a new key replaces it"
    assert (
        client.post(
            "/api/connections", json={**SONARR, "url": "http://x:1", "api_key": "   "}
        ).status_code
        == 422
    )


def test_kind_change_and_delete_refused_while_subscriptions_reference(
    client: TestClient, arr: FakeArrFactory
) -> None:
    from outriggarr.arr.base import SeriesRef

    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    arr.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", None, None, True)]
    )
    assert (
        client.post(
            "/api/subscriptions",
            json={
                "connection_id": conn_id,
                "series_id": 5,
                "source_url": "https://www.youtube.com/@x",
            },
        ).status_code
        == 201
    )
    r = client.put(f"/api/connections/{conn_id}", json={**SONARR, "kind": "radarr"})
    assert r.status_code == 409 and "kind cannot change" in r.json()["detail"]
    r = client.put(f"/api/connections/{conn_id}", json={**SONARR, "name": "Renamed"})
    assert r.status_code == 200
    r = client.delete(f"/api/connections/{conn_id}")
    assert r.status_code == 409 and "1 subscription(s)" in r.json()["detail"]
    # the web route reports it too, without a 500
    r = client.post(f"/settings/connections/{conn_id}/delete")
    assert r.status_code == 400 and "subscription(s)" in r.text
    assert client.get("/settings").status_code == 200
