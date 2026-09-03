import pytest

from outriggarr.causes import likely_cause

RATE = (
    "ERROR: [youtube] x: This content isn't available, try again later. "
    "The current session has been rate-limited by YouTube for up to an hour."
)


@pytest.mark.parametrize(
    ("error", "session", "expect"),
    [
        (None, "none", None),
        ("", "signed in", None),
        ("something nobody wrote a hint for", "signed in", None),
        # a sign-in error reads differently depending on the cookies state
        (
            "ERROR: [youtube] a: Sign in to confirm your age",
            "none",
            "Age-gated video: no cookies file is configured",
        ),
        (
            "ERROR: [youtube] a: Sign in to confirm your age",
            "signed out",
            "Age-gated video: the cookies file carries no live sign-in",
        ),
        (
            "ERROR: [youtube] a: Join this channel to get access to members-only content",
            "signed in",
            "Members-only video: the signed-in account has no access",
        ),
        ("ERROR: [youtube] a: Private video. Sign in", "unreadable", "cannot be read by the app"),
        # the bot check is the address, not the account: never a cookies instruction
        ("ERROR: [youtube] a: Sign in to confirm you're not a bot", "none", "bot check"),
        (RATE, "signed in", "rate-limited the session"),
        ("ERROR: [youtube] a: Video unavailable", "signed in", "gone from YouTube"),
        (
            "ERROR: [youtube] a: This video is no longer available because the YouTube account "
            "associated with this video has been terminated.",
            "none",
            "channel is gone",
        ),
        ("ERROR: [youtube] a: This live event will begin in 3 hours.", "none", "premiere"),
        ("ERROR: [youtube] a: Requested format is not available", "none", "format string"),
        ("ERROR: Unsupported URL: https://x", "none", "no extractor"),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "none", "expires"),
        (
            "no download progress for 30 min (stuck at 41%); abandoned, will retry",
            "none",
            "stalled",
        ),
        ("ffmpeg exited 1: Invalid data found", "none", "ffmpeg could not"),
        ("staging error: [Errno 13] Permission denied", "none", "not writable"),
        ("import rejected: Quality not wanted in profile", "none", "refused the import"),
        ("internal error: KeyError('x')", "none", "bug in Outriggarr"),
    ],
)
def test_likely_cause(error: str | None, session: str, expect: str | None) -> None:
    cause = likely_cause(error, youtube_session=session)
    if expect is None:
        assert cause is None
    else:
        assert cause is not None and expect in cause, cause


def test_specific_reading_beats_general() -> None:
    # "Video unavailable" appears inside a members-only answer on some clients: the
    # sign-in reading wins because it tells the user what to do
    text = "ERROR: [youtube] a: Video unavailable. Join this channel to get access"
    assert likely_cause(text, youtube_session="signed in").startswith("Members-only")


@pytest.mark.parametrize(
    ("error", "expect"),
    [
        (
            "ERROR: [youtube] a: Unable to download webpage: HTTP Error 404: Not Found",
            "page is gone",
        ),
        ("ERROR: unable to download video data: HTTP Error 404: Not Found", "expires quickly"),
        ("ERROR: [youtube] a: This video is DRM protected", "DRM-protected"),
        (
            "audio language tag failed (file imported untagged): ffmpeg exited 1",
            "imported fine, only untagged",
        ),
        (
            "rate-limited answer 4 times in a row for this video while other downloads "
            "went through: x",
            "Only this video",
        ),
        ("https://x is a playlist or channel, not a single video", "Queue it from Grab"),
    ],
)
def test_causes_agree_with_the_runner(error: str, expect: str) -> None:
    cause = likely_cause(error, youtube_session="none")
    assert cause is not None and expect in cause, cause
