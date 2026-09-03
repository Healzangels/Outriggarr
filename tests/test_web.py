from __future__ import annotations

import re

import pytest
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
    r = client.post(
        f"/activity/jobs/{job_id}/retry"
    )  # queued: not retryable → notice, no silent 409
    assert r.status_code == 200 and "only failed or cancelled" in r.text
    r = client.post("/activity/jobs/999/cancel")
    assert r.status_code == 200 and "not found" in r.text


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
            "sources": "https://www.youtube.com/@hotones",
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
    r = client.post("/series/6/subscribe", data={"sources": "nope"})
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
    assert "select to download" in prev and "S30E06" in prev  # a match, no job yet
    assert "S30E07" in prev and "no candidate" in prev
    assert f'hx-post="/subscriptions/{sub_id}/overrides"' in prev

    r = client.post(
        f"/subscriptions/{sub_id}/overrides", data={"video_id": "b", "season": "30", "episode": "7"}
    )
    assert r.status_code == 200 and "Override set for b" in r.text
    assert "<code>b</code>" in r.text and "S30E07" in r.text

    scan = client.post(f"/subscriptions/{sub_id}/scan")
    assert scan.status_code == 200 and "Nothing queued" in scan.text
    assert "2 matched" in scan.text and "Download all 2" in scan.text
    assert client.get("/api/jobs").json() == [], "Scan now never queues"
    dl = client.post(f"/subscriptions/{sub_id}/download")
    assert dl.status_code == 200 and "Queued 2 jobs." in dl.text
    assert len(client.get("/api/jobs").json()) == 2
    again = client.post(f"/subscriptions/{sub_id}/download")
    assert "Nothing to queue" in again.text and "disabled" in again.text
    assert "already have jobs" in client.get(f"/subscriptions/{sub_id}/preview").text

    r = client.post(f"/subscriptions/{sub_id}/overrides/b/delete")
    assert r.status_code == 200 and "Override removed for b" in r.text

    r = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={
            "sources": "https://www.youtube.com/@other",
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
    assert conn["name"] == "Renamed" and conn["enabled"] is False
    assert "api_key" not in conn and conn["has_api_key"] is True, "the key never comes back out"
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
        json={
            "connection_id": 1,
            "series_id": 5,
            "source_url": "https://www.youtube.com/@hotones",
            "auto_download": "all",
        },
    ).json()["id"]
    client.post(f"/api/subscriptions/{sub_id}/scan")  # queues S30E06 via title
    page = client.get(f"/subscriptions/{sub_id}").text
    assert f'hx-get="/subscriptions/{sub_id}/episodes"' in page
    series = client.get("/series").text
    assert "1 matched</span>" in series and "1 new job</span>" in series, (
        "the list says what the last scan did, not what is queued now"
    )
    assert "1 queued" not in series

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
    source.infos["https://y/old7"] = VideoRef(
        "old7", "Seven, older upload", "https://y/old7", 1, None, None
    )
    r = client.post(
        f"/subscriptions/{sub_id}/overrides",
        data={"video_url": "https://y/old7", "season": "30", "episode": "7"},
    )
    assert r.status_code == 200
    assert "Override set for Seven, older upload (from URL)" in r.text
    assert ">via URL<" in r.text and "S30E07" in r.text
    assert "select to download" in r.text  # S30E07 now matches via the override
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
    assert '<link rel="icon" href="/static/favicon.ico?v=' in page
    assert 'class="brand"><svg' in page, "the logo is inline; no request, no flicker"


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


