from pathlib import Path

import pytest

from outriggarr.source import (
    CoolOff,
    SourceError,
    VideoRef,
    is_permanent_failure,
    is_rate_limited,
    relative_age,
    skip_reason,
    videos_from_info,
)


def test_playlist_flat_entries() -> None:
    info = {
        "_type": "playlist",
        "id": "PL1",
        "entries": [
            {
                "id": "a1",
                "title": "First",
                "url": "https://www.youtube.com/watch?v=a1",
                "duration": 61.0,
                "playlist_index": 1,
            },
            None,
            {"_type": "playlist", "id": "nested", "title": "a channel tab"},
            {"id": "b2", "title": "Second", "duration": None, "upload_date": "20240102"},
            {"title": "no id"},
        ],
    }
    assert videos_from_info(info) == [
        VideoRef("a1", "First", "https://www.youtube.com/watch?v=a1", 61, 1, None),
        VideoRef("b2", "Second", "https://www.youtube.com/watch?v=b2", None, 4, "20240102"),
    ]


def test_single_video() -> None:
    info = {"id": "x9", "title": "Solo", "webpage_url": "https://youtu.be/x9", "duration": 12}
    assert videos_from_info(info) == [VideoRef("x9", "Solo", "https://youtu.be/x9", 12, None, None)]


def test_single_without_id_is_error() -> None:
    with pytest.raises(SourceError):
        videos_from_info({"title": "?"})


def test_title_falls_back_to_id() -> None:
    (v,) = videos_from_info({"id": "q", "url": "u"})
    assert v.title == "q" and v.url == "u"


def test_channel_videos_url() -> None:
    from outriggarr.source import channel_videos_url

    assert (
        channel_videos_url("https://www.youtube.com/@FirstWeFeast")
        == "https://www.youtube.com/@FirstWeFeast/videos"
    )
    assert (
        channel_videos_url("https://youtube.com/channel/UCabc/")
        == "https://youtube.com/channel/UCabc/videos"
    )
    assert (
        channel_videos_url("https://www.youtube.com/@x/videos")
        == "https://www.youtube.com/@x/videos"
    )
    assert (
        channel_videos_url("https://www.youtube.com/@x/playlists")
        == "https://www.youtube.com/@x/playlists"
    )
    assert (
        channel_videos_url("https://www.youtube.com/playlist?list=PL1")
        == "https://www.youtube.com/playlist?list=PL1"
    )
    assert (
        channel_videos_url(" https://www.youtube.com/@x?si=abc ")
        == "https://www.youtube.com/@x/videos"
    )


def test_ffmpeg_language_command_copies_streams_and_tags_audio(tmp_path) -> None:
    from pathlib import Path

    from outriggarr.source import ffmpeg_language_command

    cmd = ffmpeg_language_command(Path("/s/in.mkv"), Path("/s/in.lang.mkv"), "eng")
    assert cmd[0] == "ffmpeg" and cmd[-1] == "/s/in.lang.mkv"
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    # the first video, every audio stream, subtitles if any; never "0": archive.org's
    # 2008-era mp4s carry RTP hint tracks and an mjpeg cover that mp4 output refuses
    assert maps == ["0:v:0", "0:a", "0:s?"] and "-dn" in cmd
    assert cmd[cmd.index("-metadata:s:a") + 1] == "language=eng"
    assert "-y" in cmd and "-nostdin" in cmd


