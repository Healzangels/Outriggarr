from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

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
    # an empty view says what is empty; the advice to queue something belongs to "all"
    done = client.get("/activity/rows?view=done").text
    assert "Nothing finished yet." in done and "Queue one from" not in done
    assert "Nothing running." in client.get("/activity/rows?view=active").text
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
    assert client.get("/series/rows").text.strip() == "", "nothing typed: nothing to say"
    rows = client.get("/series/rows?q=hot").text
    assert "Hot Ones" in rows and "Hot Zone" in rows and 'href="/series/5/subscribe"' in rows

    form = client.get("/series/5/subscribe")
    assert form.status_code == 200 and "Subscribe · Hot Ones" in form.text
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
    assert "Subscribed ✓" in client.get("/series/rows?q=hot%20ones").text
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
    assert "Refresh preview" in prev_buttons(client, sub_id) and "Download" in prev_buttons(
        client, sub_id
    )

    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "Nothing here queues by itself" in prev and "S30E06" in prev  # a match, no job yet
    assert "S30E07" in prev and "No strategy saw a candidate for any of these" in prev
    assert f'hx-post="/subscriptions/{sub_id}/overrides"' in prev

    r = client.post(
        f"/subscriptions/{sub_id}/overrides", data={"video_id": "b", "season": "30", "episode": "7"}
    )
    assert r.status_code == 200 and "Pinned b to S30E07" in r.text
    assert '<span class="muted mono">b</span>' in r.text and "S30E07" in r.text

    scan = client.post(f"/subscriptions/{sub_id}/scan")
    assert scan.status_code == 200 and "Nothing queued" in scan.text
    assert "2 matched" in scan.text and "Download all 2" in scan.text
    assert client.get("/api/jobs").json() == [], "Refresh preview never queues"
    dl = client.post(f"/subscriptions/{sub_id}/download")
    assert dl.status_code == 200 and "Queued 2 jobs;" in dl.text
    assert len(client.get("/api/jobs").json()) == 2
    again = client.post(f"/subscriptions/{sub_id}/download")
    assert "Nothing to queue" in again.text and "Download all" not in again.text
    assert "already have jobs" in client.get(f"/subscriptions/{sub_id}/preview").text

    r = client.post(f"/subscriptions/{sub_id}/overrides/b/delete")
    assert r.status_code == 200 and "Pin removed: b" in r.text

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
    assert r.status_code == 200 and "Add a connection" in r.text and "Default quality" in r.text
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
    assert "1/3 files" in html and "1 missing" in html and "1 unmonitored" in html
    assert "1/1 files" in html, "every season has its own row, complete ones too"
    assert "complete season" not in html
    # the newest season opens by itself only while something in it is missing
    assert '<details class="plain season" open>' in html and "<summary>Season 30" in html
    assert '<details class="plain season">' in html and "<summary>Season 29" in html
    assert html.count('season" open>') == 1, "only the newest season with missing episodes opens"
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
    assert "Pinned Seven, older upload to S30E07 (from URL)" in r.text
    assert ">via URL<" in r.text and "S30E07" in r.text
    assert 'aria-label="download S30E07"' in r.text  # S30E07 now matches via the override
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
    assert r.status_code == 200 and f"Job #{job_id} deleted" in r.text
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
    # the page open shows the cached look, age and all; Refresh preview is what re-lists
    assert "may not carry these episodes" in client.get(f"/subscriptions/{sub_id}/preview").text
    assert "may not carry these episodes" not in client.post(f"/subscriptions/{sub_id}/scan").text


