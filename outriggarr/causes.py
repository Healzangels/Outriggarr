"""A plain-words reading of a failed job's error text, for the Activity page.

The error itself stays verbatim on the job (CLAUDE.md); this only adds the sentence a
seasoned operator would say next: what it usually means and what to do about it. Pure
function over the text plus the one bit of state that changes the advice (whether a
signed-in cookies file is in play), so it is cheap to test and safe to call per row.
"""

from __future__ import annotations

import re

_R = re.IGNORECASE

# (pattern, advice) — first match wins, so the specific comes before the general.
_YOUTUBE_SESSION: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"confirm you.re not a bot", _R),
        "YouTube's bot check: this address has been busy. It passes again by itself after a "
        "while, and the job retries on its own.",
    ),
    (
        re.compile(r"members[- ]only|join this channel", _R),
        "Members-only video: {session}",
    ),
    (
        re.compile(r"this video is private|private video", _R),
        "The video is private: {session}",
    ),
    (
        re.compile(r"sign in to confirm your age|age[- ]restricted", _R),
        "Age-gated video: {session}",
    ),
)

_SESSION_ADVICE = {
    "none": "no cookies file is configured, so YouTube sees no account. Export one from a "
    "private window and set its path under Settings, then Retry.",
    "unreadable": "the cookies file cannot be read by the app user. Fix its ownership or mode, "
    "then Retry.",
    "signed out": "the cookies file carries no live sign-in. Export it again from a private "
    "window (then close that window), then Retry.",
    "signed in": "the signed-in account has no access to it. Pin another upload to the episode.",
}

_GENERAL: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"rate[- ]limited|try again later|http error 429|too many requests", _R),
        "YouTube rate-limited the session. Everything pauses and resumes by itself; nothing to do.",
    ),
    (
        re.compile(r"account associated with this video has been terminated", _R),
        "The channel is gone from YouTube. Pin another upload to the episode.",
    ),
    (
        re.compile(r"video unavailable|has been removed|no longer available", _R),
        "The video is gone from YouTube (removed, or never public). Pin another upload to "
        "the episode.",
    ),
    (
        re.compile(
            r"not available in your country|not made this video available|blocked it in "
            r"your country",
            _R,
        ),
        "Geo-blocked from this network. Only a route through another country would fetch it.",
    ),
    (
        re.compile(r"live event will begin|premieres in|this live event", _R),
        "A scheduled premiere: the video does not exist yet. The job retries by itself once it "
        "has aired.",
    ),
    (
        re.compile(r"requested format is not available", _R),
        "The format string asks for something this video does not offer. Loosen it under "
        "Settings (or the subscription's override), e.g. end it with /best, then Retry.",
    ),
    (
        re.compile(r"unsupported url|is not a valid url", _R),
        "yt-dlp has no extractor for this address. Check the URL; some sites are not supported.",
    ),
    (
        re.compile(r"http error 40[34]|http error 410", _R),
        "YouTube refused the download URL, which expires quickly. A fresh attempt usually works; "
        "the job retries by itself.",
    ),
    (
        re.compile(r"no download progress|download still running", _R),
        "The download stalled and was abandoned. The job retries by itself; a stall every time "
        "points at the network or at YouTube throttling this address.",
    ),
    (
        re.compile(r"ffmpeg", _R),
        "ffmpeg could not process the file. The verbatim ffmpeg text below says which stream "
        "or container it objected to.",
    ),
    (
        re.compile(r"staging error", _R),
        "The staging folder is not writable by the app user (see the footer). Fix the mount's "
        "ownership or mode; the job retries by itself.",
    ),
    (
        re.compile(r"import rejected|manualimport", _R),
        "Sonarr/Radarr refused the import. The rejection text below names the reason (quality "
        "not in the profile, path not visible, episode mismatch); fix it and press Retry, "
        "the staged file is kept.",
    ),
    (
        re.compile(r"some of the target episodes already have a file", _R),
        "A multi-episode file would replace episodes that already have one. Split the target "
        "or delete those files first.",
    ),
    (
        re.compile(r"cookies file .* is not readable", _R),
        "The cookies file exists but the app user cannot read it. Fix its ownership or mode, "
        "then Retry.",
    ),
    (
        re.compile(r"internal error", _R),
        "A bug in Outriggarr, not in the download. The traceback is in the container log; "
        "please report it.",
    ),
)


def likely_cause(error: str | None, *, youtube_session: str = "none") -> str | None:
    """One sentence on what the error usually means and what to do. `youtube_session` is
    the cookies state from the footer (none / unreadable / signed out / signed in), which
    turns a sign-in error into the right instruction. None when nothing fits."""
    if not error:
        return None
    for pattern, advice in _YOUTUBE_SESSION:
        if pattern.search(error):
            session = _SESSION_ADVICE.get(youtube_session, _SESSION_ADVICE["none"])
            return advice.format(session=session)
    for pattern, advice in _GENERAL:
        if pattern.search(error):
            return advice
    return None