def test_subscribe_form_with_no_strategies_is_pins_only(client: TestClient) -> None:
    _seed_series(client)
    r = client.post(
        "/series/5/subscribe",
        data={"sources": "https://www.youtube.com/@hotones"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get("/api/subscriptions").json()[0]["strategies"] == []


def test_activity_stale_retry_shows_a_notice_not_a_silent_409(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    job_id = _job(client, conn_id)
    r = client.post(f"/activity/jobs/{job_id}/retry?view=all")  # queued → not retryable
    assert r.status_code == 200 and "only failed or cancelled" in r.text
    r = client.post("/activity/jobs/999/cancel?view=all")
    assert r.status_code == 200 and "not found" in r.text


def test_error_details_survive_polling_and_confirm_strings_are_data_attrs(
    client: TestClient,
) -> None:
    conn_id = client.post("/api/connections", json={**SONARR, "name": "x'); alert(1); ('"}).json()[
        "id"
    ]
    job_id = _job(client, conn_id)
    with client.app.state.session_factory() as s:
        job = s.get(Job, job_id)
        job.status = JobStatus.failed
        job.error = "boom"
        s.commit()
    rows = client.get("/activity/rows?view=failed").text
    assert re.search(rf'id="err-{job_id}-\d+" hx-preserve', rows), "keyed by job and attempt"
    page = client.get("/settings").text
    assert 'onsubmit="return confirm(this.dataset.confirm)"' in page
    assert "return confirm('" not in page, "no user text inside an inline JS string"
    assert 'data-confirm="Delete connection x&#39;); alert(1); (&#39;?"' in page
    assert 'autocomplete="new-password"' in page and 'autocomplete="off"' not in page


def test_grab_excludes_rows_that_already_have_a_file_after_fill(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    page = client.get("/grab").text
    assert "if (e && e.has_file) r.include = false" in page
    assert "if (m.has_file) row.include = false" in page
    assert ".slice(0, 300)" in page


# ---- polish pass 2 -----------------------------------------------------------------


def test_activity_delete_button_and_cap_note(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]
    job_id = _job(client, conn_id)
    rows = client.get("/activity/rows?view=all").text
    assert "/delete?view=all" not in rows, "queued jobs cannot be deleted"
    with client.app.state.session_factory() as s:
        s.get(Job, job_id).status = JobStatus.done
        s.commit()
    rows = client.get("/activity/rows?view=all").text
    assert f'hx-post="/activity/jobs/{job_id}/delete?view=all"' in rows
    r = client.post(f"/activity/jobs/{job_id}/delete?view=all")
    assert r.status_code == 200 and f"Job {job_id} deleted" in r.text
    assert client.get("/api/jobs").json() == []
    assert 'aria-current="page"' in client.get("/activity").text


def test_preview_hints_when_the_source_cannot_carry_the_episodes(client: TestClient) -> None:
    from outriggarr.source import VideoRef

    _seed_series(client)
    source = client.app.state.source
    source.recent = [
        VideoRef("z1", "Totally Unrelated Upload", "https://y/z1", 1, 1, None),
        VideoRef("dead", "dead", "https://y/dead", None, 2, None),
    ]
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "may not carry these episodes" in prev
    assert "unavailable video" in prev and '<option value="dead">' not in prev
    source.recent = [VideoRef("a", "Six Spicy Wings | Hot Ones", "https://y/a", 1, 1, None)]
    assert "may not carry these episodes" not in client.get(f"/subscriptions/{sub_id}/preview").text


def test_grab_marks_unavailable_videos(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    page = client.get("/grab").text
    assert "unavailable video" in page and "include: v.title !== v.id" in page
    assert 'aria-label="season"' in page and "htmx:responseError" in page


def test_subscription_form_video_limit_and_picker_datalist(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    page = client.get(f"/subscriptions/{sub_id}").text
    assert 'name="video_limit"' in page and "global setting (50)" in page
    base = {
        "sources": "https://www.youtube.com/@hotones",
        "strategies": ["title"],
        "date_tolerance_days": "2",
        "date_offset_days": "0",
        "title_regex": "",
        "format": "",
    }
    edit = f"/subscriptions/{sub_id}/edit"
    r = client.post(edit, data={**base, "video_limit": "1200"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/api/subscriptions/{sub_id}").json()["video_limit"] == 1200
    r = client.post(edit, data={**base, "video_limit": "lots"}, follow_redirects=False)
    assert r.status_code == 400 and "whole number" in r.text
    assert client.get(f"/api/subscriptions/{sub_id}").json()["video_limit"] == 1200
    r = client.post(edit, data={**base, "video_limit": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/api/subscriptions/{sub_id}").json()["video_limit"] is None
    # the picker is one shared datalist of listed videos, posted as a URL pin
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    if "Unmatched" in prev:
        assert '<datalist id="listed-videos">' in prev and 'list="listed-videos"' in prev
        assert 'name="video_id"' not in prev


def test_preview_holds_a_length_mismatch_and_pin_releases_it(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from outriggarr.source import VideoRef
    from tests.fakes import FakeArrClient

    now = datetime.now(UTC)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Hot Ones", 2015, 1, True)],
        episodes_by_series={
            5: [
                EpisodeRef(
                    11, 30, 6, "Six Spicy Wings", False, True, now - timedelta(days=1), runtime=25
                )
            ]
        },
    )
    source = client.app.state.source
    clip = VideoRef("a", "Six Spicy Wings | Hot Ones", "https://y/a", 90, 1, None)
    source.recent = [clip]
    source.infos["https://y/a"] = clip
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "1 held" in prev and "video runs 1m30s, Sonarr says the episode runs 25 min" in prev
    assert "It's right, pin it" in prev and "select to download" not in prev  # held: no row
    r = client.post(f"/subscriptions/{sub_id}/download")
    assert r.status_code == 200 and client.get("/api/jobs").json() == []
    # pinning it is the release valve: pins are never held
    r = client.post(
        f"/subscriptions/{sub_id}/overrides",
        data={"video_url": "https://y/a", "season": "30", "episode": "6"},
    )
    assert (
        r.status_code == 200 and "held" not in r.text.split("Your pins")[0].split("unmatched")[-1]
    )
    assert "select to download" in r.text  # pinned: a plain match row now
    page = client.get("/matches?view=review").text
    assert "Nothing needs a look" in page
    client.post(f"/subscriptions/{sub_id}/download")
    (job,) = client.get("/api/jobs").json()
    assert (job["matched_by"], job["video_duration"], job["target_runtime"]) == ("override", 90, 25)
    page = client.get("/matches?view=review").text
    assert "Six Spicy Wings" in page and "1m30s vs 25 min ✗" in page, "flagged even though pinned"
    assert 'Recorded by the scheduler when it paired them">override</span>' in page


def test_matches_page_tiers_and_fallback_for_old_jobs(client: TestClient) -> None:
    from outriggarr.db.models import Job, TargetKind
    from outriggarr.web.pages import review_entry

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    client.post(f"/subscriptions/{sub_id}/download")
    page = client.get("/matches").text
    assert "Six Spicy Wings" in page and 'paired them">contains</span>' in page
    assert f'href="/subscriptions/{sub_id}"' in page
    assert "needs a look" in page and "Matches" in client.get("/activity").text  # nav link
    old = Job(
        connection_id=1,
        target_kind=TargetKind.episode,
        target_key="x",
        video_id="v",
        video_url="https://y/v",
        video_title="Seven Spicy Wings",
        target_label="Hot Ones S30E07 - Seven Spicy Wings",
    )
    assert review_entry(old)["tier"] == "exact" and review_entry(old)["needs_look"] is False
    old.video_title = "Seven Spicy Wings | Hot Ones"
    assert review_entry(old)["tier"] == "contains" and review_entry(old)["needs_look"] is True
    old.video_title = "Something"
    assert review_entry(old)["tier"] == "unknown", "no subscription to consult"
    from outriggarr.db.models import Override, Subscription

    old.subscription = Subscription(strategies=["title", "date"], overrides=[])
    assert review_entry(old)["tier"] == "date", "the only other enabled strategy"
    old.subscription.strategies = ["title", "regex", "date"]
    assert review_entry(old)["tier"] == "unknown", "could have been either"
    old.subscription.overrides = [Override(video_id="v", season=30, episode=7)]
    assert review_entry(old)["tier"] == "override" and review_entry(old)["inferred"] is True
    # a job from before the tier was recorded shows its inferred tier with a "?"
    with client.app.state.session_factory() as s:
        sub = s.get(Subscription, sub_id)
        sub.strategies = ["title", "date"]
        s.add(
            Job(
                connection_id=1,
                subscription_id=sub_id,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[12],
                target_key="episode:5:12",
                video_id="q",
                video_url="https://y/q",
                video_title="Totally different upload",
                target_label="Hot Ones S30E07 - Seven Spicy Wings",
            )
        )
        s.commit()
    page = client.get("/matches?view=all").text
    assert "date?" in page and "Worked out afterwards" in page


def test_subscription_form_takes_one_source_per_line(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@hotones"]},
    ).json()["id"]
    base = {
        "strategies": ["title"],
        "date_tolerance_days": "2",
        "date_offset_days": "0",
        "title_regex": "",
        "format": "",
    }
    r = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={
            **base,
            "sources": "https://www.youtube.com/@hotones\n\n https://www.youtube.com/@extra \n",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert client.get(f"/api/subscriptions/{sub_id}").json()["sources"] == [
        "https://www.youtube.com/@hotones",
        "https://www.youtube.com/@extra",
    ]
    page = client.get(f"/subscriptions/{sub_id}").text
    assert 'youtube.com/@hotones</a> <span class="muted">·</span> ' in page
    assert "@hotones\nhttps://www.youtube.com/@extra</textarea>" in page
    assert "+1 more" in client.get("/series").text
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "videos listed from 2 sources" in prev


def test_matches_recheck_and_confirm_clear_the_review_list(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from outriggarr.db.models import Job, TargetKind
    from outriggarr.source import VideoRef
    from tests.fakes import FakeArrClient

    now = datetime.now(UTC)
    aired = now - timedelta(days=1)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", 2015, 1, True)],
        episodes_by_series={
            5: [
                EpisodeRef(11, 30, 6, "Six", False, True, aired, runtime=25),
                EpisodeRef(12, 30, 7, "Seven", False, True, aired, runtime=25),
                EpisodeRef(13, 30, 8, "Eight", False, True, aired, runtime=25),
            ]
        },
    )
    source = client.app.state.source
    source.recent = []
    source.infos = {
        "https://y/ok": VideoRef("ok", "x", "https://y/ok", 1500, 1, None),
        "https://y/short": VideoRef("short", "x", "https://y/short", 120, 1, None),
    }
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    # jobs from before evidence was recorded: fine, wrong length, unfetchable, and a
    # two-episode target whose second episode has no runtime in Sonarr
    with client.app.state.session_factory() as s:
        for eid, vid in ((11, "ok"), (12, "short"), (13, "gone"), (14, "double")):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=sub_id,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[eid] if eid != 14 else [11, 99],
                    target_key=f"episode:5:{eid}",
                    video_id=vid,
                    video_url=f"https://y/{vid}",
                    video_title="Something else",
                    target_label=f"Show S30E0{eid - 5} - T{eid}",
                )
            )
        s.commit()
    source.infos["https://y/double"] = VideoRef("double", "x", "https://y/double", 3000, 1, None)
    page = client.get("/matches").text
    assert (
        'needs a look<span class="count">4</span>' in page and page.count("length unchecked") == 4
    )
    assert 'aria-current="page">needs a look' in page, "work to do: land on the review view"

    import time

    r = client.post("/matches/recheck")
    assert r.status_code == 200, r.text
    assert 'hx-get="/matches/content?view=review" hx-trigger="every 2s"' in r.text, "polls itself"
    for _ in range(100):  # the recheck is a background task on the app; wait for it
        status = client.get("/api/matches/recheck").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["running"] is False and status["failure"] is None, status
    assert status["total"] == 4 and status["done"] == 4, status
    r = client.get("/matches/content?view=review")
    assert 'hx-trigger="every 2s"' not in r.text, "polling stops when the run is over"
    assert (
        "Checked 4 pairings: 3 video lengths and 3 runtimes fetched; 1 contradict their runtime."
        in r.text
    )
    assert "1 could not be fetched" in r.text
    assert 'needs a look<span class="count">3</span>' in r.text, "25 min vs 25 min cleared itself"
    assert "Checked 4 pairings" not in client.get("/matches").text, "the summary shows once"
    assert "2 unchecked" in r.text, "the button says how much is left"
    assert 'disabled title="Every pairing' not in r.text, "still something to check: enabled"
    jobs = {j["video_id"]: j for j in client.get("/api/jobs").json()}
    assert jobs["double"]["target_runtime"] is None, "a half-known runtime is no evidence"
    assert "50m00s, no runtime in Sonarr" in r.text
    assert "2m00s vs 25 min ✗" in r.text
    assert "25m00s vs 25 min ✓" in client.get("/matches?view=all").text
    assert (jobs["ok"]["video_duration"], jobs["ok"]["target_runtime"]) == (1500, 25)
    assert client.post("/api/matches/recheck").json()["running"] is True
    for _ in range(100):
        status = client.get("/api/matches/recheck").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["checked"] == 2, "unfetched + half-known"

    short_id = jobs["short"]["id"]
    r = client.post(f"/matches/{short_id}/confirm")
    assert "Confirmed: Show S30E07 - T12." in r.text
    assert 'needs a look<span class="count">2</span>' in r.text
    assert client.get(f"/api/jobs/{short_id}").json()["reviewed_at"] is not None
    r = client.post(f"/matches/{short_id}/unconfirm?view=review")
    assert 'needs a look<span class="count">3</span>' in r.text
    r = client.post("/matches/confirm-all")
    assert (
        "Confirmed 3 pairings." in r.text and 'needs a look<span class="count">0</span>' in r.text
    )
    assert client.get("/matches?view=all").text.count(">confirmed</span>") == 3
    # nothing left to look at: the page lands on "all" and the recheck button is inert
    landing = client.get("/matches").text
    assert (
        'aria-current="page">all<' in landing and 'aria-current="page">needs a look' not in landing
    )
    assert "Every pairing already has its length evidence" in landing
    assert 'disabled title="Every pairing already has its length evidence"' in landing
    assert "Nothing left to check" not in landing
    assert client.delete(f"/api/jobs/{short_id}/confirm").json()["reviewed_at"] is None


def test_static_assets_are_cacheable_and_the_logo_is_inline(client: TestClient) -> None:
    page = client.get("/activity").text
    assert "/static/app.css?v=" in page and 'src="/static/outriggarr.svg"' not in page
    assert '<a href="/activity" class="brand"><svg' in page, "no fetch, no flicker"
    r = client.get("/static/app.css")
    assert r.headers["cache-control"] == "public, max-age=3600"
    r = client.get("/static/app.css?v=abc")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "cache-control" not in client.get("/activity").headers


def test_only_one_recheck_runs_at_a_time(client: TestClient, monkeypatch) -> None:
    import time

    from outriggarr.db.models import Job, TargetKind
    from outriggarr.source import VideoRef

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                subscription_id=sub_id,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[11],
                target_key="episode:5:11",
                video_id="slow",
                video_url="https://y/slow",
                video_title="x",
                target_label="Show S30E06 - Six",
            )
        )
        s.commit()
    source = client.app.state.source
    real = source.fetch_info

    def slow_fetch(url):
        time.sleep(0.4)
        return real(url)

    monkeypatch.setattr(source, "fetch_info", slow_fetch)
    source.infos["https://y/slow"] = VideoRef("slow", "x", "https://y/slow", 1500, 1, None)
    first = client.post("/api/matches/recheck").json()
    second = client.post("/api/matches/recheck").json()
    assert first["running"] and second["running"]
    assert second["started_at"] == first["started_at"], "the running one is returned, not a new one"
    for _ in range(100):
        status = client.get("/api/matches/recheck").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["checked"] == 1 and status["durations_filled"] == 1, status
    third = client.post("/api/matches/recheck").json()
    assert third["started_at"] != first["started_at"], "a finished one can be started again"


def test_polish_pass_markup(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    # delete buttons sit in the main action row and post through their own form
    settings = client.get("/settings").text
    assert 'form="delete-connection-1"' in settings and 'id="delete-connection-1"' in settings
    assert '<details class="panel"' in settings, "collapsible sections share one look"
    page = client.get(f"/subscriptions/{sub_id}").text
    assert 'form="delete-subscription"' in page and 'id="delete-subscription"' in page
    assert "Subscription settings" in page
    # the pin form is one input group, not an input with a button wrapped underneath
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    if "Unmatched" in prev:
        assert '<fieldset role="group" class="pin">' in prev
    # time cells never wrap
    assert (
        'class="muted when"' in client.get("/activity").text
        or "No jobs" in client.get("/activity").text
    )


def test_source_hint_is_dropped_once_the_subscription_has_history(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from outriggarr.source import VideoRef
    from tests.fakes import FakeArrClient

    now = datetime.now(UTC)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", 2015, 1, True)],
        episodes_by_series={
            5: [EpisodeRef(11, 30, 6, "Six Spicy Wings", False, True, now - timedelta(days=1))]
        },
    )
    source = client.app.state.source
    source.recent = [VideoRef("z", "Nothing like it", "https://y/z", 1, 1, None)]
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "This source may not carry these episodes" in prev, "never matched: fair warning"
    client.post(
        "/api/jobs",
        json=[
            {
                "connection_id": 1,
                "subscription_id": sub_id,
                "target": {"kind": "episode", "series_id": 5, "episode_ids": [11], "label": "x"},
                "video": {"url": "https://y/z", "id": "z", "title": "Nothing like it"},
            }
        ],
    )
    from outriggarr.db.models import Job, JobStatus

    with client.app.state.session_factory() as s:
        job = s.query(Job).first()
        job.subscription_id = sub_id  # a job on record, however it got there
        job.status = JobStatus.done  # done: it no longer covers the episode, which stays wanted
        s.commit()
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "S30E06" in prev and "no candidate" in prev, "still unmatched with nothing seen"
    assert "This source may not carry these episodes" not in prev, "history alone drops the hint"


def test_subscription_form_audio_language(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    page = client.get(f"/subscriptions/{sub_id}").text
    assert 'name="audio_language"' in page and "declared by the source" in page
    base = {
        "sources": "https://www.youtube.com/@x",
        "strategies": ["title"],
        "date_tolerance_days": "2",
        "date_offset_days": "0",
        "title_regex": "",
        "format": "",
    }
    r = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={**base, "audio_language": "jpn"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(f"/api/subscriptions/{sub_id}").json()["audio_language"] == "jpn"
    r = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={**base, "audio_language": "nope"},
        follow_redirects=False,
    )
    assert r.status_code == 400 and "3-letter" in r.text


def test_subscribe_form_defaults_to_future_and_preview_downloads_selected(
    client: TestClient,
) -> None:
    _seed_series(client)
    page = client.get("/series/5/subscribe").text
    assert 'name="auto_download" value="future" checked' in page
    assert "Everything Sonarr wants" in page and "Nothing automatic" in page
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@hotones"]},
    ).json()["id"]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "auto: future only, 0 of 1 would queue" in prev and "select to download" in prev
    assert 'name="episode_id" value="11"' in prev and "Download selected" in prev
    assert 'id="selected-count">0</span>' in prev and 'id="tick-all"' in prev, "a live count"
    untouched = 'name="episode_id" value="11" aria-label="download S30E06">'
    assert untouched in prev, "unselected by default"
    assert "picks.addEventListener('change', update)" in prev
    # ticking nothing queues nothing; ticking S30E06 queues just that; the scheduler alone would not
    r = client.post(f"/subscriptions/{sub_id}/download", data={"selected": "1"})
    assert "Nothing selected." in r.text and client.get("/api/jobs").json() == []
    r = client.post(
        f"/subscriptions/{sub_id}/download", data={"selected": "1", "episode_id": ["11"]}
    )
    assert "Queued 1 job." in r.text
    assert [j["episode_ids"] for j in client.get("/api/jobs").json()] == [[11]]


def test_fetch_upload_dates_runs_in_the_background_and_caches(
    client: TestClient, monkeypatch
) -> None:
    import time
    from datetime import UTC, datetime, timedelta

    from outriggarr.worker import scheduler

    monkeypatch.setattr(scheduler, "DATE_FETCH_LIMIT", 0)  # the scan's own trickle: off

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from outriggarr.db.models import VideoMeta
    from outriggarr.source import VideoRef
    from tests.fakes import FakeArrClient

    now = datetime.now(UTC)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", 2015, 1, True)],
        episodes_by_series={
            5: [EpisodeRef(11, 30, 6, "Nothing alike", False, True, now - timedelta(days=400))]
        },
    )
    source = client.app.state.source
    source.recent = [
        VideoRef("a", "Retitled one", "https://y/a", 100, 1, None),
        VideoRef("b", "Another", "https://y/b", 100, 2, None),
        VideoRef("c", "Gone", "https://y/c", 100, 3, None),
        VideoRef("d", "d", "https://y/d", None, 4, None),  # unavailable: never fetched
    ]
    source.infos = {
        "https://y/a": VideoRef("a", "Retitled one", "https://y/a", 100, 1, "20160218"),
        "https://y/b": VideoRef("b", "Another", "https://y/b", 100, 2, None),
    }
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={
            "connection_id": 1,
            "series_id": 5,
            "sources": ["https://www.youtube.com/@x"],
            "strategies": ["title", "date"],
        },
    ).json()["id"]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "Fetch upload dates" in prev and "3 undated" in prev, "unavailable entries do not count"
    fetched_before = len(source.fetched)
    r = client.post(f"/subscriptions/{sub_id}/dates")
    assert r.status_code == 200 and f'hx-get="/subscriptions/{sub_id}/dates/status"' in r.text
    for _ in range(100):
        st = client.get(f"/api/subscriptions/{sub_id}/dates").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["failure"] is None and (
        st["total"],
        st["dated"],
        st["unknown"],
        st["error_count"],
    ) == (3, 1, 1, 1), st
    assert "Fetched dates for 1 of 3 videos (1 carry none); 1 could not be fetched" in st["summary"]
    assert len(source.fetched) - fetched_before == 3, "the scan's own trickle did not run again"
    with client.app.state.session_factory() as s:
        rows = {m.video_id: m.upload_date for m in s.query(VideoMeta).all()}
    assert rows == {"a": "20160218", "b": None, "c": None}
    status_html = client.get(f"/subscriptions/{sub_id}/dates/status").text
    assert "Fetched dates for 1 of 3" in status_html and "every 3s" not in status_html
    assert "Fetch upload dates" not in client.get(f"/subscriptions/{sub_id}/preview").text, (
        "every listed video is now dated or known to carry no date: nothing left to offer"
    )
    assert "Fetched dates" not in client.get(f"/subscriptions/{sub_id}/dates/status").text, (
        "shown once"
    )
    # the second start is a no-op for the already-known ones: nothing left to fetch
    client.post(f"/api/subscriptions/{sub_id}/dates")
    for _ in range(100):
        st = client.get(f"/api/subscriptions/{sub_id}/dates").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["total"] == 0 and "already has its date" in st["summary"]
    assert client.post("/api/subscriptions/999/dates").status_code == 404
    # the button is not offered when the date strategy is off
    client.put(
        f"/api/subscriptions/{sub_id}",
        json={
            "connection_id": 1,
            "series_id": 5,
            "sources": ["https://www.youtube.com/@x"],
            "strategies": ["title"],
        },
    )
    assert "Fetch upload dates" not in client.get(f"/subscriptions/{sub_id}/preview").text


def test_missing_episode_with_a_stale_job_offers_a_clear_button(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from outriggarr.db.models import Job, JobStatus, TargetKind
    from tests.fakes import FakeArrClient

    now = datetime.now(UTC)
    aired = now - timedelta(days=3)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", 2015, 1, True)],
        episodes_by_series={
            5: [
                EpisodeRef(11, 30, 6, "Six", False, True, aired),  # file deleted after import
                EpisodeRef(12, 30, 7, "Seven", False, True, aired),  # a live job: no ✕
                EpisodeRef(13, 30, 8, "Eight", True, True, aired),  # has a file: no ✕
            ]
        },
    )
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    with client.app.state.session_factory() as s:
        for eid, vid, st in (
            (11, "a", JobStatus.done),
            (12, "b", JobStatus.queued),
            (13, "c", JobStatus.done),
        ):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=sub_id,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[eid],
                    target_key=f"episode:5:{eid}",
                    video_id=vid,
                    video_url=f"https://y/{vid}",
                    video_title="t",
                    target_label=f"Show S30E0{eid - 5}",
                    status=st,
                )
            )
        s.commit()
        stale = s.query(Job).filter_by(video_id="a").one().id
        live = s.query(Job).filter_by(video_id="b").one().id
    panel = client.get(f"/subscriptions/{sub_id}/episodes").text
    assert f'aria-label="clear job {stale}"' in panel, "missing + done job: clearable"
    assert f'aria-label="clear job {live}"' not in panel, "a live job is not history"
    assert panel.count("x-clear") == 1
    r = client.post(f"/subscriptions/{sub_id}/episodes/jobs/{stale}/clear")
    assert r.status_code == 200 and f"Cleared job #{stale}." in r.text
    assert 'class="notice" role="status"' in r.text, "an action notice: the page fades it out"
    page = client.get(f"/subscriptions/{sub_id}").text
    assert "htmx:afterSwap', () => arm(document)" in page, (
        "a notice swapped in by outerHTML is never inside the event's target"
    )
    assert "closest('summary')" in page and "summary.blur()" in page, (
        "mouse-toggled sections drop focus"
    )
    assert "x-clear" not in r.text
    assert client.get(f"/api/jobs/{stale}").status_code == 404
    r = client.post(f"/subscriptions/{sub_id}/episodes/jobs/{live}/clear")
    assert (
        f"Job #{live} not cleared" in r.text and client.get(f"/api/jobs/{live}").status_code == 200
    )


RATE_LIMITED_TEXT = (
    "ERROR: [youtube] x: This content isn't available, try again later. "
    "The current session has been rate-limited by YouTube for up to an hour."
)


def test_date_fetch_under_a_rate_limit_leaves_the_rest_for_later(
    client: TestClient, monkeypatch
) -> None:
    import time
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from outriggarr.db.models import VideoMeta
    from outriggarr.source import SourceError, VideoRef
    from outriggarr.worker import scheduler
    from tests.fakes import FakeArrClient

    monkeypatch.setattr(scheduler, "DATE_FETCH_LIMIT", 0)
    now = datetime.now(UTC)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", 2015, 1, True)],
        episodes_by_series={
            5: [EpisodeRef(11, 30, 6, "Nothing alike", False, True, now - timedelta(days=400))]
        },
    )
    source = client.app.state.source
    source.recent = [
        VideoRef(v, f"Video {v}", f"https://y/{v}", 100, i, None) for i, v in enumerate("abc")
    ]
    monkeypatch.setattr(
        source, "fetch_info", lambda url: (_ for _ in ()).throw(SourceError(RATE_LIMITED_TEXT))
    )
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={
            "connection_id": 1,
            "series_id": 5,
            "sources": ["https://www.youtube.com/@x"],
            "strategies": ["title", "date"],
        },
    ).json()["id"]
    client.post(f"/subscriptions/{sub_id}/dates")
    for _ in range(100):
        st = client.get(f"/api/subscriptions/{sub_id}/dates").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["failure"] is None and st["error_count"] == 0 and st["skipped"] == 3, st
    assert "3 left for later: the source rate-limited us" in st["summary"]
    with client.app.state.session_factory() as s:
        assert s.query(VideoMeta).count() == 0, "nothing is remembered as unknown for a week"
    assert client.app.state.runner_deps.cooloff.active()
    assert "rate-limited: paused 15 min" in client.get(f"/subscriptions/{sub_id}").text
    client.app.state.runner_deps.cooloff.clear()


def test_recheck_under_a_rate_limit_leaves_the_rest_for_later(
    client: TestClient, monkeypatch
) -> None:
    import time

    from outriggarr.db.models import Job, TargetKind
    from outriggarr.source import SourceError

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                subscription_id=sub_id,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[11],
                target_key="episode:5:11",
                video_id="rl",
                video_url="https://y/rl",
                video_title="x",
                target_label="Show S30E06 - Six",
            )
        )
        s.commit()
    source = client.app.state.source
    monkeypatch.setattr(
        source, "fetch_info", lambda url: (_ for _ in ()).throw(SourceError(RATE_LIMITED_TEXT))
    )
    assert client.post("/api/matches/recheck").status_code in (200, 202)
    for _ in range(100):
        st = client.get("/api/matches/recheck").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["failure"] is None and st["error_count"] == 0 and st["skipped"] == 1, st
    assert "1 left for later: the source rate-limited us" in st["summary"]
    with client.app.state.session_factory() as s:
        assert s.query(Job).one().video_duration is None
    assert client.app.state.runner_deps.cooloff.active()
    client.app.state.runner_deps.cooloff.clear()