def test_grab_marks_unavailable_videos(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    page = client.get("/grab").text
    assert "unavailable video" in page and "include: v.title !== v.id" in page
    assert ":aria-label=\"'season for '" in page and "htmx:responseError" in page


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
    assert "1 held" in prev and "video runs 1:30, Sonarr says the episode runs 25 min" in prev
    assert "Looks right, pin it" in prev and "select to download" not in prev  # held: no row
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
    assert 'name="episode_id"' in r.text  # pinned: a plain match row now
    page = client.get("/matches?view=review").text
    assert "Nothing needs a look" in page
    client.post(f"/subscriptions/{sub_id}/download")
    (job,) = client.get("/api/jobs").json()
    assert (job["matched_by"], job["video_duration"], job["target_runtime"]) == ("override", 90, 25)
    page = client.get("/matches?view=review").text
    assert "Six Spicy Wings" in page and "1:30 vs 25:00 ✗" in page, "flagged even though pinned"
    assert 'You pinned this video to the episode.">pinned</span>' in page


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
    assert "Six Spicy Wings" in page and 'inside the video title.">title contains</span>' in page
    assert f'href="/subscriptions/{sub_id}"' in page
    assert "Needs a look" in page and "Matches" in client.get("/activity").text  # nav link
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
    assert "likely by date" in page and "Worked out afterwards" in page


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
        'Needs a look<span class="count warn">4</span>' in page
        and page.count("length unchecked") == 4
    )
    assert 'aria-current="page">Needs a look' in page, "work to do: land on the review view"

    import time

    r = client.post("/matches/recheck")
    assert r.status_code == 200, r.text
    assert 'hx-get="/matches/content?view=review" hx-trigger="every 2s [' in r.text, "polls itself"
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
        "Checked 4 matches: 3 video lengths and 3 runtimes fetched; 1 contradict their runtime."
        in r.text
    )
    assert "1 could not be fetched" in r.text
    assert 'Needs a look<span class="count warn">3</span>' in r.text, (
        "25 min vs 25 min cleared itself"
    )
    assert "Checked 4 matches" not in client.get("/matches").text, "the summary shows once"
    assert "2 unchecked" in r.text, "the button says how much is left"
    assert 'disabled title="Every match' not in r.text, "still something to check: enabled"
    jobs = {j["video_id"]: j for j in client.get("/api/jobs").json()}
    assert jobs["double"]["target_runtime"] is None, "a half-known runtime is no evidence"
    assert "50:00, no runtime in Sonarr" in r.text
    assert "2:00 vs 25:00 ✗" in r.text
    assert "25:00 vs 25:00 ✓" in client.get("/matches?view=all").text
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
    assert 'Needs a look<span class="count warn">2</span>' in r.text
    assert client.get(f"/api/jobs/{short_id}").json()["reviewed_at"] is not None
    r = client.post(f"/matches/{short_id}/unconfirm?view=review")
    assert 'Needs a look<span class="count warn">3</span>' in r.text
    r = client.post("/matches/confirm-all")
    assert "Confirmed 3 matches." in r.text and 'Needs a look<span class="count">0</span>' in r.text
    assert client.get("/matches?view=all").text.count(">confirmed</span>") == 3
    # nothing left to look at: the page lands on "all" and the recheck button is inert
    landing = client.get("/matches").text
    assert (
        'aria-current="page">All<' in landing and 'aria-current="page">Needs a look' not in landing
    )
    assert "Every match already has its length evidence" in landing
    assert '<span class="muted">Every match already has its length evidence.</span>' in landing
    assert 'hx-post="/matches/recheck' not in landing, (
        "no dead disabled button; the reason stands in its place"
    )
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
    assert "S30E06" in prev and "No strategy saw a candidate" in prev, (
        "still unmatched, nothing seen"
    )
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
    assert "0 of 1 would queue by itself" in prev and "Nothing here queues by itself" in prev
    assert 'name="episode_id" value="11"' in prev and "Download selected" in prev
    assert 'id="selected-count" hidden>0</span>' in prev and 'id="tick-all"' in prev, (
        "a live count, hidden while nothing is ticked"
    )
    untouched = 'name="episode_id" value="11" aria-label="download S30E06">'
    assert untouched in prev, "unselected by default"
    assert "picks.addEventListener('change', update)" in prev
    # ticking nothing queues nothing; ticking S30E06 queues just that; the scheduler alone would not
    r = client.post(f"/subscriptions/{sub_id}/download", data={"selected": "1"})
    assert "Nothing selected." in r.text and client.get("/api/jobs").json() == []
    r = client.post(
        f"/subscriptions/{sub_id}/download", data={"selected": "1", "episode_id": ["11"]}
    )
    assert "Queued 1 job;" in r.text
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
    assert f'aria-label="delete job {stale}"' in panel, "missing + done job: clearable"
    assert f'aria-label="delete job {live}"' not in panel, "a live job is not history"
    assert panel.count("x-clear") == 1
    r = client.post(f"/subscriptions/{sub_id}/episodes/jobs/{stale}/clear")
    assert r.status_code == 200 and f"Deleted job #{stale}." in r.text
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
        '<th scope="col">Evidence</th>' in page
        and "<th>Status</th>" not in page
        and "<th>How</th>" not in page
    )
    assert page.count("1500") == 0 and page.count("25:00 vs 25:00 ✓") == 2, "capped at two rows"
    assert (
        "Showing the newest 2 of 3." in page
        and page.index("Showing the newest 2 of 3.") > page.index("</table>")
        and 'href="/matches?view=all&amp;limit=all">Show all 3</a>' in page
    )
    everything = client.get("/matches?view=all&limit=all").text
    assert everything.count("25:00 vs 25:00 ✓") == 3 and "Show all" not in everything
    assert "How a match leaves that list" in page, "the long explanation folds away"


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
    assert "<dt>Match order</dt>" in head and "pins → title → date" in head
    assert "<dt>Last scan</dt>" in head and "just now" in head
    assert f"subscription {sub_id}" not in head, "the internal id is a hover, not a fact"
    assert f"listing depth · #{sub_id}</span>" in page, "…and lives with the settings"
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert prev.index('class="chips scan-summary"') < prev.index('class="form-actions"'), (
        "what the scan saw comes first, then what you can do about it"
    )
    assert "Listed just now" in prev, "the preview says how old its look at the source is"
    fresh = client.post(f"/subscriptions/{sub_id}/scan").text  # Refresh preview
    assert "1 wanted episode already has a job" in fresh, "the scan queued it: not a match row now"
    assert '<a href="/activity#job-' in client.get(f"/subscriptions/{sub_id}/episodes").text


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
    assert "Subscribe · Hot Ones" in r.text, "the header keeps the series title"
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
    assert '<details class="panel" id="settings" open>' in r.text, (
        "the form the user filled stays open"
    )
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
    assert 'id="jobs-notice" hx-swap-oob="innerHTML"><p class="notice bad" role="alert"' in r.text
    assert "jobs-notice" not in client.get("/activity/rows?view=all").text, (
        "the poll does not carry (and so cannot erase) the notice"
    )
    assert (
        '<div id="jobs-notice" role="status" aria-live="polite"></div>'
        in client.get("/activity").text
    )
    ok = client.post("/activity/jobs/1/cancel?view=all")
    assert 'hx-swap-oob="innerHTML"><p class="notice">Job #1 cancelled.</p>' in ok.text


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
    listed = prev.split("<summary>The listing")[1]
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


def test_ago_units_read_evenly() -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.web.pages import ago

    now = datetime(2026, 9, 5, 18, 40, tzinfo=UTC)  # a Saturday evening
    assert ago(now - timedelta(minutes=12), now=now, tz=UTC) == "12 min ago"
    assert ago(now - timedelta(hours=16), now=now, tz=UTC) == "16 hr ago"
    assert ago(now - timedelta(days=3), now=now, tz=UTC) == "3 days ago"
    assert ago(now + timedelta(hours=2, minutes=1), now=now, tz=UTC) == "in 2 hr"
    assert ago(now + timedelta(days=2, minutes=1), now=now, tz=UTC) == "in 2 days"
    assert ago(datetime.now(UTC) - timedelta(minutes=12)) == "12 min ago", "the clock defaults"


def test_ago_counts_the_same_days_the_activity_headers_do() -> None:
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from outriggarr.web.pages import ago, day_label

    now = datetime(2026, 9, 5, 18, 40, tzinfo=UTC)  # Saturday
    thursday_night = datetime(2026, 9, 3, 22, 47, tzinfo=UTC)  # 44 h back
    # flooring 44 h says "1 day ago", which under a "Thu 3 Sep" header on a
    # Saturday reads as Friday; the calendar says two days
    assert day_label(thursday_night, now=now, tz=UTC) == "Thu 3 Sep"
    assert ago(thursday_night, now=now, tz=UTC) == "2 days ago"
    assert ago(datetime(2026, 9, 4, 2, 0, tzinfo=UTC), now=now, tz=UTC) == "1 day ago"
    assert ago(now + timedelta(hours=26), now=now, tz=UTC) == "in 1 day"
    # the days are the container's local days, as day_label's are
    ny = ZoneInfo("America/New_York")
    late = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)  # Friday 23:00 in New York
    assert ago(thursday_night, now=late, tz=UTC) == "2 days ago"
    assert ago(thursday_night, now=late, tz=ny) == "1 day ago"