def test_tag_audio_language_replaces_file_in_place(tmp_path, monkeypatch) -> None:
    import subprocess

    from outriggarr.source import REMUX_TIMEOUT_SECONDS, SourceError, YtDlpSource

    src = tmp_path / "a.mkv"
    src.write_bytes(b"orig")
    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        assert timeout == REMUX_TIMEOUT_SECONDS, "a hung ffmpeg must not hold the worker"
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"tagged")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    from pathlib import Path

    monkeypatch.setattr(subprocess, "run", fake_run)
    YtDlpSource().tag_audio_language(src, "eng")
    assert src.read_bytes() == b"tagged"
    assert not (tmp_path / "a.lang.mkv").exists()
    assert calls[0][cmd_idx := calls[0].index("-metadata:s:a") + 1] == "language=eng" and cmd_idx

    def failing_run(cmd, capture_output, text, timeout):
        Path(cmd[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(cmd, 1, "", "Invalid data found when processing input")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(SourceError, match="Invalid data found"):
        YtDlpSource().tag_audio_language(src, "eng")
    assert src.read_bytes() == b"tagged", "original untouched on failure"
    assert not (tmp_path / "a.lang.mkv").exists(), "temp output removed on failure"

    def hanging_run(cmd, capture_output, text, timeout):
        Path(cmd[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", hanging_run)
    with pytest.raises(SourceError, match="ffmpeg gave up after 60 min"):
        YtDlpSource().tag_audio_language(src, "eng")
    assert src.read_bytes() == b"tagged", "original untouched when ffmpeg hangs"
    assert not (tmp_path / "a.lang.mkv").exists(), "temp output removed when ffmpeg hangs"


def test_ytdlp_source_merges_extra_opts_last(monkeypatch, tmp_path) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    seen: list[dict] = []

    from yt_dlp.utils import DownloadError

    class StubYDL:
        def __init__(self, opts):
            # yt-dlp gets a private copy of the operator's jar; read it while it exists
            jar = Path(opts["cookiefile"]).read_text() if "cookiefile" in opts else None
            seen.append({**opts, "_jar": jar})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if seen[-1]["_jar"] is None:  # cookies come only when YouTube asks
                raise DownloadError("ERROR: [youtube] x: Sign in to confirm your age")
            return {"id": "x", "title": "t", "webpage_url": url}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", StubYDL)
    cookies = tmp_path / "c.txt"
    cookies.write_text("# cookies")
    src = YtDlpSource(extra_opts=lambda: {"cookiefile": str(cookies), "quiet": False})
    (v,) = src.resolve("https://youtu.be/x")
    assert v.id == "x"
    with_cookies = [c for c in seen if "cookiefile" in c]
    assert with_cookies[0]["cookiefile"] != str(cookies) and with_cookies[0]["_jar"] == "# cookies"
    assert with_cookies[0]["quiet"] is False, "operator options win over ours"
    assert with_cookies[0]["extract_flat"] == "in_playlist" and "logger" in with_cookies[0]
    src.list_recent("https://www.youtube.com/@c", 7)
    with_cookies = [c for c in seen if "cookiefile" in c]
    assert with_cookies[1]["playlistend"] == 7 + 5 and with_cookies[1]["_jar"] == "# cookies"


def test_subtitle_opts_and_sidecars(tmp_path) -> None:
    from outriggarr.source import subtitle_opts, subtitle_sidecars

    o = subtitle_opts(("en", "es"), False)
    assert o["writesubtitles"] is True and o["writeautomaticsub"] is False
    assert o["subtitleslangs"] == ["en", "es"] and o["subtitlesformat"] == "srt/best"
    assert o["postprocessors"] == [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}]
    assert subtitle_opts(("en",), True)["writeautomaticsub"] is True

    (tmp_path / "abc.en.srt").write_text("x")
    (tmp_path / "abc.es.srt").write_text("x")
    (tmp_path / "abc.mkv").write_text("x")
    (tmp_path / "other.en.srt").write_text("x")
    (tmp_path / "abc.en.vtt").write_text("x")
    assert [p.name for p in subtitle_sidecars(tmp_path, "abc")] == ["abc.en.srt", "abc.es.srt"]
    assert subtitle_sidecars(tmp_path, "zzz") == ()


def test_download_result_collects_sidecars(tmp_path) -> None:
    from outriggarr.source import _result_from_info

    (tmp_path / "v1.mkv").write_text("x")
    (tmp_path / "v1.en.srt").write_text("x")
    info = {
        "id": "v1",
        "title": "T",
        "height": 720,
        "requested_downloads": [{"filepath": str(tmp_path / "v1.mkv")}],
    }
    r = _result_from_info(info, tmp_path)
    assert r.subtitles == (tmp_path / "v1.en.srt",)
    assert _result_from_info(info).subtitles == ()


def test_list_recent_caps_channels_but_lists_playlists_whole(monkeypatch) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    seen: list[tuple[str, dict]] = []

    class StubYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            seen.append((url, self.opts))
            return {
                "_type": "playlist",
                "id": "x",
                "entries": [{"id": f"v{i}", "title": str(i)} for i in range(5)],
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", StubYDL)
    src = YtDlpSource()
    assert len(src.list_recent("https://www.youtube.com/@c", 3)) == 3
    assert seen[-1][0].endswith("/videos") and seen[-1][1]["playlistend"] == 3 + 5
    assert len(src.list_recent("https://www.youtube.com/playlist?list=PL1", 3)) == 5, (
        "playlists are not truncated"
    )
    assert "playlistend" not in seen[-1][1]
    src.resolve("https://www.youtube.com/watch?v=abc&list=PL1")
    assert seen[-1][1]["noplaylist"] is True
    src.resolve("https://www.youtube.com/@c")
    assert seen[-1][0].endswith("/videos")


def test_bad_option_values_become_source_errors(monkeypatch) -> None:
    from pathlib import Path

    import yt_dlp

    from outriggarr.source import SourceError, YtDlpSource

    class Boom:
        def __init__(self, opts):
            raise SyntaxError("Invalid format specification")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Boom)
    with pytest.raises(SourceError, match="could not run"):
        YtDlpSource().resolve("https://youtu.be/x")
    with pytest.raises(SourceError, match="could not run"):
        YtDlpSource().download(
            "https://youtu.be/x",
            Path("/tmp/x"),
            fmt="bad[",
            merge_container="mkv",
            progress=lambda p: None,
            should_abort=lambda: False,
        )


def test_cookie_save_failure_after_download_keeps_the_result(monkeypatch, tmp_path) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    class SavesCookiesBadly:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            raise PermissionError(13, "Permission denied", "/config/cookies.txt")

        def extract_info(self, url, download=True):
            (tmp_path / "x.mkv").write_bytes(b"v")
            return {
                "id": "x",
                "title": "T",
                "height": 720,
                "requested_downloads": [{"filepath": str(tmp_path / "x.mkv")}],
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", SavesCookiesBadly)
    r = YtDlpSource().download(
        "https://youtu.be/x",
        tmp_path,
        fmt="best",
        merge_container="mkv",
        progress=lambda p: None,
        should_abort=lambda: False,
    )
    assert r.path.name == "x.mkv"


def test_ytdlp_stop_conditions_are_errors_not_our_abort(monkeypatch, tmp_path) -> None:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled

    from outriggarr.source import DownloadAborted, SourceError, YtDlpSource

    class Stops:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            raise DownloadCancelled(self.opts["_msg"])

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Stops)
    src = YtDlpSource(extra_opts=lambda: {"_msg": "Already downloaded (in the archive)"})
    with pytest.raises(SourceError, match="yt-dlp stopped"):
        src.download(
            "https://youtu.be/x",
            tmp_path,
            fmt="best",
            merge_container="mkv",
            progress=lambda p: None,
            should_abort=lambda: False,
        )
    src = YtDlpSource(extra_opts=lambda: {"_msg": "aborted by outriggarr"})
    with pytest.raises(DownloadAborted):
        src.download(
            "https://youtu.be/x",
            tmp_path,
            fmt="best",
            merge_container="mkv",
            progress=lambda p: None,
            should_abort=lambda: False,
        )


def test_private_and_deleted_entries_are_unavailable() -> None:
    info = {
        "_type": "playlist",
        "id": "p",
        "entries": [
            {"id": "a1", "title": "[Private video]"},
            {"id": "b2", "title": "[Deleted video]"},
            {"id": "c3", "title": "Real"},
        ],
    }
    assert [v.title for v in videos_from_info(info)] == ["a1", "b2", "Real"]


def test_unreadable_cookies_file_is_a_clear_error(monkeypatch, tmp_path) -> None:
    from outriggarr.source import SourceError, YtDlpSource

    src = YtDlpSource(extra_opts=lambda: {"cookiefile": str(tmp_path / "missing.txt")})
    with pytest.raises(SourceError, match="cookies file") as info:
        src.resolve("https://youtu.be/x")
    assert str(info.value).startswith("cookies file"), "ours, not wrapped in 'could not run'"


def test_ytdlp_gets_a_private_cookie_jar_and_never_clobbers_a_replaced_file(
    monkeypatch, tmp_path
) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\nold session\n")
    seen: dict = {}

    from yt_dlp.utils import DownloadError

    class StubYDL:
        def __init__(self, opts):
            self.opts = opts
            if "cookiefile" in opts:
                seen["cookiefile"] = opts["cookiefile"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            if "cookiefile" not in self.opts:
                return False
            # yt-dlp saves its jar on close: the session it loaded, plus rotations
            path = Path(self.opts["cookiefile"])
            path.write_text(path.read_text() + "rotated\n")
            return False

        def extract_info(self, url, download=False):
            if "cookiefile" not in self.opts:  # an age gate: cookies come on the retry
                raise DownloadError("ERROR: [youtube] x: Sign in to confirm your age")
            if seen.get("replace_during_run"):
                jar.write_text("# Netscape HTTP Cookie File\nNEW SESSION\n")
            return {"id": "x", "title": "T", "webpage_url": url, "duration": 1}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", StubYDL)
    src = YtDlpSource(extra_opts=lambda: {"cookiefile": str(jar)})
    src.fetch_info("https://youtu.be/x")
    assert seen["cookiefile"] != str(jar), "yt-dlp writes to a private copy"
    assert not Path(seen["cookiefile"]).exists(), "the private copy is removed afterwards"
    assert jar.read_text().endswith("old session\nrotated\n"), "rotations kept when untouched"

    seen["replace_during_run"] = True
    src.fetch_info("https://youtu.be/x")
    assert jar.read_text() == "# Netscape HTTP Cookie File\nNEW SESSION\n", (
        "a file replaced while yt-dlp ran wins over the old session yt-dlp would save"
    )
    assert not Path(seen["cookiefile"]).exists()


def test_po_token_provider_is_wired_when_present_and_operator_args_merge(
    monkeypatch, tmp_path
) -> None:
    import shutil

    import yt_dlp

    from outriggarr.source import YtDlpSource, pot_provider_ready

    home = tmp_path / "server"
    assert pot_provider_ready(None) is False and pot_provider_ready(home) is False
    (home / "build").mkdir(parents=True)
    (home / "build" / "generate_once.js").write_text("// stub")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/local/bin/node" if name == "node" else None
    )
    assert pot_provider_ready(home) is True

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
    operator = {
        "extractor_args": {
            "youtube": {"player_client": ["tv"]},
            "youtubepot-bgutilscript": {"disable_innertube": ["1"]},
        }
    }
    src = YtDlpSource(extra_opts=lambda: operator, pot_server_home=home)
    src.resolve("https://youtu.be/x")
    assert seen[0]["extractor_args"] == {
        "youtubepot-bgutilscript": {"server_home": [str(home)], "disable_innertube": ["1"]},
        "youtube": {"player_client": ["tv"]},
        "youtubetab": {"approximate_date": ["1"]},
    }, "ours plus the operator's, merged per extractor"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    YtDlpSource(extra_opts=lambda: {}, pot_server_home=home).resolve("https://youtu.be/x")
    assert "youtubepot-bgutilscript" not in seen[1]["extractor_args"], (
        "no node: no PO-token provider is promised to yt-dlp"
    )
    assert seen[1]["extractor_args"]["youtubetab"] == {"approximate_date": ["1"]}


JAR_SIGNED_IN = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1790000000\tPREF\tf6=400\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1790000000\tLOGIN_INFO\tAFmmF2sw\n"
)
JAR_SIGNED_OUT = (
    "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1790000000\tPREF\tf6=400\n"
)


def test_cookies_state_reads_the_sign_in_cookie(tmp_path) -> None:
    from outriggarr.source import cookies_state, has_signin_cookie

    assert cookies_state(None) == "none" and cookies_state("") == "none"
    assert cookies_state(tmp_path / "missing.txt") == "unreadable"
    jar = tmp_path / "c.txt"
    jar.write_text(JAR_SIGNED_IN)
    assert cookies_state(jar) == "signed in"
    jar.write_text(JAR_SIGNED_OUT)
    assert cookies_state(jar) == "signed out"
    # a LOGIN_INFO on another site does not count
    assert has_signin_cookie(".example.com\tTRUE\t/\tTRUE\t1\tLOGIN_INFO\tx\n") is False


def test_a_jar_that_lost_the_sign_in_is_never_written_back(monkeypatch, tmp_path, caplog) -> None:
    import logging

    import yt_dlp

    from outriggarr.source import YtDlpSource

    jar = tmp_path / "cookies.txt"
    jar.write_text(JAR_SIGNED_IN)

    from yt_dlp.utils import DownloadError

    class SignsOut:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            if "cookiefile" not in self.opts:
                return False
            # YouTube cleared LOGIN_INFO during the run; yt-dlp saves what is left
            Path(self.opts["cookiefile"]).write_text(JAR_SIGNED_OUT + "rotated\n")
            return False

        def extract_info(self, url, download=False):
            if "cookiefile" not in self.opts:  # an age gate: cookies come on the retry
                raise DownloadError("ERROR: [youtube] x: Sign in to confirm your age")
            return {"id": "x", "title": "t", "webpage_url": url}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", SignsOut)
    src = YtDlpSource(extra_opts=lambda: {"cookiefile": str(jar)})
    with caplog.at_level(logging.WARNING, logger="outriggarr.source"):
        src.fetch_info("https://youtu.be/x")
    assert jar.read_text() == JAR_SIGNED_IN, "the operator's signed-in export is kept"
    assert any("signed the cookie session out" in r.getMessage() for r in caplog.records)


def test_ytdlp_logger_demotes_the_known_sabr_notice(caplog) -> None:
    import logging

    from outriggarr.source import _YtDlpLogger

    with caplog.at_level(logging.DEBUG, logger="outriggarr.source"):
        _YtDlpLogger().warning(
            "[youtube] x: Some web_embedded client https formats have been skipped as they "
            "are missing a URL. YouTube may have enabled the SABR-only streaming experiment"
        )
        _YtDlpLogger().warning("[youtube] x: something that matters")
    levels = {r.getMessage()[:20]: r.levelno for r in caplog.records}
    assert levels["[youtube] x: Some we"] == logging.DEBUG
    assert levels["[youtube] x: somethi"] == logging.WARNING


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self):
        return self._data


def _archive_http(pages, collection=True):
    calls = []

    def get(url, params=None, timeout=None, headers=None):
        calls.append((url, params))
        if url.endswith("/metadata/scam_school"):
            return _Resp(
                {
                    "metadata": {
                        "mediatype": "collection" if collection else "movies",
                        "title": "Scam School",
                    }
                }
            )
        if "advancedsearch" in url:
            return _Resp({"response": {"docs": pages[params["page"] - 1]}})
        raise AssertionError(url)

    return get, calls


def test_archive_collection_is_listed_through_the_search_api(monkeypatch) -> None:
    from outriggarr import source as src_mod
    from outriggarr.source import YtDlpSource, strip_collection_prefix

    monkeypatch.setattr(src_mod, "ARCHIVE_ROWS", 2)
    pages = [
        [
            {
                "identifier": "Scam_School_194",
                "title": "Scam School 194: The Amazing iCard",
                "date": "2011-11-30T00:00:00Z",
                "mediatype": "movies",
            },
            {
                "identifier": "Scam_School_2_audio",
                "title": "Scam School 2 - audio",
                "date": "2008-01-01",
                "mediatype": "audio",
            },
        ],
        [
            {
                "identifier": "Scam_School_34",
                "title": "Scam School 34 - It's In the Bank",
                "date": None,
                "mediatype": "movies",
            }
        ],
    ]
    get, calls = _archive_http(pages)
    src = YtDlpSource(http_get=get)
    refs = src.list_recent("https://archive.org/details/scam_school?tab=collection", 50)
    assert [(r.id, r.title, r.url, r.upload_date, r.playlist_index) for r in refs] == [
        (
            "Scam_School_194",
            "The Amazing iCard",
            "https://archive.org/details/Scam_School_194",
            "20111130",
            1,
        ),
        (
            "Scam_School_34",
            "It's In the Bank",
            "https://archive.org/details/Scam_School_34",
            None,
            2,
        ),
    ], "movies only, prefix stripped, date → YYYYMMDD, two pages"
    assert [p["page"] for _, p in calls if p] == [1, 2] and calls[0][0].endswith(
        "/metadata/scam_school"
    )
    assert src.resolve("https://archive.org/details/scam_school") == refs, "Grab lists it too"
    assert strip_collection_prefix("Scam School: Just a colon", "Scam School") == "Just a colon"
    assert strip_collection_prefix("Unrelated title", "Scam School") == "Unrelated title"
    assert strip_collection_prefix("Scam School 5:", "Scam School") == "Scam School 5:", (
        "never empty"
    )


def test_archive_item_and_other_hosts_go_through_ytdlp(monkeypatch) -> None:
    import yt_dlp

    from outriggarr.source import SourceError, YtDlpSource

    seen = []

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
    get, calls = _archive_http([[]], collection=False)  # a single item, not a collection
    src = YtDlpSource(http_get=get)
    assert [v.id for v in src.list_recent("https://archive.org/details/scam_school", 50)] == ["x"]
    assert len(calls) == 1, "one metadata call, then yt-dlp"
    src.list_recent("https://vimeo.com/channels/staffpicks", 5)
    assert len(calls) == 1, "other hosts never touch archive.org"

    # the API failing is a clear, verbatim error, not a silent empty listing
    def failing(url, params=None, timeout=None, headers=None):
        return _Resp({}, status=503)

    with pytest.raises(SourceError, match="archive.org"):
        YtDlpSource(http_get=failing).list_recent("https://archive.org/details/scam_school", 50)


@pytest.mark.parametrize(
    ("tag", "code"),
    [
        ("ja", "jpn"),
        ("ja-JP", "jpn"),
        ("en-US", "eng"),
        ("zh-Hans", "chi"),
        ("ko", "kor"),
        ("jpn", "jpn"),
        ("und", None),
        ("zxx", None),
        ("xx", None),
        ("", None),
        (None, None),
    ],
)
def test_iso639_2_mapping(tag, code) -> None:
    from outriggarr.source import iso639_2

    assert iso639_2(tag) == code


def test_detected_audio_language_reads_the_chosen_audio_track() -> None:
    from outriggarr.source import detected_audio_language

    merged = {
        "language": "en",  # the video's page language is not the audio track's
        "requested_formats": [
            # the video-only format may carry a language too; the audio track's is the one
            {"format_id": "137", "vcodec": "avc1", "acodec": "none", "language": "en"},
            {"format_id": "140-1", "vcodec": "none", "acodec": "mp4a", "language": "ja"},
        ],
    }
    assert detected_audio_language(merged) == "jpn"
    single = {"language": "ko", "requested_downloads": [{"language": "ko-KR"}]}
    assert detected_audio_language(single) == "kor"
    assert detected_audio_language({"language": "und"}) is None
    assert detected_audio_language({}) is None


def test_cookies_are_used_only_when_youtube_asks_for_a_sign_in(monkeypatch, tmp_path) -> None:
    import yt_dlp
    from yt_dlp.utils import DownloadError

    from outriggarr.source import SourceError, YtDlpSource

    jar = tmp_path / "cookies.txt"
    jar.write_text(JAR_SIGNED_IN)
    calls: list[dict] = []
    fail_without = {"msg": "ERROR: [youtube] x: Sign in to confirm your age. Use --cookies…"}

    class StubYDL:
        def __init__(self, opts):
            calls.append(opts)
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if "cookiefile" not in self.opts and fail_without["msg"]:
                raise DownloadError(fail_without["msg"])
            return {"id": "x", "title": "t", "webpage_url": url}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", StubYDL)
    src = YtDlpSource(extra_opts=lambda: {"cookiefile": str(jar)})
    # an age gate: the first attempt carries no cookies, the retry does
    src.fetch_info("https://youtu.be/x")
    assert [("cookiefile" in c) for c in calls] == [False, True]
    # a public video: one attempt, no cookies, even though a file is configured
    calls.clear()
    fail_without["msg"] = ""
    src.fetch_info("https://youtu.be/x")
    assert [("cookiefile" in c) for c in calls] == [False]
    # any other error is not worth a signed-in retry
    calls.clear()
    fail_without["msg"] = "ERROR: [youtube] x: Video unavailable"
    with pytest.raises(SourceError, match="Video unavailable"):
        src.fetch_info("https://youtu.be/x")
    assert len(calls) == 1
    # no cookies configured: the sign-in error is simply the error
    calls.clear()
    fail_without["msg"] = "ERROR: [youtube] x: Sign in to confirm your age."
    with pytest.raises(SourceError, match="Sign in"):
        YtDlpSource(extra_opts=lambda: {}).fetch_info("https://youtu.be/x")
    assert len(calls) == 1


def test_expected_sign_in_error_is_not_logged_as_an_error(caplog, tmp_path) -> None:
    import logging

    from outriggarr.source import YtDlpSource, _YtDlpLogger

    msg = "ERROR: [youtube] x: Sign in to confirm your age."
    with caplog.at_level(logging.DEBUG, logger="outriggarr.source"):
        _YtDlpLogger(expected_login=True).error(msg)
        _YtDlpLogger(expected_login=False).error(msg)
        _YtDlpLogger(expected_login=True).error("ERROR: [youtube] x: Video unavailable")
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.DEBUG, logging.ERROR, logging.ERROR]
    jar = tmp_path / "c.txt"
    jar.write_text("# cookies")
    src = YtDlpSource(extra_opts=lambda: {"cookiefile": str(jar)})
    assert src._opts({}, cookies=False)["logger"]._expected_login is True
    assert src._opts({}, cookies=True)["logger"]._expected_login is False
    assert YtDlpSource()._opts({}, cookies=False)["logger"]._expected_login is False