def test_title_scope_round_trips_and_shows_in_the_preview(client: TestClient) -> None:
    from outriggarr.source import VideoRef

    _seed_series(client)
    source = client.app.state.source
    source.recent = [
        VideoRef("a", "Scam School 1: Fire", "https://y/a", 100, 1, None, approx_age="3 years ago"),
        VideoRef("b", "Other Show: Fire", "https://y/b", 100, 2, "20240102"),
        VideoRef("c", "Scam School 2: Ice", "https://y/c", 100, 3, None),
    ]
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    r = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={
            "sources": "https://www.youtube.com/@x",
            "strategies": ["title"],
            "date_tolerance_days": "2",
            "date_offset_days": "0",
            "title_regex": "",
            "title_require": " Scam School ",
            "format": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert client.get(f"/api/subscriptions/{sub_id}").json()["title_require"] == "Scam School"
    page = client.get(f"/subscriptions/{sub_id}").text
    assert 'name="title_require" maxlength="100" value="Scam School"' in page
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "titles must contain “Scam School” · 2 do" in prev
    # the listing says what the page said about age, marked as a guess; a real date is a date
    assert "~3 years ago" in prev and "20240102" not in prev and "2024-01-02" in prev


def test_activity_reads_a_failed_job_in_plain_words(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus, TargetKind

    client.post("/api/connections", json=SONARR)
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[11],
                target_key="episode:5:11",
                video_id="gone",
                video_url="https://y/gone",
                video_title="x",
                target_label="Show S30E06 - Six",
                status=JobStatus.failed,
                error="ERROR: [youtube] gone: Video unavailable",
            )
        )
        s.commit()
    page = client.get("/activity?view=failed").text
    assert '<span class="cause">The video is gone from YouTube' in page
    assert "Pin another upload to the episode." in page
    # a finished job's note (an audio-tag hiccup) is not a failure and gets no reading
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[12],
                target_key="episode:5:12",
                video_id="fine",
                video_url="https://y/fine",
                video_title="y",
                target_label="Show S30E07 - Seven",
                status=JobStatus.done,
                error="audio language tag failed (file imported untagged): ffmpeg exited 1",
            )
        )
        s.commit()
    assert client.get("/activity?view=done").text.count('class="cause"') == 0


