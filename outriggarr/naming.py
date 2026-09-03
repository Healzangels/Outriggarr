"""Staging filename + quality mapping. Pure functions, no I/O.

The staging name is parseable by Sonarr/Radarr as a safety net; the import itself
carries explicit episode/movie ids, so parsing is never load-bearing.
"""

from __future__ import annotations

import re

QUALITY_LADDER: tuple[tuple[int, str], ...] = (
    (2160, "WEBDL-2160p"),
    (1080, "WEBDL-1080p"),
    (720, "WEBDL-720p"),
)
FALLBACK_QUALITY = "WEBDL-480p"

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_WS = re.compile(r"\s+")
# the tag WE append sits at the very end, before the extension: a title that happens to
# carry "[WEBDL-720p]" of its own must not be read as the file's quality
_QUALITY_TAG = re.compile(r"\[(WEBDL-\d{3,4}p)\]\.[^.]+$")
MAX_STEM = 180  # characters, but see MAX_STEM_BYTES: Linux limits a NAME to 255 bytes
MAX_STEM_BYTES = 200  # leaves room for " [WEBDL-2160p]" + ".ext" and a ".xx.srt" sidecar


def _fit(stem: str) -> str:
    """Trim a stem to MAX_STEM characters and MAX_STEM_BYTES of UTF-8, never splitting
    a character; CJK/emoji titles exceed NAME_MAX long before 180 characters."""
    stem = stem[:MAX_STEM]
    encoded = stem.encode("utf-8")
    if len(encoded) > MAX_STEM_BYTES:
        stem = encoded[:MAX_STEM_BYTES].decode("utf-8", errors="ignore")
    return stem.rstrip(" .")


def _fit_keeping(prefix: str, tail: str) -> str:
    """Fit `prefix + tail` so that `tail` (the parseable part: the episode code or the
    year) always survives: the prefix gives way first. Trimming the end of the whole
    stem would erase exactly the part the safety-net parse needs."""
    room = _fit(prefix)
    while room and (
        len(room) + len(tail) > MAX_STEM or len((room + tail).encode()) > MAX_STEM_BYTES
    ):
        room = _fit(room[:-1])
    return (room + tail) if room else tail.lstrip(" -")


def quality_for_height(height: int | None) -> str:
    if height is not None:
        for min_height, name in QUALITY_LADDER:
            if height >= min_height:
                return name
    return FALLBACK_QUALITY


def quality_from_filename(filename: str) -> str | None:
    m = _QUALITY_TAG.search(filename)
    return m.group(1) if m else None


def sanitize(text: str) -> str:
    text = _WS.sub(" ", text)  # tabs/newlines are whitespace first, not control chars
    text = _UNSAFE.sub("-", text)
    return text.strip(" .")


def episode_code(season: int, episode_numbers: list[int]) -> str:
    numbers = sorted(set(episode_numbers))
    if not numbers:
        raise ValueError("episode_numbers must not be empty")
    code = f"S{season:02d}E{numbers[0]:02d}"
    if len(numbers) > 1:
        code += f"-E{numbers[-1]:02d}"
    return code


def episode_filename(
    series_title: str,
    season: int,
    episode_numbers: list[int],
    episode_title: str,
    quality: str,
    ext: str,
) -> str:
    code = episode_code(season, episode_numbers)
    head = _fit_keeping(sanitize(series_title), f" - {code}")
    stem = head
    if episode_title:
        # the title is the part that gives way; the series and the code are already in
        stem = _fit(f"{head} - {sanitize(episode_title)}")
        if not stem.endswith(code) and code not in stem:
            stem = head
    return f"{stem} [{quality}].{ext.lstrip('.')}"


def movie_filename(title: str, year: int | None, quality: str, ext: str) -> str:
    stem = _fit_keeping(sanitize(title) or "untitled", f" ({year})" if year else "")
    return f"{stem} [{quality}].{ext.lstrip('.')}"
