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
