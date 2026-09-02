from __future__ import annotations

from fastapi.testclient import TestClient

from outriggarr.db.models import Job, JobStatus

SONARR = {
    "kind": "sonarr",
    "name": "Sonarr",
    "url": "http://sonarr-host:1234",
    "api_key": "k1",
    "staging_path_remote": "/data/outriggarr",
}


def _job(client: TestClient, conn_id: int, video_id: str = "abc") -> int:
    return client.post(
        "/api/jobs",
        json=[
            {
                "connection_id": conn_id,
                "target": {
                    "kind": "episode",
                    "series_id": 5,
                    "episode_ids": [42],
                    "label": "Show S01E02 - Two",
                },
                "video": {
                    "url": f"https://youtube.invalid/watch?v={video_id}",
                    "id": video_id,
                    "title": "<b>Vid</b>",
                },
            }
        ],
    ).json()[0]["id"]


def test_home_redirects_and_static_served(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/activity"
    assert client.get("/static/pico.min.css").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/alpine.min.js").status_code == 200


def test_activity_page_and_rows(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    r = client.get("/activity")
    assert r.status_code == 200
    assert "No jobs" in r.text
    assert 'hx-get="/activity/rows?view=all"' in r.text

    job_id = _job(client, conn_id)
    with client.app.state.session_factory() as s:
        job = s.get(Job, job_id)
        job.status = JobStatus.failed
        job.error = "GET http://sonarr-host:1234/api/v3/manualimport -> HTTP 500: <boom>"
        s.commit()
    r = client.get("/activity/rows?view=failed")
    assert r.status_code == 200
    assert "Show S01E02 - Two" in r.text
    assert "&lt;b&gt;Vid&lt;/b&gt;" in r.text, "video title is escaped"
    assert "HTTP 500: &lt;boom&gt;" in r.text, "error text shown verbatim (escaped)"
    assert f'hx-post="/activity/jobs/{job_id}/retry?view=failed"' in r.text
    assert f'hx-post="/activity/jobs/{job_id}/cancel?view=failed"' in r.text
    assert "No jobs" in client.get("/activity/rows?view=done").text
    assert "status-failed" in client.get("/activity?view=bogus").text  # unknown view → all


def test_activity_retry_and_cancel_return_rows(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    job_id = _job(client, conn_id)
    r = client.post(f"/activity/jobs/{job_id}/cancel?view=all")
    assert r.status_code == 200 and "status-cancelled" in r.text
    assert "Retry" in r.text and "Cancel</button>" not in r.text
    r = client.post(f"/activity/jobs/{job_id}/retry?view=all")
    assert r.status_code == 200 and "status-queued" in r.text
    assert client.post(f"/activity/jobs/{job_id}/retry").status_code == 409
    assert client.post("/activity/jobs/999/cancel").status_code == 404


def test_grab_page_lists_enabled_connections(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    client.post(
        "/api/connections", json={**SONARR, "name": "Off", "url": "http://x:1", "enabled": False}
    )
    r = client.get("/grab")
    assert r.status_code == 200
    assert "Sonarr (sonarr)" in r.text and "Off (" not in r.text
    assert "grabApp()" in r.text and "/api/resolve" in r.text
    assert "No enabled connections" not in r.text


def test_grab_page_without_connections_warns(client: TestClient) -> None:
    assert "No enabled connections" in client.get("/grab").text


# ---- M4: Series screen -------------------------------------------------------------


def _seed_series(client: TestClient):
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from outriggarr.source import VideoRef
    from tests.fakes import FakeArrClient

    arr = client.app.state.arr_factory
    source = client.app.state.source
    now = datetime.now(UTC)
    arr.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[
            SeriesRef(5, "Hot Ones", 2015, 1, True),
            SeriesRef(6, "Hot Zone", 2019, 2, True),
        ],
        episodes_by_series={
            5: [
                EpisodeRef(11, 30, 6, "Six Spicy Wings", False, True, now - timedelta(days=1)),
                EpisodeRef(12, 30, 7, "Seven Spicy Wings", False, True, now - timedelta(days=1)),
            ]
        },
    )
    source.recent = [
        VideoRef("a", "Six Spicy Wings | Hot Ones", "https://y/a", 1, 1, None),
        VideoRef("b", "Bonus", "https://y/b", 1, 2, None),
    ]
    return client.post("/api/connections", json=SONARR).json()["id"]


def test_series_page_without_sonarr(client: TestClient) -> None:
    assert "No enabled Sonarr connection" in client.get("/series").text


def test_series_search_rows_and_subscribe_flow(client: TestClient) -> None:
    _seed_series(client)
    r = client.get("/series")
    assert r.status_code == 200 and 'hx-get="/series/rows"' in r.text
    assert "Type to search" in client.get("/series/rows").text
    rows = client.get("/series/rows?q=hot").text
    assert "Hot Ones" in rows and "Hot Zone" in rows and 'href="/series/5/subscribe"' in rows

    form = client.get("/series/5/subscribe")
    assert form.status_code == 200 and "Subscribe: Hot Ones" in form.text
    r = client.post(
        "/series/5/subscribe",
        data={
            "source_url": "https://www.youtube.com/@hotones",
            "strategies": ["title"],
            "date_tolerance_days": "2",
            "date_offset_days": "0",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/subscriptions/1"
    assert "subscribed ✓" in client.get("/series/rows?q=hot%20ones").text
    # subscribing again redirects to the existing one
    assert (
        client.get("/series/5/subscribe", follow_redirects=False).headers["location"]
        == "/subscriptions/1"
    )
    # bad form shows the error on the page
    r = client.post("/series/6/subscribe", data={"source_url": "nope"})
    assert r.status_code == 400 and "http://" in r.text


def test_subscription_page_preview_scan_and_override(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    page = client.get(f"/subscriptions/{sub_id}")
    assert page.status_code == 200
    assert f'hx-get="/subscriptions/{sub_id}/preview"' in page.text and "Scan now" in page.text

    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "would queue" in prev and "S30E06" in prev
    assert "S30E07" in prev and "no candidate" in prev
    assert f'hx-post="/subscriptions/{sub_id}/overrides"' in prev

    r = client.post(
        f"/subscriptions/{sub_id}/overrides", data={"video_id": "b", "season": "30", "episode": "7"}
    )
    assert r.status_code == 200 and "Override set for b" in r.text
    assert "<code>b</code>" in r.text and "S30E07" in r.text

    scan = client.post(f"/subscriptions/{sub_id}/scan")
    assert scan.status_code == 200 and "2 job(s) queued" in scan.text
    assert len(client.get("/api/jobs").json()) == 2
    assert "already have jobs" in client.get(f"/subscriptions/{sub_id}/preview").text

    r = client.post(f"/subscriptions/{sub_id}/overrides/b/delete")
    assert r.status_code == 200 and "Override removed for b" in r.text

    r = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={
            "source_url": "https://www.youtube.com/@other",
            "strategies": ["title", "date"],
            "date_tolerance_days": "3",
            "date_offset_days": "1",
            "title_regex": "",
            "format": "best",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    sub = client.get(f"/api/subscriptions/{sub_id}").json()
    assert (
        sub["enabled"] is False
        and sub["strategies"] == ["title", "date"]
        and sub["format"] == "best"
    )
    assert client.post(f"/subscriptions/{sub_id}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/api/subscriptions/{sub_id}").status_code == 404
    assert client.get(f"/subscriptions/{sub_id}", follow_redirects=False).status_code == 302
