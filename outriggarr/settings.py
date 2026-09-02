"""Environment settings and DB-backed settings access.

Environment variables configure where the process lives (paths, DB, port).
Everything the GUI can edit lives in the `setting` table and is read through
`get_setting` / `set_setting`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from outriggarr.db.models import Setting


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    staging_dir: Path
    database_url: str
    log_level: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        config_dir = Path(env.get("OUTRIGGARR_CONFIG_DIR", "/config"))
        staging_dir = Path(env.get("OUTRIGGARR_STAGING_DIR", "/staging"))
        database_url = env.get("OUTRIGGARR_DATABASE_URL", f"sqlite:///{config_dir / 'app.db'}")
        level = env.get("OUTRIGGARR_LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if level not in logging.getLevelNamesMapping():
            raise ValueError(f"OUTRIGGARR_LOG_LEVEL={level!r} is not a logging level")
        return cls(
            config_dir=config_dir,
            staging_dir=staging_dir,
            database_url=database_url,
            log_level=level,
        )


# Defaults for DB-backed settings. Keys are the only ones the app knows about.
DEFAULTS: dict[str, str] = {
    "scan_interval_minutes": "30",
    "scan_video_limit": "50",  # newest N videos listed per subscription scan
    "concurrency": "1",
    # 1080p cap + H.264/AAC preference: every profile on the target stack tops out at
    # WEBDL-1080p and AV1/VP9 forces transcodes on most players. Editable in Settings.
    "default_format": (
        "bestvideo*[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]"
        "/bestvideo*[height<=1080]+bestaudio/best[height<=1080]"
    ),
    "merge_container": "mkv",
    "ytdlp_extra_opts": "{}",
    "cookies_path": "",
    "sonarr_tag": "",  # label to put on subscribed series in Sonarr; blank = off
    # ISO 639-2 code stamped on the audio stream(s) of every download; YouTube tracks
    # carry none, which players show as "Unknown". Blank = leave untagged.
    "audio_language": "eng",
    # Subtitle languages to fetch as .srt sidecars when the upload has them (comma-separated
    # yt-dlp/YouTube codes such as en, en-US, es); blank = none. Sonarr/Radarr import them
    # alongside the video when "Import Extra Files" includes srt.
    "subtitles_langs": "en",
    "subtitles_auto": "0",  # "1" also accepts auto-generated captions (machine transcripts)
    # Apprise URLs (one per line) and which of Outriggarr's own events to announce.
    "apprise_urls": "",
    "notify_on_failed": "1",  # a job failed for good (no more retries) or was rejected
    "notify_on_scan_error": "1",  # a subscription scan hit an error (announced once per new error)
    "notify_on_done": "0",  # a job imported (the *arr usually announces this itself)
}


def apprise_urls(session: Session) -> list[str]:
    return [u.strip() for u in get_setting(session, "apprise_urls").splitlines() if u.strip()]


MERGE_CONTAINERS = ("mkv", "mp4")  # webm cannot hold the default H.264/AAC streams

# yt-dlp options the app owns; the operator passthrough may not set them (it would
# redirect output outside staging, drop our progress/abort hooks, or run arbitrary
# post-processors such as `Exec`).
RESERVED_YTDLP_KEYS = frozenset(
    {
        "outtmpl",
        "paths",
        "postprocessors",
        "progress_hooks",
        "postprocessor_hooks",
        "logger",
        "format",
        "merge_output_format",
        "writesubtitles",
        "writeautomaticsub",
        "subtitleslangs",
        "subtitlesformat",
        "exec_cmd",
        "external_downloader",
        "external_downloader_args",
        "ffmpeg_location",
        "noplaylist",
        "extract_flat",
        "skip_download",
        "playlistend",
    }
)


def validate_setting(key: str, value: str) -> str:
    """Normalise and validate one setting value; raises ValueError with a plain message."""
    if key not in DEFAULTS:
        raise KeyError(key)
    value = value.strip()
    if key in ("scan_interval_minutes", "concurrency", "scan_video_limit"):
        try:
            n = int(value)
        except ValueError:
            raise ValueError(f"{key} must be an integer") from None
        lo, hi = {
            "scan_interval_minutes": (1, 1440),
            "concurrency": (1, 8),
            "scan_video_limit": (1, 500),
        }[key]
        if not lo <= n <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        return str(n)
    if key == "default_format":
        if not value:
            raise ValueError("default_format must not be empty")
        _probe_ytdlp({"format": value})
        return value
    if key == "merge_container":
        if value not in MERGE_CONTAINERS:
            raise ValueError(f"merge_container must be one of {', '.join(MERGE_CONTAINERS)}")
        return value
    if key == "ytdlp_extra_opts":
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"ytdlp_extra_opts is not valid JSON: {exc.msg}") from None
        if not isinstance(parsed, dict):
            raise ValueError("ytdlp_extra_opts must be a JSON object")
        reserved = sorted(k for k in parsed if k in RESERVED_YTDLP_KEYS)
        if reserved:
            raise ValueError(
                f"ytdlp_extra_opts may not set {reserved}: Outriggarr owns those options"
            )
        _probe_ytdlp(parsed)
        return json.dumps(parsed)
    if key == "cookies_path":
        if value:
            p = Path(value)
            if not p.is_file():
                raise ValueError(f"cookies_path {value!r} is not a file inside the container")
            if not os.access(p, os.R_OK):
                raise ValueError(f"cookies_path {value!r} is not readable by the app user")
        return value
    if key == "apprise_urls":
        from outriggarr.notify import validate_apprise_urls

        return "\n".join(validate_apprise_urls(value))
    if key in ("notify_on_failed", "notify_on_scan_error", "notify_on_done"):
        if value not in ("0", "1"):
            raise ValueError(f"{key} must be 0 or 1")
        return value
    if key == "subtitles_langs":
        langs = [x.strip() for x in value.split(",") if x.strip()]
        bad = [x for x in langs if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", x)]
        if bad:
            raise ValueError(f"subtitles_langs: not language codes: {bad}")
        return ",".join(dict.fromkeys(langs))
    if key == "subtitles_auto":
        if value not in ("0", "1"):
            raise ValueError("subtitles_auto must be 0 or 1")
        return value
    if key == "audio_language":
        if value and not re.fullmatch(r"[a-z]{3}", value):
            raise ValueError("audio_language must be a 3-letter ISO 639-2 code (e.g. eng) or blank")
        return value
    if key == "sonarr_tag":
        if value and (len(value) > 50 or " " in value or value != value.lower()):
            raise ValueError("sonarr_tag must be a short lowercase label without spaces")
        return value
    return value  # cookies_path: free text


def _probe_ytdlp(opts: dict[str, Any]) -> None:
    """yt-dlp validates format strings and many options only when constructing the
    downloader; do that here so a typo is a 422, not an 'internal error' on every job."""
    import yt_dlp

    try:
        yt_dlp.YoutubeDL({**opts, "quiet": True, "no_warnings": True})
    except Exception as exc:
        raise ValueError(f"yt-dlp rejected it: {exc}") from None


def ytdlp_options(session: Session) -> dict[str, Any]:
    """The operator's yt-dlp passthrough: extra opts JSON plus the cookies file."""
    opts: dict[str, Any] = dict(json.loads(get_setting(session, "ytdlp_extra_opts") or "{}"))
    cookies = get_setting(session, "cookies_path")
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def get_setting(session: Session, key: str) -> str:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = session.get(Setting, key)
    return DEFAULTS[key] if row is None else row.value


def set_setting(session: Session, key: str, value: str) -> None:
    value = validate_setting(key, value)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def all_settings(session: Session) -> dict[str, str]:
    return {key: get_setting(session, key) for key in DEFAULTS}
