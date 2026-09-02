from pathlib import Path

import pytest

from outriggarr.source import SourceError, VideoRef, videos_from_info


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

    from outriggarr.source import SourceError, YtDlpSource

    src = tmp_path / "a.mkv"
    src.write_bytes(b"orig")
    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"tagged")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    from pathlib import Path

    monkeypatch.setattr(subprocess, "run", fake_run)
    YtDlpSource().tag_audio_language(src, "eng")
    assert src.read_bytes() == b"tagged"
    assert not (tmp_path / "a.lang.mkv").exists()
    assert calls[0][cmd_idx := calls[0].index("-metadata:s:a") + 1] == "language=eng" and cmd_idx

    def failing_run(cmd, capture_output, text):
        Path(cmd[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(cmd, 1, "", "Invalid data found when processing input")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(SourceError, match="Invalid data found"):
        YtDlpSource().tag_audio_language(src, "eng")
    assert src.read_bytes() == b"tagged", "original untouched on failure"
    assert not (tmp_path / "a.lang.mkv").exists(), "temp output removed on failure"


def test_ytdlp_source_merges_extra_opts_last(monkeypatch, tmp_path) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    seen: list[dict] = []

    class StubYDL:
        def __init__(self, opts):
            # yt-dlp gets a private copy of the operator's jar; read it while it exists
            seen.append({**opts, "_jar": Path(opts["cookiefile"]).read_text()})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "x", "title": "t", "webpage_url": url}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", StubYDL)
    cookies = tmp_path / "c.txt"
    cookies.write_text("# cookies")
    src = YtDlpSource(extra_opts=lambda: {"cookiefile": str(cookies), "quiet": False})
    (v,) = src.resolve("https://youtu.be/x")
    assert v.id == "x"
    assert seen[0]["cookiefile"] != str(cookies) and seen[0]["_jar"] == "# cookies"
    assert seen[0]["quiet"] is False, "operator options win over ours"
    assert seen[0]["extract_flat"] == "in_playlist" and "logger" in seen[0]
    src.list_recent("https://www.youtube.com/@c", 7)
    assert seen[1]["playlistend"] == 7 and seen[1]["_jar"] == "# cookies"


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
    assert seen[-1][0].endswith("/videos") and seen[-1][1]["playlistend"] == 3
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
    with pytest.raises(SourceError, match="cookies file"):
        src.resolve("https://youtu.be/x")


def test_ytdlp_gets_a_private_cookie_jar_and_never_clobbers_a_replaced_file(
    monkeypatch, tmp_path
) -> None:
    import yt_dlp

    from outriggarr.source import YtDlpSource

    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\nold session\n")
    seen: dict = {}

    class StubYDL:
        def __init__(self, opts):
            self.opts = opts
            seen["cookiefile"] = opts["cookiefile"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            # yt-dlp saves its jar on close: the session it loaded, plus rotations
            path = Path(self.opts["cookiefile"])
            path.write_text(path.read_text() + "rotated\n")
            return False

        def extract_info(self, url, download=False):
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
    }, "ours plus the operator's, merged per extractor"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    YtDlpSource(extra_opts=lambda: {}, pot_server_home=home).resolve("https://youtu.be/x")
    assert "extractor_args" not in seen[1], "no node: nothing is promised to yt-dlp"


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

    class SignsOut:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            # YouTube cleared LOGIN_INFO during the run; yt-dlp saves what is left
            Path(self.opts["cookiefile"]).write_text(JAR_SIGNED_OUT + "rotated\n")
            return False

        def extract_info(self, url, download=False):
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
            {"format_id": "137", "vcodec": "avc1", "acodec": "none", "language": None},
            {"format_id": "140-1", "vcodec": "none", "acodec": "mp4a", "language": "ja"},
        ],
    }
    assert detected_audio_language(merged) == "jpn"
    single = {"language": "ko", "requested_downloads": [{"language": "ko-KR"}]}
    assert detected_audio_language(single) == "kor"
    assert detected_audio_language({"language": "und"}) is None
    assert detected_audio_language({}) is None