def test_same_title_ignores_only_the_channels_own_prefix() -> None:
    from outriggarr.web.pages import same_title

    assert same_title("Scam School S01E05 - How to Win a Bar Bet", "How to Win a Bar Bet!")
    assert same_title("A - B S01E02 - Title", "Title"), "a dash in the series name is not the split"
    assert same_title("Show S01E01-E02 - Two Parts", "Two Parts"), "a multi-episode code"
    assert not same_title(
        "Hot Ones S30E07 - Penélope Cruz Laughs", "Penélope Cruz Laughs While Eating Spicy Wings"
    ), "containing is not the same: the extra words may be what shows a wrong match"
    kt = same_title("Kill Tony S2026E33 - #783 - GARY OWEN", "KT #783 - GARY OWEN")
    assert kt and "“KT” prefix: KT #783 - GARY OWEN" in kt, "the channel's initials are its tag"
    assert not same_title("Show S01E01", "anything"), "no title part to compare"
    assert not same_title(None, "x") and not same_title("Show S01E01 - x", None)
    from outriggarr.web.pages import titles_match

    assert titles_match("Seven Spicy Wings", "seven spicy wings!")
    assert not titles_match("", "x") and not titles_match("x", None)
    assert not titles_match("...", "!!!"), (
        "two titles that normalise to nothing are not the same title"
    )
    assert titles_match("Title", "Kill Tony - Title", "Kill Tony"), "the full name too"
    assert titles_match("Title", "kill tony: Title", "Kill Tony")
    assert not titles_match("#783 - X", "KT #783 - X"), "no series named: no prefix to forgive"
    assert not titles_match("Title", "KTown Title", "Kill Tony"), "initials need a separator"
    assert not titles_match("Title", "KTTitle", "Kill Tony"), "the tag ends at a boundary"
    assert not titles_match("Title", "Title | Kill Tony", "Kill Tony"), "a trailing tag stays"
    assert not titles_match("Title", "M Title", "Monstrum"), "a one-word name has no initialism"


def test_recent_jobs_say_same_title_once(client: TestClient) -> None:
    from outriggarr.db.models import TargetKind

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    with client.app.state.session_factory() as s:
        for vid, label, title in (
            ("same", "Hot Ones S30E07 - Seven Spicy Wings", "Seven Spicy Wings"),
            ("more", "Hot Ones S30E08 - Eight Spicy Wings", "Eight Spicy Wings | Hot Ones"),
        ):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=sub_id,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[int(vid == "more") + 7],
                    target_key=f"episode:5:{vid}",
                    video_id=vid,
                    video_url=f"https://y/{vid}",
                    video_title=title,
                    target_label=label,
                )
            )
        s.commit()
    recent = client.get(f"/subscriptions/{sub_id}/recent").text
    assert (
        'class="truncate same-title" '
        'title="The video is titled as the episode is: Seven Spicy Wings">same title</span>'
        in recent
    )
    assert ">Seven Spicy Wings</span>" not in recent
    assert ">Eight Spicy Wings | Hot Ones</span>" in recent, "a title that says more stays in full"
    # the preview's matched table says it the same way
    from outriggarr.source import VideoRef

    client.app.state.source.recent = [
        VideoRef("a", "HO Six Spicy Wings", "https://y/a", 1, 1, None),
        VideoRef("c", "Seven Spicy Wings", "https://y/c", 1, 3, None),
    ]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert (
        'class="truncate same-title" '
        'title="The video is titled as the episode is: Seven Spicy Wings">same title</a>' in prev
    )
    assert '“HO” prefix: HO Six Spicy Wings">same title</a>' in prev, "the preview knows the series"
    picks = prev.split('<tbody id="match-picks">', 1)[1].split("</tbody>", 1)[0]
    assert ">HO Six Spicy Wings</a>" not in picks, (
        "the listing panel may show it; the match row does not"
    )


def test_video_column_says_same_title_once(client: TestClient) -> None:
    conn_id = client.post("/api/connections", json=SONARR).json()["id"]

    def post(label: str, title: str, vid: str, ep: int) -> None:
        r = client.post(
            "/api/jobs",
            json=[
                {
                    "connection_id": conn_id,
                    "target": {
                        "kind": "episode",
                        "series_id": 5,
                        "episode_ids": [ep],
                        "label": label,
                    },
                    "video": {
                        "url": f"https://youtube.invalid/watch?v={vid}",
                        "id": vid,
                        "title": title,
                    },
                }
            ],
        )
        assert r.status_code in (200, 201), r.text

    post("Scam School S01E05 - How to Win a Bar Bet", "How to Win a Bar Bet!", "same1", 5)
    post(
        "Hot Ones S30E07 - Penélope Cruz Laughs",
        "Penélope Cruz Laughs While Eating Spicy Wings",
        "more2",
        7,
    )
    post("Kill Tony S2026E33 - #783 - GARY OWEN", "KT #783 - GARY OWEN", "kt3", 33)
    page = client.get("/activity").text
    assert ">same title</a>" in page and 'href="https://youtube.invalid/watch?v=same1"' in page, (
        "the link stays; its text does not repeat the Episode column"
    )
    assert ">How to Win a Bar Bet!</a>" not in page
    assert ">Penélope Cruz Laughs While Eating Spicy Wings</a>" in page, (
        "a title that says more is the evidence and stays in full"
    )
    assert '“KT” prefix: KT #783 - GARY OWEN">same title</a>' in page, "the tooltip names the tag"
    assert ">KT #783 - GARY OWEN</a>" not in page