def test_listing_leaves_out_premieres_live_streams_and_shorts(caplog) -> None:
    import logging

    info = {
        "_type": "playlist",
        "id": "PL9",
        "entries": [
            {"id": "up", "title": "Premieres Thursday", "live_status": "is_upcoming"},
            {"id": "live", "title": "Live now", "live_status": "is_live"},
            {"id": "sh", "title": "A Short", "url": "https://www.youtube.com/shorts/sh"},
            {"id": "was", "title": "Streamed earlier", "live_status": "was_live"},
            {"id": "mem", "title": "Members only", "availability": "subscriber_only"},
            {"id": "ok", "title": "Plain"},
        ],
    }
    with caplog.at_level(logging.INFO, logger="outriggarr.source"):
        refs = videos_from_info(info)
    assert [r.id for r in refs] == ["was", "mem", "ok"], (
        "a finished stream and a members-only video are downloadable; the rest are not"
    )
    assert (
        "listing PL9: left out 1 x upcoming premiere, 1 x live stream in progress, 1 x Short"
        in caplog.text
    )


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"live_status": "is_upcoming"}, "upcoming premiere"),
        ({"live_status": "is_live"}, "live stream in progress"),
        ({"url": "https://www.youtube.com/shorts/abc"}, "Short"),
        ({"live_status": "was_live"}, None),
        ({"availability": "subscriber_only"}, None),
        ({"url": "https://www.youtube.com/watch?v=abc"}, None),
        ({}, None),
    ],
)
def test_skip_reason(entry: dict, why: str | None) -> None:
    assert skip_reason(entry) == why


