from __future__ import annotations

from fastapi.testclient import TestClient

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