def test_tier_label_speaks_the_page_language() -> None:
    from outriggarr.web.pages import tier_label

    assert tier_label("override") == "pinned"
    assert tier_label("exact") == "exact title"
    assert tier_label("numbered") == "show number"
    assert tier_label("contains") == "title contains"
    assert tier_label("date") == "by date"
    assert tier_label(None) == "unknown"
    assert tier_label("something-new") == "something-new", "an unmapped tier still shows"


def test_settings_saved_notice_names_the_form(client: TestClient) -> None:
    r = client.post(
        "/settings/downloads",
        data={"_notify_form": "1", "apprise_urls": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=notifications#notifications"
    assert "Notifications saved." in client.get("/settings?saved=notifications").text
    assert "Downloads saved." in client.get("/settings?saved=downloads").text
    assert "Connection saved." in client.get("/settings?saved=connection").text
    assert "saved" not in client.get("/settings").text.split("<h1>Settings</h1>")[1][:200]


def test_matches_rank_needs_a_look_first_and_confirmed_rows_sink(client: TestClient) -> None:
    # A confirmed length mismatch used to stay pinned at the top of "All" forever: the
    # contradiction still counted as risk. Once vouched for or confirmed, a row is
    # history and takes its place by date under the rows that still need a look.
    from datetime import UTC, datetime, timedelta

    from outriggarr.db.models import Job, TargetKind

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    now = datetime.now(UTC)
    rows = (  # oldest first
        ("A", "exact", 1500, None),  # vouched by its title
        ("B", "contains", None, None),  # length unchecked: needs a look
        ("C", "contains", 120, now),  # length contradicts the runtime, but confirmed
        ("D", "exact", 1500, None),  # newest, vouched
    )
    with client.app.state.session_factory() as s:
        for age, (name, tier, duration, reviewed) in zip(range(4, 0, -1), rows, strict=True):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=sub_id,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[10 + age],
                    target_key=f"episode:5:{10 + age}",
                    video_id=name.lower(),
                    video_url=f"https://y/{name.lower()}",
                    video_title=f"Video {name}",
                    target_label=f"Show S30E0{age} - Row {name}",
                    matched_by=tier,
                    target_runtime=25,
                    video_duration=duration,
                    reviewed_at=reviewed,
                    created_at=now - timedelta(days=age),
                )
            )
        s.commit()
    page = client.get("/matches?view=all").text
    order = sorted("ABCD", key=lambda n: page.index(f"Row {n}"))
    assert order == ["B", "D", "C", "A"], order
    review = client.get("/matches?view=review").text
    assert "Row B" in review and not any(f"Row {n}" in review for n in "ACD")


def test_matches_show_one_row_per_target_the_newest_job(client: TestClient) -> None:
    # A re-download of the same episode (say at a higher quality) made the episode
    # appear twice with identical evidence; only the newest job for a target is shown
    from datetime import UTC, datetime, timedelta

    from outriggarr.db.models import Job, JobStatus, TargetKind

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    now = datetime.now(UTC)
    with client.app.state.session_factory() as s:
        # only a finished job can share its target and video with a newer one
        for name, age, duration in (("Old", 2, None), ("New", 1, 1500)):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=sub_id,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[11],
                    target_key="episode:5:11",
                    video_id="same",
                    video_url="https://y/same",
                    video_title=f"Video {name}",
                    target_label="Show S30E06 - Six",
                    matched_by="contains",
                    target_runtime=25,
                    video_duration=duration,
                    created_at=now - timedelta(days=age),
                    status=JobStatus.done if name == "Old" else JobStatus.queued,
                )
            )
        s.commit()
    page = client.get("/matches?view=all").text
    assert "Video New" in page and "Video Old" not in page
    assert page.count("<strong>Show S30E06</strong> Six") == 1
    assert 'All<span class="count">1</span>' in page
    assert 'Needs a look<span class="count">0</span>' in page, "the superseded job's gap is moot"


def test_settings_errors_keep_typed_values_and_sit_in_the_failing_card(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    r = client.post(
        "/settings/downloads",
        data={"ytdlp_extra_opts": "{bad", "cookies_path": "/typed/cookies.txt"},
    )
    assert r.status_code == 400
    assert 'value="/typed/cookies.txt"' in r.text, "a typo elsewhere must not empty the form"
    assert "{bad" in r.text and 'class="notice bad"' in r.text
    r = client.post(
        "/settings/connections/1",
        data={
            "kind": "sonarr",
            "name": "Typed Name",
            "url": "not a url",
            "api_key": "typed-secret",
            "staging_path_remote": "/data/x",
        },
    )
    assert r.status_code == 400
    card = r.text.split('action="/settings/connections/1"')[1].split("</article>")[0]
    assert 'value="Typed Name"' in card and 'class="notice bad"' in card, "error inside the card"
    assert "typed-secret" not in r.text and "k1" not in r.text, "keys are never echoed"
    assert 'id="downloads"' in r.text and 'id="notifications"' in r.text


def test_matches_chip_colour_follows_need_not_tier(client: TestClient) -> None:
    from datetime import UTC, datetime

    from outriggarr.db.models import Job, JobStatus, TargetKind

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    with client.app.state.session_factory() as s:
        for eid, duration, reviewed in ((11, 1500, None), (12, 120, datetime.now(UTC))):
            s.add(
                Job(
                    connection_id=1,
                    subscription_id=sub_id,
                    target_kind=TargetKind.episode,
                    series_id=5,
                    episode_ids=[eid],
                    target_key=f"episode:5:{eid}",
                    video_id=f"v{eid}",
                    video_url=f"https://y/v{eid}",
                    video_title=f"Video {eid}",
                    target_label=f"Show S30E{eid - 5:02d} - T{eid}",
                    matched_by="contains",
                    target_runtime=25,
                    video_duration=duration,
                    reviewed_at=reviewed,
                    status=JobStatus.done,
                )
            )
        s.commit()
    page = client.get("/matches?view=all").text
    rows = page.split("<tbody>")[1].split("</tbody>")[0].split("<tr")[1:]
    ok_row = next(r for r in rows if "Video 11" in r)
    confirmed_row = next(r for r in rows if "Video 12" in r)
    assert '<span class="chip " title=' in ok_row, "length vouches: no orange on a contains tier"
    assert 'class="chip warn"' not in ok_row and 'class="chip warn"' not in confirmed_row
    assert ">confirmed</span> <form" in confirmed_row, "the chip sits beside its Undo"
    assert ">Wrong video?</a>" in ok_row and "reappears under Unmatched" in ok_row


def test_preview_says_so_when_sonarr_wants_nothing(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from tests.fakes import FakeArrClient

    aired = datetime.now(UTC) - timedelta(days=1)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Done Show", 2015, 1, True)],
        episodes_by_series={5: [EpisodeRef(11, 1, 1, "One", True, True, aired)]},
    )
    client.app.state.source.recent = []
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@done"},
    ).json()["id"]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "Sonarr wants nothing from this series right now" in prev
    assert "None of the" not in prev, "not the wrong-source warning"


