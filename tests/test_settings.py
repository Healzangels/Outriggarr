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
        ("cookies_path", "", ""),
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


def test_set_setting_validates_and_ytdlp_options(session_factory, tmp_path) -> None:
    with session_factory() as s:
        assert ytdlp_options(s) == {}
        set_setting(s, "ytdlp_extra_opts", '{"sponsorblock_remove": ["sponsor"]}')
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File")
        set_setting(s, "cookies_path", str(cookies))
        s.commit()
        assert ytdlp_options(s) == {"sponsorblock_remove": ["sponsor"], "cookiefile": str(cookies)}
        with pytest.raises(ValueError, match="not a file"):
            set_setting(s, "cookies_path", str(tmp_path / "missing.txt"))
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


def test_health_reports_tooling(client: TestClient, monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["yt_dlp"], str) and body["yt_dlp"]
    assert "js_runtime" in body and "ffmpeg" in body
    assert json.dumps(body)


def test_health_reports_staging_writable(client: TestClient, settings, monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    assert client.get("/health").json()["staging_writable"] is True
    import shutil as _sh

    _sh.rmtree(settings.staging_dir)
    assert client.get("/health").json()["staging_writable"] is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("en", "en"), (" en, es ,en", "en,es"), ("", ""), ("en-US,pt-BR", "en-US,pt-BR")],
)
def test_subtitles_langs_valid(value: str, expected: str) -> None:
    assert validate_setting("subtitles_langs", value) == expected


@pytest.mark.parametrize("value", ["english", "e", "en;es", "en_US"])
def test_subtitles_langs_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        validate_setting("subtitles_langs", value)
    with pytest.raises(ValueError):
        validate_setting("subtitles_auto", "yes")
    assert validate_setting("subtitles_auto", "1") == "1"


def test_apprise_url_validation() -> None:
    assert validate_setting("apprise_urls", "") == ""
    ok = validate_setting(
        "apprise_urls", "json://localhost:1/hook\n , mailto://user:pass@gmail.com"
    )
    assert ok.split("\n") == ["json://localhost:1/hook", "mailto://user:pass@gmail.com"]
    with pytest.raises(ValueError, match="did not accept"):
        validate_setting("apprise_urls", "nope://what")
    with pytest.raises(ValueError):
        validate_setting("notify_on_done", "maybe")


def test_notify_test_endpoint(client: TestClient, notifier) -> None:
    r = client.post("/api/settings/notify/test")
    assert r.status_code == 422 and "no Apprise URLs" in r.json()["detail"]
    client.put("/api/settings", json={"apprise_urls": "json://localhost:1/hook"})
    r = client.post("/api/settings/notify/test")
    assert r.status_code == 200 and r.json() == {"sent": True, "targets": 1}
    assert notifier.sent == [("Outriggarr: test", "Notifications work.")]
    notifier.result = False
    assert client.post("/api/settings/notify/test").json()["sent"] is False


def test_apprise_notifier_reads_urls_per_send(monkeypatch) -> None:
    import apprise

    from outriggarr.notify import AppriseNotifier, NullNotifier

    calls: list[tuple[list[str], str]] = []

    class StubApprise:
        def __init__(self):
            self.urls = []

        def add(self, u):
            self.urls.append(u)
            return True

        def notify(self, title, body):
            calls.append((list(self.urls), title))
            return True

    monkeypatch.setattr(apprise, "Apprise", StubApprise)
    urls = ["json://a/1"]
    n = AppriseNotifier(lambda: list(urls))
    assert n.send("t", "b") is True
    urls.append("json://b/2")
    assert n.send("t2", "b") is True
    # one Apprise per target: Apprise's own notify() is all-or-nothing across targets
    assert calls == [(["json://a/1"], "t"), (["json://a/1"], "t2"), (["json://b/2"], "t2")]
    urls.clear()
    assert n.send("t3", "b") is False, "no URLs → nothing sent"
    assert NullNotifier().send("x", "y") is False


def test_notifier_reports_delivered_when_one_of_two_targets_accepts(monkeypatch) -> None:
    import apprise

    from outriggarr.notify import AppriseNotifier

    class FlakyApprise:
        def __init__(self):
            self.urls = []

        def add(self, u):
            self.urls.append(u)
            return True

        def notify(self, title, body):
            return "good" in self.urls[0]

    monkeypatch.setattr(apprise, "Apprise", FlakyApprise)
    assert AppriseNotifier(lambda: ["json://dead/1", "json://good/2"]).send("t", "b") is True
    assert AppriseNotifier(lambda: ["json://dead/1"]).send("t", "b") is False


