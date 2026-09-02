from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from outriggarr.settings import DEFAULTS, get_setting, set_setting, validate_setting, ytdlp_options


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("scan_interval_minutes", " 15 ", "15"),
        ("concurrency", "3", "3"),
        ("scan_video_limit", "500", "500"),
        ("default_format", "best", "best"),
        ("merge_container", "mp4", "mp4"),
        ("ytdlp_extra_opts", '{"ratelimit": 1}', '{"ratelimit": 1}'),
        ("ytdlp_extra_opts", "", "{}"),
        ("cookies_path", "/config/cookies.txt", "/config/cookies.txt"),
        ("sonarr_tag", "outriggarr", "outriggarr"),
        ("sonarr_tag", "", ""),
        ("audio_language", "eng", "eng"),
        ("audio_language", "", ""),
    ],
)
def test_validate_setting_ok(key: str, value: str, expected: str) -> None:
    assert validate_setting(key, value) == expected


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("scan_interval_minutes", "0"),
        ("scan_interval_minutes", "x"),
        ("concurrency", "9"),
        ("scan_video_limit", "0"),
        ("default_format", "  "),
        ("merge_container", "avi"),
        ("ytdlp_extra_opts", "{not json"),
        ("ytdlp_extra_opts", "[1, 2]"),
        ("sonarr_tag", "Has Space"),
        ("sonarr_tag", "Upper"),
        ("audio_language", "english"),
        ("audio_language", "EN"),
    ],
)
def test_validate_setting_rejects(key: str, value: str) -> None:
    with pytest.raises(ValueError):
        validate_setting(key, value)
    with pytest.raises(KeyError):
        validate_setting("nope", "x")


def test_set_setting_validates_and_ytdlp_options(session_factory) -> None:
    with session_factory() as s:
        assert ytdlp_options(s) == {}
        set_setting(s, "ytdlp_extra_opts", '{"sponsorblock_remove": ["sponsor"]}')
        set_setting(s, "cookies_path", "/config/cookies.txt")
        s.commit()
        assert ytdlp_options(s) == {
            "sponsorblock_remove": ["sponsor"],
            "cookiefile": "/config/cookies.txt",
        }
        with pytest.raises(ValueError):
            set_setting(s, "concurrency", "0")
        assert get_setting(s, "concurrency") == DEFAULTS["concurrency"]


def test_settings_api(client: TestClient) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 200 and r.json() == DEFAULTS
    r = client.put("/api/settings", json={"concurrency": "2", "audio_language": ""})
    assert r.status_code == 200
    assert r.json()["concurrency"] == "2" and r.json()["audio_language"] == ""
    assert client.get("/api/settings").json()["concurrency"] == "2"
    r = client.put("/api/settings", json={"concurrency": "4", "merge_container": "avi"})
    assert r.status_code == 422 and "merge_container" in r.json()["detail"]
    assert client.get("/api/settings").json()["concurrency"] == "2", "all-or-nothing"
    r = client.put("/api/settings", json={"bogus": "1"})
    assert r.status_code == 422 and "unknown setting" in r.json()["detail"]


def test_health_reports_tooling(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["yt_dlp"], str) and body["yt_dlp"]
    assert "js_runtime" in body and "ffmpeg" in body
    assert json.dumps(body)