def test_a_single_pasted_video_is_never_second_guessed() -> None:
    info = {"id": "up", "title": "Premiere", "live_status": "is_upcoming"}
    assert [r.id for r in videos_from_info(info)] == ["up"]


@pytest.mark.parametrize(
    ("message", "limited"),
    [
        (
            "ERROR: [youtube] abc: This content isn't available, try again later. "
            "The current session has been rate-limited by YouTube for up to an hour.",
            True,
        ),
        ("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests", True),
        ("ERROR: [youtube] abc: Video unavailable", False),
        ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", False),
        ("ERROR: [youtube] abc: This live event will begin in 3 hours.", False),
    ],
)
def test_is_rate_limited(message: str, limited: bool) -> None:
    assert is_rate_limited(message) is limited


def test_cooloff_escalates_only_after_a_pause_proved_too_short() -> None:
    t = [1000.0]
    c = CoolOff(clock=lambda: t[0])
    assert not c.active() and c.remaining() == 0
    assert c.hit("first") == 900 and c.active() and c.message == "first"
    t[0] += 100
    assert c.remaining() == 800
    # answers during a pause in force are the same wall, not more strikes
    assert c.hit("second") == 800 and c.strikes == 1 and c.message == "first"
    t[0] = 1900
    assert not c.active()
    assert c.hit("third") == 1800 and c.strikes == 2, "the last pause was too short: double it"
    t[0] = 3700
    assert c.hit("fourth") == 3600
    t[0] = 7300
    assert c.hit("fifth") == 3600, "capped at an hour, YouTube's own figure"
    c.clear()
    assert c.strikes == 0 and c.message is None and not c.active()
    assert c.hit("again") == 900, "a success resets the ladder"