def test_extra_opts_cannot_override_runner_keys() -> None:
    from outriggarr.settings import RESERVED_YTDLP_KEYS

    for key in ("outtmpl", "postprocessors", "paths", "logger", "format", "progress_hooks"):
        assert key in RESERVED_YTDLP_KEYS
        with pytest.raises(ValueError, match="owns those options"):
            validate_setting("ytdlp_extra_opts", json.dumps({key: "x"}))
    assert validate_setting(
        "ytdlp_extra_opts", '{"ratelimit": 1, "sponsorblock_remove": ["sponsor"]}'
    )


def test_source_drops_reserved_extra_keys_even_if_stored(monkeypatch) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    seen: list[dict] = []

    class StubYDL:
        def __init__(self, opts):
            seen.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "x", "title": "t", "webpage_url": url}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", StubYDL)
    src = YtDlpSource(
        extra_opts=lambda: {
            "outtmpl": "/etc/passwd",
            "postprocessors": [{"key": "Exec"}],
            "ratelimit": 5,
        }
    )
    src.resolve("https://youtu.be/x")
    assert seen[0].get("outtmpl") != "/etc/passwd" and "postprocessors" not in seen[0]
    assert seen[0]["ratelimit"] == 5 and seen[0]["extract_flat"] == "in_playlist"


def test_default_format_and_extra_opts_are_probed_by_ytdlp() -> None:
    with pytest.raises(ValueError, match="yt-dlp rejected"):
        validate_setting("default_format", "bestvideo[height<=1080")
    assert validate_setting("default_format", "bestvideo[height<=1080]+bestaudio/best")
    with pytest.raises(ValueError):
        validate_setting("merge_container", "webm")


def test_log_level_env_is_normalised_and_validated() -> None:
    from outriggarr.settings import Settings

    assert Settings.from_env({"OUTRIGGARR_LOG_LEVEL": "debug"}).log_level == "DEBUG"
    assert Settings.from_env({}).log_level == "INFO"
    with pytest.raises(ValueError, match="not a logging level"):
        Settings.from_env({"OUTRIGGARR_LOG_LEVEL": "loud"})


def test_ytdlp_stop_condition_keys_are_reserved() -> None:
    for key in ("download_archive", "break_on_existing", "max_downloads"):
        with pytest.raises(ValueError, match="owns those options"):
            validate_setting("ytdlp_extra_opts", json.dumps({key: 1}))


def test_every_format_preset_is_a_valid_selector_and_the_default_is_one() -> None:
    from outriggarr.settings import DEFAULTS, FORMAT_PRESETS, preset_for, validate_setting

    keys = [p.key for p in FORMAT_PRESETS]
    assert len(keys) == len(set(keys)) and len(keys) >= 5
    for p in FORMAT_PRESETS:
        assert validate_setting("default_format", p.format) == p.format, p.key  # yt-dlp parses it
        assert p.label and p.note
    assert preset_for(DEFAULTS["default_format"]).key == "1080p-h264", (
        "the picker shows the default"
    )
    assert preset_for("  bestvideo*+bestaudio/best\n").key == "best", (
        "whitespace is not a difference"
    )
    assert preset_for(None) is None and preset_for("") is None
    assert preset_for("bestvideo[height<=600]+bestaudio") is None, "hand-written is custom"
    by_height = {p.key: p.format for p in FORMAT_PRESETS}
    assert "height<=2160" in by_height["2160p-any"] and "avc1" not in by_height["2160p-any"], (
        "YouTube has no H.264 above 1080p: the 4K preset must not ask for it"
    )
    assert "avc1" in by_height["720p-h264"] and "mp4a" in by_height["720p-h264"]


def test_noisy_ytdlp_keys_are_reserved() -> None:
    from outriggarr.settings import validate_setting

    with pytest.raises(ValueError, match="Outriggarr owns those options"):
        validate_setting("ytdlp_extra_opts", '{"verbose": true}')
    assert validate_setting("ytdlp_extra_opts", '{"quiet": false}'), "quiet stays the operator's"


@pytest.mark.parametrize(
    "key",
    ["ignoreerrors", "simulate", "cookiesfrombrowser", "playlist_items", "playlistreverse"],
)
def test_options_that_change_what_the_runner_sees_are_reserved(key: str) -> None:
    from outriggarr.settings import RESERVED_YTDLP_KEYS

    assert key in RESERVED_YTDLP_KEYS
    assert "cookiefile" not in RESERVED_YTDLP_KEYS, "the app passes its own cookies path this way"
