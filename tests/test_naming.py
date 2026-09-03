import pytest

from outriggarr.naming import (
    episode_code,
    episode_filename,
    movie_filename,
    quality_for_height,
    quality_from_filename,
    sanitize,
)


@pytest.mark.parametrize(
    ("height", "expected"),
    [
        (None, "WEBDL-480p"),
        (0, "WEBDL-480p"),
        (480, "WEBDL-480p"),
        (719, "WEBDL-480p"),
        (720, "WEBDL-720p"),
        (1079, "WEBDL-720p"),
        (1080, "WEBDL-1080p"),
        (1440, "WEBDL-1080p"),
        (2159, "WEBDL-1080p"),
        (2160, "WEBDL-2160p"),
        (4320, "WEBDL-2160p"),
    ],
)
def test_quality_ladder(height: int | None, expected: str) -> None:
    assert quality_for_height(height) == expected


def test_sanitize_strips_path_and_control_chars() -> None:
    assert sanitize('A/B\\C:D*E?F"G<H>I|J') == "A-B-C-D-E-F-G-H-I-J"
    assert sanitize("  many   spaces\t here ") == "many spaces here"
    assert sanitize("trailing dot.") == "trailing dot"
    assert sanitize("ctrl\x01char") == "ctrl-char"


def test_episode_code_single_and_range() -> None:
    assert episode_code(1, [2]) == "S01E02"
    assert episode_code(12, [5, 3, 4]) == "S12E03-E05"
    assert episode_code(2026, [33]) == "S2026E33"
    with pytest.raises(ValueError):
        episode_code(1, [])


def test_episode_filename() -> None:
    name = episode_filename("Kill: Tony", 2026, [33], "#783 - GARY/OWEN", "WEBDL-1080p", "mkv")
    assert name == "Kill- Tony - S2026E33 - #783 - GARY-OWEN [WEBDL-1080p].mkv"
    assert quality_from_filename(name) == "WEBDL-1080p"


def test_episode_filename_without_title_and_multi() -> None:
    name = episode_filename("Show", 1, [1, 2], "", "WEBDL-720p", ".mp4")
    assert name == "Show - S01E01-E02 [WEBDL-720p].mp4"


def test_episode_filename_truncates_long_stem() -> None:
    name = episode_filename("S" * 300, 1, [1], "T" * 300, "WEBDL-480p", "mkv")
    assert name.endswith(" [WEBDL-480p].mkv")
    assert len(name) <= 180 + len(" [WEBDL-480p].mkv")


def test_movie_filename() -> None:
    assert (
        movie_filename("Some: Film", 1999, "WEBDL-2160p", "mkv")
        == "Some- Film (1999) [WEBDL-2160p].mkv"
    )
    assert movie_filename("No Year", None, "WEBDL-720p", "mkv") == "No Year [WEBDL-720p].mkv"


def test_quality_from_filename_absent() -> None:
    assert quality_from_filename("random.mkv") is None


def test_stem_is_capped_by_utf8_bytes_for_linux_name_max() -> None:
    cjk = "日本語のタイトル" * 30  # 3 bytes per character
    name = episode_filename(cjk, 1, [1], cjk, "WEBDL-2160p", "mkv")
    assert name.endswith(" [WEBDL-2160p].mkv")
    assert len(name.encode("utf-8")) + len(".en.srt") <= 255, "video + sidecar fit NAME_MAX"
    assert "�" not in name, "never split a character"
    movie = movie_filename("🍿" * 300, 2020, "WEBDL-1080p", "mkv")
    assert len(movie.encode("utf-8")) <= 240


def test_episode_code_survives_a_long_series_title() -> None:
    from outriggarr.naming import MAX_STEM, MAX_STEM_BYTES, episode_filename

    name = episode_filename("S" * 300, 1, [1], "Title", "WEBDL-1080p", "mkv")
    stem = name.rsplit(" [", 1)[0]
    assert "S01E01" in name and len(stem) <= MAX_STEM and len(stem.encode()) <= MAX_STEM_BYTES
    assert stem.endswith("S01E01") or " - S01E01 - " in stem
    cjk = episode_filename("剧" * 300, 2, [3, 4], "标题" * 100, "WEBDL-720p", "mp4")
    assert "S02E03-E04" in cjk and len(cjk.rsplit(" [", 1)[0].encode()) <= MAX_STEM_BYTES


def test_year_survives_a_long_movie_title_and_an_empty_title_gets_a_name() -> None:
    from outriggarr.naming import movie_filename

    name = movie_filename("🍿" * 300, 2020, "WEBDL-720p", "mkv")
    assert "(2020) [WEBDL-720p].mkv" in name
    assert movie_filename("...", 2020, "WEBDL-720p", "mkv") == "untitled (2020) [WEBDL-720p].mkv"
    assert movie_filename("///", None, "WEBDL-720p", "mkv") == "--- [WEBDL-720p].mkv"


def test_quality_tag_is_read_from_the_tail_only() -> None:
    from outriggarr.naming import quality_from_filename

    assert (
        quality_from_filename("Show - S01E01 - Old cut [WEBDL-720p] remux [WEBDL-1080p].mkv")
        == "WEBDL-1080p"
    )
    assert quality_from_filename("Show - S01E01 - Old cut [WEBDL-720p].mkv") == "WEBDL-720p"
    assert quality_from_filename("Show - S01E01 - no tag.mkv") is None


def test_sanitize_drops_del_too() -> None:
    from outriggarr.naming import sanitize

    assert sanitize("a\x7fb") == "a-b"