@pytest.mark.parametrize(
    ("message", "permanent"),
    [
        ("ERROR: [youtube] a: Video unavailable", True),
        ("ERROR: [youtube] a: This video has been removed by the uploader", True),
        (
            "ERROR: [youtube] a: This video is no longer available because the YouTube account "
            "associated with this video has been terminated.",
            True,
        ),
        ("ERROR: [youtube] a: Private video. Sign in if you've been granted access", True),
        ("ERROR: [youtube] a: Join this channel to get access to members-only content", True),
        ("ERROR: [youtube] a: Sign in to confirm your age", True),
        (
            "ERROR: [youtube] a: The uploader has not made this video available in your country",
            True,
        ),
        ("ERROR: [youtube] a: Requested format is not available", False),  # transient on YouTube
        ("ERROR: Unsupported URL: https://example.com/x", True),
        ("ERROR: unable to download webpage: HTTP Error 404: Not Found", True),
        # the address being busy, not the video: retried
        ("ERROR: [youtube] a: Sign in to confirm you're not a bot", False),
        (
            "ERROR: [youtube] a: This content isn't available, try again later. The current "
            "session has been rate-limited by YouTube for up to an hour.",
            False,
        ),
        ("ERROR: [youtube] a: This live event will begin in 3 hours.", False),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", False),
        ("ERROR: Unable to download webpage: <urlopen error timed out>", False),
    ],
)
def test_is_permanent_failure(message: str, permanent: bool) -> None:
    assert is_permanent_failure(message) is permanent


