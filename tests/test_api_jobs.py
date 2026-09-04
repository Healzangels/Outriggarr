from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

SONARR = {
    "kind": "sonarr",
    "name": "Sonarr",
    "url": "http://sonarr-host:1234",
    "api_key": "k1",
    "staging_path_remote": "/data/outriggarr",
}
RADARR = {**SONARR, "kind": "radarr", "name": "Radarr", "url": "http://radarr-host:1234"}


def episode_job(conn_id: int, video_id: str = "abc") -> dict:
    return {
        "connection_id": conn_id,
        "target": {"kind": "episode", "series_id": 5, "episode_ids": [42, 41]},
        "video": {
            "url": f"https://youtube.invalid/watch?v={video_id}",
            "id": video_id,
            "title": "T",
        },
    }


def test_create_and_read(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    r = client.post("/api/jobs", json=[episode_job(conn_id)])
    assert r.status_code == 201, r.text
    (job,) = r.json()
    assert job["status"] == "queued"
    assert job["target_kind"] == "episode"
    assert job["episode_ids"] == [42, 41]
    assert job["attempts"] == 0 and job["progress_pct"] == 0
    assert job["video_id"] == "abc"
    assert job["subscription_id"] is None and job["format"] is None  # manual grab
    assert client.get(f"/api/jobs/{job['id']}").json() == job
    assert client.get("/api/jobs").json() == [job]
    assert client.get("/api/jobs?status=queued").json() == [job]
    assert client.get("/api/jobs?status=done").json() == []
    assert client.get("/api/jobs?status=bogus").status_code == 422
    assert client.get("/api/jobs/999").status_code == 404


def test_duplicate_is_409_with_existing_id(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    first = client.post("/api/jobs", json=[episode_job(conn_id)]).json()[0]
    # same target (ids in another order) + same video → duplicate
    dup = episode_job(conn_id)
    dup["target"]["episode_ids"] = [41, 42]
    r = client.post("/api/jobs", json=[dup])
    assert r.status_code == 409
    assert f"'existing_job_id': {first['id']}" in r.json()["detail"]
    assert len(client.get("/api/jobs").json()) == 1

    # a batch with one dup creates nothing
    r = client.post("/api/jobs", json=[episode_job(conn_id, "new"), dup])
    assert r.status_code == 409
    assert len(client.get("/api/jobs").json()) == 1

    # same video for a different target is fine
    other = episode_job(conn_id)
    other["target"]["episode_ids"] = [43]
    assert client.post("/api/jobs", json=[other]).status_code == 201


def test_batch_with_internal_duplicate_is_409(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    r = client.post("/api/jobs", json=[episode_job(conn_id), episode_job(conn_id)])
    assert r.status_code == 409
    assert client.get("/api/jobs").json() == []


def test_validation(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    radarr_id = client.post("/api/connections", json=RADARR).json()["id"]
    bad = episode_job(conn_id)
    bad["target"] = {"kind": "episode", "series_id": 5}
    assert client.post("/api/jobs", json=[bad]).status_code == 422
    bad["target"] = {"kind": "movie"}
    assert client.post("/api/jobs", json=[bad]).status_code == 422
    bad["target"] = {"kind": "movie", "movie_id": 1, "series_id": 2}
    assert client.post("/api/jobs", json=[bad]).status_code == 422
    assert client.post("/api/jobs", json=[]).status_code == 422
    assert client.post("/api/jobs", json=[episode_job(999)]).status_code == 404
    # kind must match the connection
    wrong = episode_job(radarr_id)
    r = client.post("/api/jobs", json=[wrong])
    assert r.status_code == 422
    assert "radarr connection takes movie targets" in r.json()["detail"]
    movie = {
        "connection_id": radarr_id,
        "target": {"kind": "movie", "movie_id": 7},
        "video": {"url": "https://x.invalid/m", "id": "m"},
    }
    r = client.post("/api/jobs", json=[movie])
    assert r.status_code == 201
    assert r.json()[0]["movie_id"] == 7 and r.json()[0]["video_title"] == ""


def test_target_label_stored_and_returned(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    body = episode_job(conn_id)
    body["target"]["label"] = "Show S01E02 - Title"
    (job,) = client.post("/api/jobs", json=[body]).json()
    assert job["target_label"] == "Show S01E02 - Title"
    plain = episode_job(conn_id, "other")
    assert client.post("/api/jobs", json=[plain]).json()[0]["target_label"] is None


def _set_status(client: TestClient, job_id: int, status: str) -> None:
    from outriggarr.db.models import Job, JobStatus

    with client.app.state.session_factory() as s:
        job = s.get(Job, job_id)
        job.status = JobStatus(status)
        job.staged_path = "/staging/x"
        job.error = "stale error from the previous attempt"
        s.commit()


def test_retry_transitions(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    job_id = client.post("/api/jobs", json=[episode_job(conn_id)]).json()[0]["id"]
    # queued → 409
    r = client.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 409 and "queued" in r.json()["detail"]
    for st in ("failed", "cancelled"):
        _set_status(client, job_id, st)
        r = client.post(f"/api/jobs/{job_id}/retry")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "queued"
        assert r.json()["error"] is None and r.json()["next_retry_at"] is None
        assert r.json()["finished_at"] is None and r.json()["progress_pct"] == 0
    for st in ("downloading", "importing", "done"):
        _set_status(client, job_id, st)
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409
    assert client.post("/api/jobs/999/retry").status_code == 404


def test_cancel_transitions(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    job_id = client.post("/api/jobs", json=[episode_job(conn_id)]).json()[0]["id"]
    # a fresh queued job has no error: cancel says so
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["error"] == "cancelled" and r.json()["finished_at"] is not None
    for st in ("queued", "downloading", "failed"):
        _set_status(client, job_id, st)
        r = client.post(f"/api/jobs/{job_id}/cancel")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled"
        assert r.json()["finished_at"] is not None
        assert r.json()["error"] == "stale error from the previous attempt", "failure text kept"
        assert r.json()["staged_path"] == "/staging/x", "the worker sweeps the file, not the API"
    for st in ("importing", "done", "cancelled"):
        _set_status(client, job_id, st)
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409
    assert client.post("/api/jobs/999/cancel").status_code == 404


def test_done_job_does_not_block_a_new_one_but_live_does(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus

    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    first = client.post("/api/jobs", json=[episode_job(conn_id)]).json()[0]
    assert client.post("/api/jobs", json=[episode_job(conn_id)]).status_code == 409  # queued
    for st in ("failed", "cancelled"):
        with client.app.state.session_factory() as s:
            s.get(Job, first["id"]).status = JobStatus(st)
            s.commit()
        r = client.post("/api/jobs", json=[episode_job(conn_id)])
        assert r.status_code == 409 and "retry or cancel" in r.json()["detail"]
    with client.app.state.session_factory() as s:
        s.get(Job, first["id"]).status = JobStatus.done
        s.commit()
    r = client.post("/api/jobs", json=[episode_job(conn_id)])
    assert r.status_code == 201, "a done job is history; the same video can be grabbed again"
    assert r.json()[0]["id"] != first["id"]
    assert len(client.get("/api/jobs").json()) == 2


def test_video_url_must_be_http(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    bad = episode_job(conn_id)
    bad["video"]["url"] = "javascript:alert(1)"
    assert client.post("/api/jobs", json=[bad]).status_code == 422


def test_delete_job_terminal_only_and_removes_staging(client: TestClient, settings) -> None:
    from outriggarr.db.models import Job, JobStatus

    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    job_id = client.post("/api/jobs", json=[episode_job(conn_id)]).json()[0]["id"]
    assert client.delete(f"/api/jobs/{job_id}").status_code == 409  # queued
    with client.app.state.session_factory() as s:
        j = s.get(Job, job_id)
        j.status = JobStatus.failed
        j.next_retry_at = None
        s.commit()
    folder = settings.staging_dir / str(job_id)
    folder.mkdir(parents=True)
    (folder / "x.mkv").write_bytes(b"x")
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert not folder.exists() and client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.delete("/api/jobs/999").status_code == 404


def test_retry_resets_attempts_and_ids_are_deduped(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus

    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    body = episode_job(conn_id)
    body["target"]["episode_ids"] = [42, 42]
    assert client.post("/api/jobs", json=[body]).status_code == 422
    job_id = client.post("/api/jobs", json=[episode_job(conn_id)]).json()[0]["id"]
    with client.app.state.session_factory() as s:
        j = s.get(Job, job_id)
        j.status = JobStatus.failed
        j.attempts = 4
        s.commit()
    r = client.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 200 and r.json()["attempts"] == 0
    assert (
        Job.make_target_key(
            __import__("outriggarr.db.models", fromlist=["TargetKind"]).TargetKind.episode,
            series_id=5,
            episode_ids=[42, 42],
        )
        == "episode:5:42"
    )


def test_jobs_refused_for_a_disabled_connection(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json={**SONARR, "enabled": False}).json()["id"]
    r = client.post("/api/jobs", json=[episode_job(conn_id)])
    assert r.status_code == 422 and "disabled" in r.json()["detail"]


def test_delete_keeps_the_row_when_the_staging_folder_will_not_go(
    client, tmp_path, monkeypatch
) -> None:
    import os
    import stat

    from outriggarr.db.models import Job, JobStatus, TargetKind

    if os.geteuid() == 0:
        pytest.skip("root removes anything")
    client.post("/api/connections", json=SONARR)
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[1],
                target_key="episode:5:1",
                video_id="v",
                video_url="https://y/v",
                status=JobStatus.done,
            )
        )
        s.commit()
        job_id = s.scalars(select(Job.id)).one()
    staging = client.app.state.runner_deps.staging_dir
    folder = staging / str(job_id) / "locked"
    folder.mkdir(parents=True)
    (folder / "file.mkv").write_bytes(b"x")
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)  # no write bit: its file cannot be unlinked
    try:
        r = client.delete(f"/api/jobs/{job_id}")
        assert r.status_code == 409 and "could not be removed" in r.json()["detail"]
        assert client.get(f"/api/jobs/{job_id}").status_code == 200, "the row stays"
    finally:
        folder.chmod(stat.S_IRWXU)
    assert client.delete(f"/api/jobs/{job_id}").status_code in (200, 204)


@pytest.mark.parametrize(
    ("url", "listing"),
    [
        ("https://www.youtube.com/playlist?list=PL123", True),
        ("https://www.youtube.com/@FirstWeFeast", True),
        ("https://www.youtube.com/@FirstWeFeast/videos", True),
        ("https://www.youtube.com/channel/UCabc", True),
        ("https://www.youtube.com/watch?v=abc&list=PL123", False),
        ("https://youtu.be/abc", False),
        ("https://www.youtube.com/watch?v=abc", False),
        ("https://archive.org/details/x", False),
    ],
)
def test_a_listing_url_is_not_a_video(url: str, listing: bool) -> None:
    from outriggarr.api.jobs import looks_like_a_listing

    assert looks_like_a_listing(url) is listing


def test_jobs_api_refuses_a_playlist_url(client) -> None:
    client.post("/api/connections", json=SONARR)
    r = client.post(
        "/api/jobs",
        json=[
            {
                "connection_id": 1,
                "target": {"kind": "episode", "series_id": 5, "episode_ids": [1]},
                "video": {"url": "https://www.youtube.com/playlist?list=PL1", "id": "PL1"},
            }
        ],
    )
    assert r.status_code == 422 and "playlist or channel" in r.text


def test_cancel_is_one_conditional_write(client) -> None:
    from outriggarr.db.models import Job, JobStatus, TargetKind

    client.post("/api/connections", json=SONARR)
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[1],
                target_key="episode:5:1",
                video_id="v",
                video_url="https://y/v",
                status=JobStatus.importing,
            )
        )
        s.commit()
        job_id = s.scalars(select(Job.id)).one()
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 409 and "importing" in r.json()["detail"]
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "importing"
