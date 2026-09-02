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