@pytest.mark.parametrize(
    ("age_seconds", "text"),
    [
        (3 * 365 * 86400, "3 years ago"),
        (365 * 86400, "1 year ago"),
        (2 * 30 * 86400, "2 months ago"),
        (7 * 86400, "1 week ago"),
        (2 * 86400, "2 days ago"),
        (5 * 3600, "5 hours ago"),
        (120, "today"),
    ],
)
def test_relative_age_reads_the_unit_back(age_seconds: int, text: str) -> None:
    now = 1_800_000_000.0
    assert relative_age(now - age_seconds, now=now) == text
    assert relative_age(None, now=now) is None
    assert relative_age(now + 60, now=now) is None, "the future is not an age"


def test_flat_entries_carry_an_approximate_age_not_a_date() -> None:
    import time

    now = time.time()
    info = {
        "_type": "playlist",
        "id": "PL1",
        "entries": [
            {"id": "old", "title": "Old", "timestamp": now - 3 * 365 * 86400},
            {"id": "new", "title": "New", "timestamp": now - 2 * 86400},
            {"id": "dated", "title": "Dated", "timestamp": now - 86400, "upload_date": "20260901"},
            {"id": "bare", "title": "Bare"},
        ],
    }
    refs = {r.id: r for r in videos_from_info(info)}
    assert refs["old"].approx_age == "3 years ago" and refs["old"].upload_date is None
    assert refs["new"].approx_age == "2 days ago" and refs["new"].upload_date is None
    assert refs["dated"].approx_age is None and refs["dated"].upload_date == "20260901", (
        "a real date needs no guess"
    )
    assert refs["bare"].approx_age is None