def test_format_preset_pickers_follow_the_text(client: TestClient) -> None:
    from outriggarr.settings import FORMAT_PRESETS, set_setting

    best = next(p for p in FORMAT_PRESETS if p.key == "best")
    page = client.get("/settings").text
    assert 'select data-fills="default_format"' in page and "data-fills" in page
    assert "selected>Up to 1080p · H.264 + AAC (direct play)</option>" in page, "the default preset"
    assert 'value="__custom__" >Custom' in page, "custom is not selected while the text is a preset"
    with client.app.state.session_factory() as s:
        set_setting(s, "default_format", "bestvideo[height<=600]+bestaudio")
        s.commit()
    page = client.get("/settings").text
    assert 'value="__custom__" selected>Custom' in page and "selected>Up to 1080p" not in page
    assert 'name="default_format" value="bestvideo[height&lt;=600]+bestaudio"' in page  # escaped

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    page = client.get(f"/subscriptions/{sub_id}").text
    assert 'select data-fills="format"' in page and 'value="" selected>Global default' in page
    body = client.get(f"/api/subscriptions/{sub_id}").json()
    body["format"] = best.format
    assert client.put(f"/api/subscriptions/{sub_id}", json=body).status_code == 200
    page = client.get(f"/subscriptions/{sub_id}").text
    assert "selected>Best available · no cap</option>" in page and 'value="" selected' not in page
    body["format"] = "worst"
    assert client.put(f"/api/subscriptions/{sub_id}", json=body).status_code == 200
    page = client.get(f"/subscriptions/{sub_id}").text
    assert 'value="__custom__" selected>Custom' in page and 'name="format" value="worst"' in page