def test_series_rows_say_nothing_new_and_have_no_open_button(client: TestClient) -> None:
    from datetime import UTC, datetime

    from outriggarr.db.models import Subscription

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    with client.app.state.session_factory() as s:
        sub = s.get(Subscription, sub_id)
        sub.last_scan_at = datetime.now(UTC)
        sub.last_scan_result = {"matched": 0, "created": 0, "unmatched": 0, "error": None}
        s.commit()
    page = client.get("/series").text
    assert ">nothing new</span>" in page and "0 matched" not in page
    assert ">Open</a>" not in page, "the title is already the link"
    with client.app.state.session_factory() as s:
        sub = s.get(Subscription, sub_id)
        sub.last_scan_result = {"matched": 3, "created": 0, "unmatched": 1, "error": None}
        s.commit()
    page = client.get("/series").text
    assert '<span class="chip ok">3 matched</span>' in page and "1 unmatched" in page


@pytest.mark.parametrize(
    ("minutes_ago", "interval", "expect"),
    [
        (None, 30, "first scan due soon"),
        (10, 30, "next in ~20 min"),
        (31, 30, "next scan due now"),
        (60, 720, "next in ~11 hr"),
        (30, 720, "next in ~12 hr"),  # 11.5 h rounds half up, like the ago filter
        (60, 24 * 60 * 3, "next in ~2 d"),
    ],
)
def test_next_scan_text(minutes_ago: int | None, interval: int, expect: str) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.web.pages import next_scan_text

    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    last = None if minutes_ago is None else now - timedelta(minutes=minutes_ago)
    assert next_scan_text(last, interval, now) == expect


def test_activity_empty_state_points_a_fresh_install_at_settings(client: TestClient) -> None:
    page = client.get("/activity").text
    assert "Start by connecting Sonarr or Radarr" in page and 'href="/settings"' in page
    client.post("/api/connections", json=SONARR)
    page = client.get("/activity").text
    assert "Start by connecting" not in page and 'href="/grab"' in page