def test_listing_asks_youtube_for_approximate_dates() -> None:
    from outriggarr.source import YtDlpSource

    source = YtDlpSource(extra_opts=lambda: {})
    opts = source._opts({"extractor_args": {"youtubetab": {"skip": ["webpage"]}}})
    assert opts["extractor_args"]["youtubetab"] == {"approximate_date": ["1"], "skip": ["webpage"]}


class _ScriptedYDL:
    """A yt-dlp stand-in that drives the progress hook with a scripted sequence."""

    script: list[dict] = []
    info: dict = {}
    seen_opts: list[dict] = []

    def __init__(self, opts):
        self.opts = opts
        type(self).seen_opts.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        for event in type(self).script:
            for hook in self.opts.get("progress_hooks", []):
                hook(event)
        return dict(type(self).info)


def _drive_download(monkeypatch, tmp_path, script, progress, should_abort=lambda: False):
    import yt_dlp

    from outriggarr.source import YtDlpSource

    out = tmp_path / "v1.mkv"
    out.write_bytes(b"x")
    _ScriptedYDL.script = script
    _ScriptedYDL.info = {"id": "v1", "title": "t", "requested_downloads": [{"filepath": str(out)}]}
    _ScriptedYDL.seen_opts = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _ScriptedYDL)
    src = YtDlpSource(extra_opts=lambda: {})
    return src.download(
        "https://y/v1",
        tmp_path,
        fmt="best",
        merge_container="mkv",
        progress=progress,
        should_abort=should_abort,
    )


def test_download_progress_is_cumulative_across_video_and_audio(monkeypatch, tmp_path) -> None:
    two = {"info_dict": {"requested_formats": [{}, {}]}}
    script = [
        {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, **two},
        {"status": "downloading", "downloaded_bytes": 100, "total_bytes": 100, **two},
        {"status": "finished", **two},
        {"status": "downloading", "downloaded_bytes": 0, "total_bytes": 40, **two},  # audio starts
        {"status": "downloading", "downloaded_bytes": 20, "total_bytes": 40, **two},
        {"status": "finished", **two},
    ]
    seen: list[float] = []
    result = _drive_download(monkeypatch, tmp_path, script, seen.append)
    assert seen == [25.0, 50.0, 50.0, 75.0], "one figure that never drops back"
    assert result.video_id == "v1"