def test_activity_reads_as_a_diary(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.db.models import Job, JobStatus, TargetKind

    client.post("/api/connections", json=SONARR)
    now = datetime.now(UTC)
    with client.app.state.session_factory() as s:
        for i, (status, finished) in enumerate(
            (
                (JobStatus.done, now),
                (JobStatus.done, now - timedelta(days=1)),
                (JobStatus.queued, None),
            )
        ):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=None,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[10 + i],
                    target_key=f"episode:5:{10 + i}",
                    video_id=f"v{i}",
                    video_url=f"https://y/v{i}",
                    video_title=f"Video {i}",
                    target_label=f"Show S30E0{i}",
                    status=status,
                    finished_at=finished,
                    progress_pct=100 if status is JobStatus.done else 0,
                )
            )
        s.commit()
    page = client.get("/activity").text
    body = page.split("<tbody>")[1]
    assert body.count('class="day-group"') == 2 and ">Today<" in body and ">Yesterday<" in body
    assert "<th>Progress</th>" not in page and "100%" not in body, (
        "a finished job's 100% is not news"
    )
    assert "· subscription" not in body and "queued by hand" in body, (
        "the provenance is a hover, not a line"
    )
    assert body.count('class="chip" title="Queued from Grab') == 3
    assert 'class="job-id mono">#' in body and "status-queued" in body