def test_download_tells_the_page_its_other_cards_are_stale(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    r = client.post(f"/subscriptions/{sub_id}/download")
    assert r.status_code == 200 and r.headers.get("HX-Trigger") == "jobs-changed"
    assert "they show under Recent jobs below and on Activity" in r.text
    again = client.post(f"/subscriptions/{sub_id}/download")
    assert "HX-Trigger" not in again.headers, "nothing queued: nothing to refresh"
    recent = client.get(f"/subscriptions/{sub_id}/recent").text
    assert "<strong>S30E06</strong>" in recent and "Hot Ones S30E06" not in recent
    page = client.get(f"/subscriptions/{sub_id}").text
    assert 'hx-trigger="load, jobs-changed from:body"' in page
    assert 'id="recent-jobs" hx-get="/subscriptions/' in page and "jobs-changed from:body" in page
    assert client.get("/subscriptions/999/recent").status_code == 404


def test_new_connection_is_tested_on_the_page_that_comes_back(client: TestClient) -> None:
    r = client.post(
        "/settings/connections",
        data={
            "kind": "sonarr",
            "name": "Sonarr",
            "url": "http://sonarr-host:1234",
            "api_key": "k1",
            "staging_path_remote": "/data/x",
        },
        follow_redirects=False,
    )
    assert (
        r.status_code == 303
        and r.headers["location"] == "/settings?saved=connection&test=1#connections"
    )
    page = client.get("/settings?saved=connection&test=1").text
    assert "testing it now, the result shows on its card" in page
    assert (
        'id="test-1" role="status" aria-live="polite" '
        'hx-post="/settings/connections/1/test" hx-trigger="load"' in page
    )
    assert 'hx-trigger="load"' not in client.get("/settings").text, "only right after adding one"


def test_subscribe_form_folds_expert_fields_until_one_is_set(client: TestClient) -> None:
    _seed_series(client)
    page = client.get("/series/5/subscribe").text
    assert '<details class="plain more" >' in page or '<details class="plain more">' in page
    assert "More options" in page and 'name="title_regex"' in page
    sub_id = client.post(
        "/api/subscriptions",
        json={
            "connection_id": 1,
            "series_id": 5,
            "source_url": "https://www.youtube.com/@hotones",
            "title_require": "Hot Ones",
        },
    ).json()["id"]
    page = client.get(f"/subscriptions/{sub_id}").text
    assert '<details class="plain more" open>' in page, "a set expert field keeps the section open"
    r = client.post(
        f"/subscriptions/{sub_id}/edit",
        data={
            "sources": "https://www.youtube.com/@hotones",
            "strategies": "title",
            "auto_download": "future",
            "date_tolerance_days": "2",
            "date_offset_days": "0",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=1")
    assert "Subscription saved." in client.get(f"/subscriptions/{sub_id}?saved=1").text


def test_grab_lists_are_keyboard_operable_and_named(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    page = client.get("/grab").text
    assert 'role="combobox"' in page and 'role="listbox"' in page and 'role="option"' in page
    assert (
        "@keydown.down.prevent" in page
        and "@keydown.enter.prevent" in page
        and "@keydown.escape" in page
    )
    assert 'aria-label="Video or playlist URL"' in page and 'role="alert"' in page


def test_activity_poll_waits_while_the_keyboard_is_in_the_table(client: TestClient) -> None:
    page = client.get("/activity").text
    assert "hx-trigger=\"every 3s [!document.activeElement.closest('#jobs')]\"" in page
    assert '<nav class="container" aria-label="Primary">' in page and "<h1>Activity</h1>" in page


def test_a_dead_worker_loop_is_announced_on_every_page(client: TestClient) -> None:
    import asyncio

    async def boom() -> None:
        raise RuntimeError("worker crashed")

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(boom())
        with pytest.raises(RuntimeError):
            loop.run_until_complete(task)
        assert task.done()
        client.app.state.background_tasks = {"worker": task, "scheduler": None}
        page = client.get("/series").text
        assert "The worker loop has stopped" in page and 'role="alert"' in page
        assert client.get("/health").status_code == 503
    finally:
        client.app.state.background_tasks = {}
        loop.close()
    assert "loop has stopped" not in client.get("/series").text
    client.app.state.worker_note = "Another Outriggarr instance holds this database"
    try:
        assert "Another Outriggarr instance holds this database" in client.get("/grab").text
    finally:
        del client.app.state.worker_note


def test_pins_only_subscription_preview_renders(client: TestClient) -> None:
    # no strategies at all: every unmatched row used to hit names[-1] on an empty list
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={
            "connection_id": 1,
            "series_id": 5,
            "source_url": "https://www.youtube.com/@hotones",
            "strategies": [],
        },
    ).json()["id"]
    r = client.get(f"/subscriptions/{sub_id}/preview")
    assert r.status_code == 200, r.text[:300]
    assert "Pins only, and none set yet" in r.text and "Pin a video" in r.text
    assert "<th>What each strategy saw</th>" not in r.text, (
        "a column of identical nothing is dropped"
    )


def test_stale_connection_form_says_so(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    r = client.post(
        "/settings/connections/999",
        data={"kind": "sonarr", "name": "x", "url": "http://h:1", "staging_path_remote": "/d"},
    )
    assert r.status_code == 404
    assert 'class="notice bad">Connection 999 no longer exists; nothing saved.' in r.text


def test_page_scripts_cover_network_errors_and_sticky_banners(client: TestClient) -> None:
    page = client.get("/activity").text
    assert "htmx:sendError" in page and "htmx:timeout" in page
    assert "htmx:afterRequest" in page and "bar.remove()" in page
    assert ":not([data-sticky])" in page


def test_removing_a_missing_pin_is_an_error_notice(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    r = client.post(f"/subscriptions/{sub_id}/overrides/nope/delete")
    assert r.status_code == 200
    assert 'class="notice bad" role="alert"' in r.text and "Pin not removed:" in r.text


def test_rejected_downloads_form_has_a_pointer_at_the_top(client: TestClient) -> None:
    r = client.post("/settings/downloads", data={"ytdlp_extra_opts": "{bad"})
    assert r.status_code == 400
    assert 'Downloads not saved: <a href="#downloads">see the form below</a>.' in r.text


def test_clearing_a_job_of_another_series_is_refused(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus, TargetKind

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                target_kind=TargetKind.episode,
                series_id=6,
                episode_ids=[77],
                target_key="episode:6:77",
                video_id="other",
                video_url="https://y/other",
                video_title="Other show",
                target_label="Hot Zone S01E01",
                status=JobStatus.done,
            )
        )
        s.commit()
        job_id = s.scalars(select(Job.id).where(Job.video_id == "other")).one()
    r = client.post(f"/subscriptions/{sub_id}/episodes/jobs/{job_id}/clear")
    assert r.status_code == 200 and "does not belong to this series" in r.text
    assert client.get(f"/api/jobs/{job_id}").status_code == 200, "untouched"


def test_subscribe_form_says_when_sonarr_did_not_answer(client: TestClient, monkeypatch) -> None:
    from outriggarr.arr.base import ArrError
    from outriggarr.web import pages

    _seed_series(client)

    async def dead(conn, factory):
        raise ArrError("Sonarr: connection refused")

    monkeypatch.setattr(pages, "_series_list", dead)
    page = client.get("/series/5/subscribe").text
    assert "did not answer, so the series title is missing: Sonarr: connection refused" in page
    assert 'name="sources"' in page, "the form still works"


def test_season_summary_says_what_a_zero_means(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from tests.fakes import FakeArrClient

    now = datetime.now(UTC)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", 2015, 1, True)],
        episodes_by_series={
            5: [
                EpisodeRef(1, 2, 1, "Off", False, False, now - timedelta(days=9)),  # unmonitored
                EpisodeRef(2, 2, 2, "Off too", False, False, now - timedelta(days=8)),
                EpisodeRef(3, 3, 1, "Have", True, True, now - timedelta(days=7)),
                EpisodeRef(4, 3, 2, "Soon", False, True, now + timedelta(days=7)),  # unaired
                EpisodeRef(5, 3, 3, "Skip", False, False, now - timedelta(days=1)),  # unmonitored
            ]
        },
    )
    client.app.state.source.recent = []
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@x"},
    ).json()["id"]
    html = client.get(f"/subscriptions/{sub_id}/episodes").text
    assert 'Season 3 <span class="muted">· 1/2 files · 1 unaired · 1 unmonitored</span>' in html
    assert 'Season 2 <span class="muted">· unmonitored · 2 episodes</span>' in html
    assert "0/2 files" not in html, "an unmonitored season is not a gap"
    # a season that is still airing opens by default; a settled one stays closed
    assert '<details class="plain season" open>\n  <summary>Season 3' in html.replace(
        "\n\n", "\n"
    ) or (html.index('season" open>') < html.index("<summary>Season 3"))
    assert html.count('season" open>') == 1, "only the airing season opened"


def test_subscription_header_says_what_the_last_scan_did(client: TestClient) -> None:
    from datetime import UTC, datetime

    from outriggarr.db.models import Subscription

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    for result, expect in (
        ({"matched": 3, "created": 2, "unmatched": 1, "error": None}, "3 matched, 2 queued"),
        ({"matched": 3, "created": 0, "unmatched": 1, "error": None}, "3 matched, 1 unmatched"),
        ({"matched": 0, "created": 0, "unmatched": 0, "error": None}, "nothing new"),
        (
            {"matched": 0, "created": 0, "unmatched": 0, "error": "boom"},
            '<span class="bad">failed</span>',
        ),
    ):
        with client.app.state.session_factory() as s:
            sub = s.get(Subscription, sub_id)
            sub.last_scan_at = datetime.now(UTC)
            sub.last_scan_result = result
            s.commit()
        page = client.get(f"/subscriptions/{sub_id}").text
        assert expect in page, expect


def test_preview_with_nothing_wanted_says_it_once(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from tests.fakes import FakeArrClient

    aired = datetime.now(UTC) - timedelta(days=1)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Done Show", 2015, 1, True)],
        episodes_by_series={5: [EpisodeRef(11, 1, 1, "One", True, True, aired)]},
    )
    client.app.state.source.recent = []
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@done"},
    ).json()["id"]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "Sonarr wants nothing from this series right now" in prev
    assert "0 matched" not in prev and "Nothing to queue" not in prev, "said once, not three times"


def test_a_spent_pin_says_so_and_a_failed_count_is_red(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus, TargetKind

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    client.post(
        f"/subscriptions/{sub_id}/overrides",
        data={"season": "1", "episode": "1", "video_id": "b"},  # picked from the listing
    )
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "not wanted now" in prev, "S01E01 is not a wanted episode"
    with client.app.state.session_factory() as s:
        s.add(
            Job(
                connection_id=1,
                target_kind=TargetKind.episode,
                series_id=5,
                episode_ids=[11],
                target_key="episode:5:11",
                video_id="f",
                video_url="https://y/f",
                status=JobStatus.failed,
                error="x",
            )
        )
        s.commit()
    page = client.get("/activity").text
    assert 'Failed<span class="count bad">1</span>' in page
    assert 'Active<span class="count">0</span>' in page


def test_matches_episode_cell_mutes_the_series_and_bolds_the_code(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus, TargetKind

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
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
                video_id="v",
                video_url="https://y/v",
                video_title="Six Spicy Wings | Hot Ones",
                target_label="Hot Ones S30E06 - Six Spicy Wings",
                matched_by="exact",
                status=JobStatus.done,
            )
        )
        s.commit()
    page = client.get("/matches?view=all").text
    assert '<span class="muted">Hot Ones</span> <strong>S30E06</strong> Six Spicy Wings' in page
    assert 'href="/activity#job-' in page, "the job ref lands on its Activity row"


def test_the_preview_is_cached_so_a_page_open_costs_no_listing(client: TestClient) -> None:
    _seed_series(client)
    source = client.app.state.source
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    source.listed.clear()
    first = client.get(f"/subscriptions/{sub_id}/preview").text  # nothing cached yet: one listing
    assert len(source.listed) == 1 and "Listed just now" in first
    for _ in range(3):
        again = client.get(f"/subscriptions/{sub_id}/preview").text
    assert len(source.listed) == 1, "three more page opens, still one listing"
    assert "Six Spicy Wings" in again and "Listed just now" in again
    client.post(f"/subscriptions/{sub_id}/scan")  # Refresh preview lists again
    assert len(source.listed) == 2


def test_a_failed_scan_leaves_the_last_good_preview_alone(client: TestClient) -> None:
    from outriggarr.source import SourceError

    _seed_series(client)
    source = client.app.state.source
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    assert "Six Spicy Wings" in client.get(f"/subscriptions/{sub_id}/preview").text
    source.recent_error = SourceError("ERROR: [youtube:tab] @hotones: This channel does not exist")
    failed = client.post(f"/subscriptions/{sub_id}/scan").text
    assert "This channel does not exist" in failed, "the failure is said"
    source.recent_error = None
    source.listed.clear()
    again = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "Six Spicy Wings" in again and "does not exist" not in again, "the good look survived"
    assert source.listed == [], "and it came from the cache"


def test_changing_what_a_scan_matches_drops_the_cached_preview(client: TestClient) -> None:
    _seed_series(client)
    source = client.app.state.source
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    client.get(f"/subscriptions/{sub_id}/preview")
    source.listed.clear()
    client.put(  # a name is cosmetic: the cache stands
        f"/api/subscriptions/{sub_id}",
        json={
            "connection_id": 1,
            "series_id": 5,
            "sources": ["https://www.youtube.com/@hotones"],
            "strategies": ["title"],
            "audio_language": "eng",
        },
    )
    client.get(f"/subscriptions/{sub_id}/preview")
    assert source.listed == [], "an unrelated edit keeps the cache"
    client.put(  # the strategies decide what matches: the cached look is void
        f"/api/subscriptions/{sub_id}",
        json={
            "connection_id": 1,
            "series_id": 5,
            "sources": ["https://www.youtube.com/@hotones"],
            "strategies": ["title", "date"],
        },
    )
    client.get(f"/subscriptions/{sub_id}/preview")
    assert len(source.listed) == 1, "and the next page open looks again"


def test_the_activity_poll_sends_nothing_while_nothing_moves(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.web.pages import JOBS_VERSION_BUCKET_SECONDS, jobs_version

    client.post("/api/connections", json=SONARR)
    page = client.get("/activity").text
    assert 'hx-vals=\'js:{v: (document.getElementById("jobs-version") || {}).value || ""}\'' in page
    version = page.split('id="jobs-version" value="')[1].split('"')[0]
    assert version

    # the page already shows this table: nothing to send, and htmx swaps nothing
    unchanged = client.get(f"/activity/rows?view=all&v={version}")
    assert unchanged.status_code == 204 and not unchanged.content

    # no stamp (a first load) or an old one: the rows come
    assert client.get("/activity/rows?view=all").status_code == 200
    assert client.get("/activity/rows?view=all&v=stale").status_code == 200

    job_id = _job(client, 1)
    moved = client.get(f"/activity/rows?view=all&v={version}")
    assert moved.status_code == 200 and f"#{job_id}" in moved.text, "a new job is a change"
    version = moved.text.split('id="jobs-version" value="')[1].split('"')[0]
    assert client.get(f"/activity/rows?view=all&v={version}").status_code == 204

    client.post(f"/api/jobs/{job_id}/cancel")
    assert client.get(f"/activity/rows?view=all&v={version}").status_code == 200, "a status change"

    # the relative times ("22 hr ago") age with no row changing: the stamp turns over anyway
    with client.app.state.session_factory() as s:
        now = datetime.now(UTC)
        assert jobs_version(s, now) == jobs_version(s, now + timedelta(seconds=1))
        later = now + timedelta(seconds=JOBS_VERSION_BUCKET_SECONDS + 1)
        assert jobs_version(s, now) != jobs_version(s, later)


def test_the_poll_notices_progress_and_retries(client: TestClient) -> None:
    from outriggarr.db.models import Job, JobStatus

    client.post("/api/connections", json=SONARR)
    job_id = _job(client, 1)
    version = (
        client.get("/activity/rows?view=all")
        .text.split('id="jobs-version" value="')[1]
        .split('"')[0]
    )
    with client.app.state.session_factory() as s:
        job = s.get(Job, job_id)
        job.status, job.progress_pct = JobStatus.downloading, 40
        s.commit()
    moved = client.get(f"/activity/rows?view=all&v={version}")
    assert moved.status_code == 200
    version = moved.text.split('id="jobs-version" value="')[1].split('"')[0]
    with client.app.state.session_factory() as s:  # the same status, further along
        s.get(Job, job_id).progress_pct = 70
        s.commit()
    assert client.get(f"/activity/rows?view=all&v={version}").status_code == 200
    version = (
        client.get("/activity/rows?view=all")
        .text.split('id="jobs-version" value="')[1]
        .split('"')[0]
    )
    with client.app.state.session_factory() as s:  # a retry: another attempt on the same row
        s.get(Job, job_id).attempts = 2
        s.commit()
    assert client.get(f"/activity/rows?view=all&v={version}").status_code == 200


def test_a_closed_season_sends_its_rows_only_when_it_opens(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from outriggarr.arr.base import EpisodeRef, SeriesRef
    from tests.fakes import FakeArrClient

    now = datetime.now(UTC)
    client.app.state.arr_factory.by_url["http://sonarr-host:1234"] = FakeArrClient(
        series_list=[SeriesRef(5, "Show", 2015, 1, True)],
        episodes_by_series={
            5: [
                EpisodeRef(1, 3, 1, "Newest missing", False, True, now - timedelta(days=1)),
                EpisodeRef(2, 2, 1, "Older settled", True, True, now - timedelta(days=40)),
                EpisodeRef(3, 1, 1, "Oldest settled", True, True, now - timedelta(days=80)),
            ]
        },
    )
    client.app.state.source.recent = []
    client.post("/api/connections", json=SONARR)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@x"},
    ).json()["id"]
    card = client.get(f"/subscriptions/{sub_id}/episodes").text
    assert "Newest missing" in card, "the open season's rows come with the card"
    assert "Older settled" not in card and "Oldest settled" not in card
    assert card.count('hx-trigger="intersect once"') == 2, "the closed ones fetch on open"
    assert f'hx-get="/subscriptions/{sub_id}/episodes/2"' in card
    assert "Season 2" in card and "Season 1" in card, "every season still has its summary"

    opened = client.get(f"/subscriptions/{sub_id}/episodes/2")
    assert opened.status_code == 200
    assert "Older settled" in opened.text and "✓ file" in opened.text
    assert "Oldest settled" not in opened.text, "one season, not the rest"
    assert "<summary>" not in opened.text, "the rows swap into the season that asked"

    assert client.get(f"/subscriptions/{sub_id}/episodes/99").status_code == 404
    assert client.get("/subscriptions/999/episodes/1").status_code == 404


def test_the_lazy_season_route_reports_a_sonarr_failure(client: TestClient) -> None:
    from outriggarr.arr.base import ArrError

    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]

    async def boom(series_id):
        raise ArrError("GET /api/v3/episode -> HTTP 500: x")

    client.app.state.arr_factory.by_url["http://sonarr-host:1234"].episodes = boom
    r = client.get(f"/subscriptions/{sub_id}/episodes/30")
    assert r.status_code == 502 and "HTTP 500" in r.json()["detail"]


def test_the_why_panel_explains_a_pair(client: TestClient) -> None:
    _seed_series(client)
    sub_id = client.post(
        "/api/subscriptions",
        json={"connection_id": 1, "series_id": 5, "source_url": "https://www.youtube.com/@hotones"},
    ).json()["id"]
    prev = client.get(f"/subscriptions/{sub_id}/preview").text
    assert "Why didn&#39;t a video match?" in prev or "Why didn't a video match?" in prev
    assert f'hx-get="/subscriptions/{sub_id}/explain"' in prev
    assert 'list="explain-videos"' in prev and "listed-videos" in prev
    assert '<datalist id="explain-videos">' not in prev, "the picker's options load on open"

    options = client.get(f"/subscriptions/{sub_id}/listed-videos").text
    assert '<datalist id="explain-videos">' in options and 'value="https://y/a"' in options

    # S30E07 is unmatched; the Bonus video is why not
    answer = client.get(
        f"/subscriptions/{sub_id}/explain", params={"episode_id": 12, "video_url": "https://y/b"}
    ).text
    assert "No strategy pairs these two" in answer
    assert "does not appear inside" in answer and "seven spicy wings" in answer

    paired = client.get(
        f"/subscriptions/{sub_id}/explain", params={"episode_id": 11, "video_url": "https://y/a"}
    ).text
    assert "These two pair by <strong>title</strong>" in paired
    assert "appears inside" in paired, "and says why"

    assert "Pick one of the episodes" in client.get(f"/subscriptions/{sub_id}/explain").text
    assert (
        "Pick one of the listed videos"
        in client.get(
            f"/subscriptions/{sub_id}/explain",
            params={"episode_id": 11, "video_url": "https://y/nope"},
        ).text
    )
    assert client.get("/subscriptions/999/explain").status_code == 404
    assert client.get("/subscriptions/999/listed-videos").status_code == 404


def test_pages_travel_compressed(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    r = client.get("/activity", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
    assert "Activity" in r.text, "the client still reads it as HTML"
    css = client.get("/static/app.css", headers={"Accept-Encoding": "gzip"})
    assert css.headers.get("content-encoding") == "gzip", "the static bundle too"


def test_head_declares_the_dark_chrome_and_settings_sections_are_h2(client: TestClient) -> None:
    page = client.get("/activity").text
    assert '<meta name="theme-color" content="#13171f">' in page
    settings = client.get("/settings").text
    assert '<h2 class="section">Connections</h2>' in settings and "<h3" not in settings, (
        "a section heading is the level under the page's h1, not a skipped one"
    )
