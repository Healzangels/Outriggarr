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
    assert 'hx-trigger="input changed delay:300ms, search"' in r.text  # keyup misses paste
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


def prev_buttons(client: TestClient, sub_id: int) -> str:
    return client.get(f"/subscriptions/{sub_id}/preview").text


def test_subscription_page_preview_scan_and_override(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    page = client.get(f"/subscriptions/{sub_id}")
    assert page.status_code == 200
    assert f'hx-get="/subscriptions/{sub_id}/preview"' in page.text
    assert "Scan now" in prev_buttons(client, sub_id) and "Download" in prev_buttons(client, sub_id)

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
    assert scan.status_code == 200 and "Nothing queued" in scan.text
    assert "2 matched" in scan.text and "Download 2 matched" in scan.text
    assert client.get("/api/jobs").json() == [], "Scan now never queues"
    dl = client.post(f"/subscriptions/{sub_id}/download")
    assert dl.status_code == 200 and "Queued 2 job(s)" in dl.text
    assert len(client.get("/api/jobs").json()) == 2
    again = client.post(f"/subscriptions/{sub_id}/download")
    assert "Nothing to queue" in again.text and "disabled" in again.text
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


# ---- M5: Settings screen ------------------------------------------------------------


def test_settings_page_and_downloads_form(client: TestClient) -> None:
    r = client.get("/settings")
    assert (
        r.status_code == 200 and "Add a connection" in r.text and "Default yt-dlp format" in r.text
    )
    assert 'name="audio_language"' in r.text and 'value="eng"' in r.text
    r = client.post(
        "/settings/downloads",
        data={
            "scan_interval_minutes": "10",
            "concurrency": "2",
            "scan_video_limit": "20",
            "default_format": "best",
            "merge_container": "mp4",
            "cookies_path": "",
            "ytdlp_extra_opts": '{"ratelimit": 1}',
            "audio_language": "eng",
            "sonarr_tag": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = client.get("/api/settings").json()
    assert (s["scan_interval_minutes"], s["merge_container"], s["ytdlp_extra_opts"]) == (
        "10",
        "mp4",
        '{"ratelimit": 1}',
    )
    r = client.post("/settings/downloads", data={"concurrency": "99"})
    assert r.status_code == 400 and "concurrency must be between" in r.text
    assert client.get("/api/settings").json()["concurrency"] == "2"


def test_settings_connections_forms_and_test(client: TestClient) -> None:
    r = client.post(
        "/settings/connections",
        data={
            "kind": "sonarr",
            "name": "Sonarr",
            "url": "http://sonarr-host:1234",
            "api_key": "k1",
            "staging_path_remote": "/staging",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    (conn,) = client.get("/api/connections").json()
    page = client.get("/settings").text
    assert "Sonarr" in page and 'hx-post="/settings/connections/1/test"' in page
    assert "k1" not in page, "API key is never rendered"

    r = client.post("/settings/connections/1/test")
    assert r.status_code == 200 and "staging visible" in r.text
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"].visible_paths = set()
    assert "cannot see staging path" in client.post("/settings/connections/1/test").text

    # blank key keeps the stored one; other fields update
    r = client.post(
        "/settings/connections/1",
        data={
            "kind": "sonarr",
            "name": "Renamed",
            "url": "http://sonarr-host:1234",
            "api_key": "",
            "staging_path_remote": "/staging",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    (conn,) = client.get("/api/connections").json()
    assert conn["name"] == "Renamed" and conn["api_key"] == "k1" and conn["enabled"] is False
    r = client.post(
        "/settings/connections",
        data={
            "kind": "lidarr",
            "name": "x",
            "url": "http://x",
            "api_key": "k",
            "staging_path_remote": "/s",
        },
    )
    assert r.status_code == 400
    assert client.post("/settings/connections/1/delete", follow_redirects=False).status_code == 303
    assert client.get("/api/connections").json() == []


def test_grab_has_newest_first_toggle(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    assert 'x-model="bulk.reverse"' in client.get("/grab").text


def test_subscription_episodes_panel_states_and_jobs(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef

    _seed_series(client)
    now = datetime.now(UTC)
    fake = client.app.state.arr_factory.by_url["http://sonarr-host:1234"]
    fake.episodes_by_series[5] = [
        EpisodeRef(11, 30, 6, "Six Spicy Wings", False, True, now - timedelta(days=1)),
        EpisodeRef(12, 30, 7, "Has A File", True, True, now - timedelta(days=8)),
        EpisodeRef(13, 30, 8, "Not Yet", False, True, now + timedelta(days=3)),
        EpisodeRef(14, 30, 9, "Ignored", False, False, now - timedelta(days=30)),
        EpisodeRef(15, 29, 1, "Old One", True, True, now - timedelta(days=300)),
    ]
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    client.post(f"/api/subscriptions/{sub_id}/scan")  # queues S30E06 via title
    page = client.get(f"/subscriptions/{sub_id}").text
    assert f'hx-get="/subscriptions/{sub_id}/episodes"' in page

    html = client.get(f"/subscriptions/{sub_id}/episodes").text
    assert html.index("Season 30") < html.index("Season 29"), "newest season first"
    assert "1/4 files" in html and "1 missing" in html
    assert "1/1 files" in html
    for needle in ("✓ file", ">missing<", ">unaired<", ">unmonitored<"):
        assert needle in html, needle
    assert "status-queued" in html and "#1" in html, "the queued job is linked to S30E06"


def test_series_rows_show_file_counts(client: TestClient) -> None:
    from outriggarr.arr.base import SeriesRef

    _seed_series(client)
    fake = client.app.state.arr_factory.by_url["http://sonarr-host:1234"]
    fake.series_list = [
        SeriesRef(5, "Hot Ones", 2015, 1, True, 140, 128),
        SeriesRef(6, "Hot Zone", 2019, 2, True),
    ]
    rows = client.get("/series/rows?q=hot").text
    assert "128/140" in rows and "12 missing" in rows


def test_override_form_accepts_a_url(client: TestClient) -> None:
    from outriggarr.source import VideoRef

    _seed_series(client)
    source = client.app.state.source
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    source.videos = [VideoRef("old7", "Seven, older upload", "https://y/old7", 1, None, None)]
    r = client.post(
        f"/subscriptions/{sub_id}/overrides",
        data={"video_url": "https://y/old7", "season": "30", "episode": "7"},
    )
    assert r.status_code == 200
    assert "Override set for Seven, older upload (from URL)" in r.text
    assert ">URL<" in r.text and "S30E07" in r.text
    assert "would queue" in r.text  # S30E07 now matches via the override
    r = client.post(f"/subscriptions/{sub_id}/overrides", data={"season": "30", "episode": "7"})
    assert "Pick a video or paste a URL" in r.text
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert 'name="video_url"' in prev or "Unmatched" not in prev


def test_branding_assets_and_favicon(client: TestClient) -> None:
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/favicon.ico").headers["content-type"].startswith("image/")
    assert client.get("/static/outriggarr.svg").status_code == 200
    assert client.get("/static/outriggarr.png").status_code == 200
    page = client.get("/activity").text
    assert '<link rel="icon" href="/static/favicon.ico"' in page
    assert '<img src="/static/outriggarr.svg"' in page and 'class="brand"' in page


def test_settings_page_has_subtitle_fields(client: TestClient) -> None:
    page = client.get("/settings").text
    assert 'name="subtitles_langs"' in page and 'name="subtitles_auto"' in page


def test_settings_notifications_form_and_test_button(client: TestClient, notifier) -> None:
    page = client.get("/settings").text
    assert 'name="apprise_urls"' in page and 'hx-post="/settings/notify/test"' in page
    assert "✗ no Apprise URLs" in client.post("/settings/notify/test").text
    r = client.post(
        "/settings/downloads",
        data={
            "apprise_urls": "json://localhost:1/hook",
            "_notify_form": "1",
            "notify_on_done": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = client.get("/api/settings").json()
    assert s["apprise_urls"] == "json://localhost:1/hook"
    assert (s["notify_on_done"], s["notify_on_failed"], s["notify_on_scan_error"]) == (
        "1",
        "0",
        "0",
    ), "unchecked boxes save as 0"
    assert "✓ sent" in client.post("/settings/notify/test").text
    assert notifier.sent[-1][0] == "Outriggarr: test"
    r = client.post("/settings/downloads", data={"apprise_urls": "nope://x", "_notify_form": "1"})
    assert r.status_code == 400 and "did not accept" in r.text