def test_matches_all_view_is_capped_with_a_show_all_switch(client: TestClient, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.db.models import Job, TargetKind
    from outriggarr.web import pages

    monkeypatch.setattr(pages, "MATCHES_PAGE", 2)
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    now = datetime.now(UTC)
    with client.app.state.session_factory() as s:
        for i in range(3):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=sub_id,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[20 + i],
                    target_key=f"episode:5:{20 + i}",
                    video_id=f"m{i}",
                    video_url=f"https://y/m{i}",
                    video_title=f"Match {i}",
                    target_label=f"Show S31E0{i} - Ep {i}",
                    matched_by="exact",
                    video_duration=1500,
                    target_runtime=25,
                    created_at=now - timedelta(minutes=i),
                )
            )
        s.commit()
    page = client.get("/matches?view=all").text
    assert (
        "<th>Evidence</th>" in page and "<th>Status</th>" not in page and "<th>How</th>" not in page
    )
    assert page.count("1500") == 0 and page.count("25m00s vs 25 min ✓") == 2, "capped at two rows"
    assert (
        "Showing the newest 2 of 3." in page
        and 'href="/matches?view=all&limit=all">Show all 3</a>' in page
    )
    everything = client.get("/matches?view=all&limit=all").text
    assert everything.count("25m00s vs 25 min ✓") == 3 and "Show all" not in everything
    assert "How a pairing leaves that list" in page, "the long explanation folds away"