def test_a_failing_progress_callback_does_not_discard_the_download(monkeypatch, tmp_path) -> None:
    script = [{"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2}]

    def boom(pct):
        raise RuntimeError("database is locked")

    result = _drive_download(monkeypatch, tmp_path, script, boom)
    assert result.path.exists()


def test_the_hook_aborts_on_request(monkeypatch, tmp_path) -> None:
    from outriggarr.source import DownloadAborted

    script = [{"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2}]
    with pytest.raises(DownloadAborted):
        _drive_download(monkeypatch, tmp_path, script, lambda p: None, should_abort=lambda: True)


def test_fetch_info_refuses_a_collection_and_stays_flat(monkeypatch) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    _ScriptedYDL.script = []
    _ScriptedYDL.info = {
        "_type": "playlist",
        "id": "PL",
        "entries": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
    }
    _ScriptedYDL.seen_opts = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _ScriptedYDL)
    with pytest.raises(SourceError, match="not a single video \\(2 entries"):
        YtDlpSource(extra_opts=lambda: {}).fetch_info("https://www.youtube.com/playlist?list=PL")
    assert _ScriptedYDL.seen_opts[0]["extract_flat"] == "in_playlist", "never one request per entry"


def _ytdlp_stamp(listed, precision_seconds: int) -> float:
    # yt-dlp: datetime_round(now - N units, unit): weeks/months/years round to the DAY,
    # hours to the hour, half-up
    raw = listed.timestamp()
    return ((raw + precision_seconds / 2) // precision_seconds) * precision_seconds


@pytest.mark.parametrize("clock", ["03:00", "13:00", "23:30"])
@pytest.mark.parametrize(
    ("back", "precision", "text"),
    [
        ({"days": 2}, 86400, "2 days ago"),
        ({"days": 7}, 86400, "1 week ago"),
        ({"days": 13}, 86400, "1 week ago"),
        ({"hours": 5}, 3600, "5 hours ago"),
        ({"years": 3}, 86400, "3 years ago"),
    ],
)
def test_relative_age_reads_back_what_the_listing_said(
    clock: str, back: dict, precision: int, text: str
) -> None:
    from datetime import UTC, datetime, timedelta

    h, m = (int(x) for x in clock.split(":"))
    now = datetime(2026, 9, 2, h, m, tzinfo=UTC)
    if "years" in back:
        listed = now.replace(year=now.year - back["years"])
    else:
        listed = now - timedelta(**back)
    assert relative_age(_ytdlp_stamp(listed, precision), now=now.timestamp()) == text


def test_relative_age_calendar_month() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 3, 30, 13, 0, tzinfo=UTC).timestamp()
    assert relative_age(now - 28 * 86400, now=now) == "1 month ago", "February is a month too"


def test_list_recent_still_returns_the_newest_n_past_a_premiere_and_a_live_stream(
    monkeypatch,
) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    _ScriptedYDL.script = []
    _ScriptedYDL.info = {
        "_type": "playlist",
        "id": "UC1",
        "entries": [
            {"id": "up", "title": "Premiere", "live_status": "is_upcoming"},
            {"id": "live", "title": "Live", "live_status": "is_live"},
            *({"id": f"v{i}", "title": f"V{i}"} for i in range(6)),
        ],
    }
    _ScriptedYDL.seen_opts = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _ScriptedYDL)
    refs = YtDlpSource(extra_opts=lambda: {}).list_recent("https://www.youtube.com/@c", 3)
    assert [r.id for r in refs] == ["v0", "v1", "v2"]
    assert _ScriptedYDL.seen_opts[0]["playlistend"] == 8, "a few extra cover the entries left out"


def test_cooloff_strikes_reset_once_a_pause_is_long_over() -> None:
    t = [0.0]
    c = CoolOff(clock=lambda: t[0])
    assert c.hit("a") == 900
    t[0] = 1000
    assert c.hit("b") == 1800, "the last pause was too short: escalate"
    t[0] = 1000 + 1800 + 3600 + 1  # more than the cap after the pause ended
    assert c.hit("c") == 900, "a wall a long time ago is no reason to start high"


def test_channel_home_tab_lists_the_uploads_tab() -> None:
    from outriggarr.source import channel_videos_url

    assert (
        channel_videos_url("https://www.youtube.com/@c/featured")
        == "https://www.youtube.com/@c/videos"
    )
    assert (
        channel_videos_url("https://www.youtube.com/@c/featured?x=1")
        == "https://www.youtube.com/@c/videos"
    )


def test_private_cookie_jar_leaves_no_temp_file_when_the_copy_fails(monkeypatch, tmp_path) -> None:
    import shutil
    import tempfile

    from outriggarr.source import YtDlpSource

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        shutil, "copyfile", lambda a, b: (_ for _ in ()).throw(OSError(28, "No space left"))
    )
    src = YtDlpSource(extra_opts=lambda: {"cookiefile": str(jar)})
    with pytest.raises(OSError), src._private_cookie_jar({"cookiefile": str(jar)}):
        pass
    assert not list(tmp_path.glob("outriggarr-cookies-*")), "no orphaned private copy"


def test_archive_search_answers_with_lists_and_odd_docs_are_tolerated() -> None:
    from outriggarr.source import YtDlpSource

    pages = [
        [
            {
                "identifier": ["Scam_School_7"],
                "title": ["Scam School 7: Lists"],
                "date": ["2011-11-30T00:00:00Z"],
                "mediatype": ["movies"],
            },
            "not a dict at all",
            {
                "identifier": "Scam_School_8",
                "title": "Scam School 8",
                "date": [],
                "mediatype": "movies",
            },
        ]
    ]
    get, _calls = _archive_http(pages)
    refs = YtDlpSource(http_get=get).list_recent("https://archive.org/details/scam_school", 50)
    assert [(r.id, r.title, r.upload_date) for r in refs] == [
        ("Scam_School_7", "Lists", "20111130"),
        ("Scam_School_8", "Scam School 8", None),
    ]