def test_subscription_page_labels_its_facts_and_orders_the_preview(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from outriggarr.source import VideoRef
    from tests.fakes import FakeArrClient

    now = datetime.now(UTC)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", 2015, 1, True)],
        episodes_by_series={
            5: [EpisodeRef(11, 30, 6, "Six", False, True, now - timedelta(days=3))]
        },
    )
    source = client.app.state.source
    source.recent = [VideoRef("a", "Six", "https://y/a", 100, 1, None)]
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={
            "connection_id": 1,
            "series_id": 5,
            "sources": ["https://www.youtube.com/@x"],
            "strategies": ["title", "date"],
            "auto_download": "all",
        },
    ).json()["id"]
    client.post(f"/api/subscriptions/{sub_id}/scan")
    page = client.get(f"/subscriptions/{sub_id}").text
    head = page.split('id="preview"')[0]
    assert "<dt>Matching</dt>" in head and "pins → title → date" in head
    assert "<dt>Last scan</dt>" in head and "just now" in head
    assert f"subscription {sub_id}" not in head, "the internal id is a hover, not a fact"
    assert f"listing depth · #{sub_id}</span>" in page, "…and lives with the settings"
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert prev.index('class="chips scan-summary"') < prev.index('class="form-actions"'), (
        "what the scan saw comes first, then what you can do about it"
    )
    assert "1 wanted episode already has a job" in prev, "the scan queued it: not a match row now"
    assert (
        '<a href="/activity" class="job-ref">#'
        in client.get(f"/subscriptions/{sub_id}/episodes").text
    )


def test_notify_test_result_is_escaped(client: TestClient) -> None:
    from outriggarr.settings import set_setting

    with client.app.state.session_factory() as s:
        set_setting(s, "apprise_urls", "discord://webhook_id/webhook_token")
        s.commit()
    client.app.state.runner_deps.notifier.error = RuntimeError("<img src=x onerror=alert(1)>")
    r = client.post("/settings/notify/test")
    assert r.status_code == 200 and "<img" not in r.text and "&lt;img" in r.text
    client.app.state.runner_deps.notifier.error = None


def test_subscribe_validation_error_keeps_what_was_typed(client: TestClient) -> None:
    _seed_series(client)
    r = client.post(
        "/series/5/subscribe",
        data={
            "sources": "https://www.youtube.com/@typed",
            "strategies": ["regex", "title"],
            "date_tolerance_days": "3",
            "date_offset_days": "0",
            "title_regex": "#(?P<episode>\\d+)",
            "title_require": "Typed Show",
            "video_limit": "250",
            "audio_language": "nope",  # the one bad field
            "format": "",
        },
    )
    assert r.status_code == 400 and "audio_language" in r.text
    assert "Subscribe: Hot Ones" in r.text, "the header keeps the series title"
    assert "https://www.youtube.com/@typed</textarea>" in r.text
    assert 'value="regex" checked' in r.text and 'value="title" checked' in r.text
    assert 'name="title_require" maxlength="100" value="Typed Show"' in r.text
    assert 'name="video_limit" min="1" max="5000" value="250"' in r.text
    assert 'name="date_tolerance_days" min="0" max="60" value="3"' in r.text


def test_edit_validation_error_keeps_what_was_typed_and_opens_the_panel(client: TestClient) -> None:
    from outriggarr.db.models import Job, TargetKind

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@stored"]},
    ).json()["id"]
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                subscription_id=sub_id,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[11],
                target_key="episode:5:11",
                video_id="j",
                video_url="https://y/j",
                video_title="Recent",
                target_label="Show S30E06 - Six",
            )
        )
        s.commit()
    r = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={
            "sources": "https://www.youtube.com/@typed",
            "strategies": ["title"],
            "date_tolerance_days": "x",  # the bad field: not a number
            "date_offset_days": "0",
            "title_regex": "",
            "title_require": "",
            "format": "",
        },
    )
    assert r.status_code == 400 and "Date tolerance must be a whole number" in r.text
    assert (
        "@typed</textarea>" in r.text
        and "@stored" not in r.text.split("<textarea")[1].split("</textarea>")[0]
    )
    assert '<details class="panel" open>' in r.text, "the form the user filled stays open"
    assert "Recent" in r.text, "the recent jobs are still shown"


def test_activity_action_notices_survive_the_poll(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus, TargetKind

    client.post("/api/connections", json=SONARR)
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[11],
                target_key="episode:5:11",
                video_id="q",
                video_url="https://y/q",
                video_title="x",
                target_label="Show S30E06",
                status=JobStatus.queued,
            )
        )
        s.commit()
    r = client.post("/activity/jobs/1/retry?view=all")  # queued: not retryable
    assert 'id="jobs-notice" hx-swap-oob="true" class="notice bad"' in r.text
    assert "jobs-notice" not in client.get("/activity/rows?view=all").text, (
        "the poll does not carry (and so cannot erase) the notice"
    )
    assert '<div id="jobs-notice"></div>' in client.get("/activity").text
    ok = client.post("/activity/jobs/1/cancel?view=all")
    assert 'class="notice" role="status"' in ok.text and "Job 1 cancelled." in ok.text


def test_preview_query_count_does_not_grow_with_the_listing(client: TestClient) -> None:
    from sqlalchemy import event

    from outriggarr.source import VideoRef

    _seed_series(client)
    source = client.app.state.source
    source.recent = [
        VideoRef(f"v{i}", f"Video {i}", f"https://y/v{i}", 100, i, None) for i in range(300)
    ]
    sub_id = client.post(
        "/api/subscriptions",
        json={
            "connection_id": 1,
            "series_id": 5,
            "sources": ["https://www.youtube.com/@x"],
            "strategies": ["title", "date"],
        },
    ).json()["id"]
    statements: list[str] = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", spy)
    try:
        client.get(f"/subscriptions/{sub_id}/preview")
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) < 40, f"{len(selects)} SELECTs for 300 listed videos"


def test_day_labels_follow_the_local_clock() -> None:
    from datetime import UTC, datetime, timedelta, timezone

    from outriggarr.web.pages import day_label

    new_york = timezone(timedelta(hours=-4))
    now = datetime(2026, 9, 3, 0, 30, tzinfo=UTC)  # 20:30 in New York
    an_hour_ago = now - timedelta(hours=1)
    assert day_label(an_hour_ago, now=now, tz=new_york) == "Today"
    assert day_label(an_hour_ago, now=now, tz=UTC) == "Yesterday", "UTC would cut the day here"
    assert day_label(now - timedelta(days=1), now=now, tz=new_york) == "Yesterday"


def test_matches_show_all_survives_an_action(client: TestClient, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.db.models import Job, TargetKind
    from outriggarr.web import pages

    monkeypatch.setattr(pages, "MATCHES_PAGE", 1)
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    now = datetime.now(UTC)
    with client.app.state.session_factory() as s:
        for i in range(2):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=sub_id,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[20 + i],
                    target_key=f"episode:5:{20 + i}",
                    video_id=f"m{i}",
                    video_url=f"https://y/m{i}",
                    video_title=f"Match {i}",
                    target_label=f"Show S31E0{i}",
                    matched_by="contains",
                    created_at=now - timedelta(minutes=i),
                )
            )
        s.commit()
    with client.app.state.session_factory() as s:
        job_id = s.query(Job).first().id
    r = client.post(f"/matches/{job_id}/confirm?view=all&limit=all")
    assert r.text.count('title="Match ') == 2 and "limit=all" in r.text and "Show all" not in r.text
    r = client.post(f"/matches/{job_id}/unconfirm?view=all")
    assert r.text.count('title="Match ') == 1 and "Show all 2" in r.text


def test_error_details_are_keyed_by_attempt_and_tabs_are_links(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus, TargetKind

    client.post("/api/connections", json=SONARR)
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[11],
                target_key="episode:5:11",
                video_id="e",
                video_url="https://y/e",
                video_title="x",
                target_label="Show S30E06",
                status=JobStatus.failed,
                attempts=2,
                error="ERROR: second try",
            )
        )
        s.commit()
    page = client.get("/activity?view=failed").text
    assert 'id="err-1-2" hx-preserve' in page, "a new attempt's text replaces the preserved node"
    assert 'role="tab"' not in page and 'aria-current="page"' in page


def test_listed_videos_panel_is_capped(client: TestClient) -> None:
    from outriggarr.source import VideoRef
    from outriggarr.web.pages import templates

    _seed_series(client)
    source = client.app.state.source
    source.recent = [
        VideoRef(f"v{i}", f"Video {i}", f"https://y/v{i}", 100, i, None) for i in range(7)
    ]
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "sources": ["https://www.youtube.com/@x"]},
    ).json()["id"]
    templates.env.globals["LISTED_VIDEOS_SHOWN"] = 5
    try:
        prev = client.get(f"/subscriptions/{sub_id}/preview").text
    finally:
        templates.env.globals["LISTED_VIDEOS_SHOWN"] = 100
    listed = prev.split("<summary>7 videos listed")[1]
    assert listed.count("<li>") == 5 and "…and 2 more" in listed
    assert prev.count('<option value="https://y/v') == 7, "the pin picker still offers every one"


def test_templates_refuse_undefined_names() -> None:
    # a renamed context key must be an error in the suite, not a blank in production
    from jinja2 import StrictUndefined, UndefinedError

    from outriggarr.web.pages import templates

    assert templates.env.undefined is StrictUndefined
    with pytest.raises(UndefinedError):
        templates.env.from_string("{{ nobody_passes_this }}").render()
    assert templates.env.from_string("{{ notice }}|{{ notice_bad }}").render() == "None|False", (
        "the optional notice keys default; everything else must be passed"
    )
