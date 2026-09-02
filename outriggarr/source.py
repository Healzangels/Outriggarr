"""VideoSource protocol + the yt-dlp implementation. The only module that imports yt_dlp."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class SourceError(Exception):
    """yt-dlp failed. `str(err)` is yt-dlp's own message, verbatim."""


class DownloadAborted(Exception):
    """The download was stopped because `should_abort()` returned True."""


@dataclass(frozen=True)
class VideoRef:
    """One video from a flat listing (no per-video fetch)."""

    id: str
    title: str
    url: str
    duration: int | None
    playlist_index: int | None
    upload_date: str | None  # YYYYMMDD when the listing carries it, else None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    height: int | None
    ext: str
    title: str
    video_id: str
    subtitles: tuple[Path, ...] = ()  # .srt sidecars written next to `path`


ProgressCallback = Callable[[float], None]
AbortCheck = Callable[[], bool]


class VideoSource(Protocol):
    def resolve(self, url: str) -> list[VideoRef]:
        """Blocking. A video URL → [that video]; a playlist URL → its videos, flat."""
        ...

    def list_recent(self, url: str, limit: int) -> list[VideoRef]:
        """Blocking. The newest `limit` videos of a channel/playlist, flat (no dates)."""
        ...

    def fetch_info(self, url: str) -> VideoRef:
        """Blocking. One video with its upload date (a per-video fetch)."""
        ...

    def tag_audio_language(self, path: Path, language: str) -> None:
        """Blocking. Stamp `language` (ISO 639-2) on every audio stream of `path`, in
        place, without re-encoding. Raises SourceError with ffmpeg's own message."""
        ...

    def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        fmt: str,
        merge_container: str,
        progress: ProgressCallback,
        should_abort: AbortCheck,
        subtitle_langs: tuple[str, ...] = (),
        auto_subtitles: bool = False,
    ) -> DownloadResult:
        """Blocking. Downloads `url` into `dest_dir` (created if needed) and returns the
        final merged file plus any requested subtitles as .srt sidecars.
        Raises SourceError / DownloadAborted."""
        ...


class _YtDlpLogger:
    def debug(self, msg: str) -> None:
        # yt-dlp routes its normal progress/info lines through debug() too.
        if not msg.startswith("[debug] "):
            log.debug("%s", msg)

    def info(self, msg: str) -> None:
        log.info("%s", msg)

    def warning(self, msg: str) -> None:
        log.warning("%s", msg)

    def error(self, msg: str) -> None:
        log.error("%s", msg)


_FLAT_OPTS: dict[str, Any] = {
    "extract_flat": "in_playlist",
    "skip_download": True,
    "quiet": True,
    "no_warnings": False,
    "noprogress": True,
}


def channel_videos_url(url: str) -> str:
    """A bare YouTube channel URL lists featured shelves; its /videos tab is the flat,
    newest-first upload list the scheduler wants. Other URLs pass through unchanged."""
    m = re.match(
        r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))/?(?:[?#].*)?$",
        url.strip(),
    )
    return f"{m.group(1)}/videos" if m else url.strip()


OptsProvider = Callable[[], dict[str, Any]]


class YtDlpSource:
    """`extra_opts` is called per operation and merged LAST over our options, so the
    operator's passthrough (cookies, SponsorBlock, rate limits…) always wins."""

    def __init__(self, extra_opts: OptsProvider | None = None) -> None:
        self._extra = extra_opts or (lambda: {})

    def _opts(self, base: dict[str, Any]) -> dict[str, Any]:
        from outriggarr.settings import RESERVED_YTDLP_KEYS

        extra = {k: v for k, v in self._extra().items() if k not in RESERVED_YTDLP_KEYS}
        cookies = extra.get("cookiefile")
        if cookies and not os.access(cookies, os.R_OK):
            # yt-dlp would silently run without cookies and fail with an unrelated
            # bot-check/age-gate message; say what actually went wrong.
            raise SourceError(f"cookies file {cookies!r} is not readable by the app user")
        return {**base, **extra, "logger": _YtDlpLogger()}  # operator wins, except reserved

    def _extract(self, url: str, opts: dict[str, Any]) -> dict[str, Any]:
        import yt_dlp
        from yt_dlp.utils import DownloadError

        try:
            with yt_dlp.YoutubeDL(self._opts(opts)) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as exc:
            raise SourceError(str(exc)) from exc
        except Exception as exc:  # a bad format/option string raises inside YoutubeDL()
            raise SourceError(f"yt-dlp could not run: {exc!r}") from exc
        if info is None:
            raise SourceError(f"yt-dlp returned no info for {url}")
        return info

    def resolve(self, url: str) -> list[VideoRef]:
        # A pasted watch URL that also carries &list= is the video, not the playlist;
        # a bare channel URL lists its uploads tab rather than its shelves.
        return videos_from_info(
            self._extract(channel_videos_url(url), {**_FLAT_OPTS, "noplaylist": True})
        )

    def list_recent(self, url: str, limit: int) -> list[VideoRef]:
        target = channel_videos_url(url)
        opts = dict(_FLAT_OPTS)
        if target != url.strip() or "/videos" in target:
            # Channel uploads are newest-first: the first N are the newest N.
            opts["playlistend"] = limit
        # A playlist is in whatever order its owner chose; list it whole (flat listing is
        # cheap) so a newest-last playlist still surfaces its newest entries.
        return videos_from_info(self._extract(target, opts))[
            : limit if "playlistend" in opts else None
        ]

    def fetch_info(self, url: str) -> VideoRef:
        info = self._extract(url, {"skip_download": True, "quiet": True, "noplaylist": True})
        (video,) = videos_from_info(info)
        return video

    def tag_audio_language(self, path: Path, language: str) -> None:
        tmp = path.with_name(f"{path.stem}.lang{path.suffix}")
        try:
            proc = subprocess.run(
                ffmpeg_language_command(path, tmp, language), capture_output=True, text=True
            )
        except OSError as exc:  # ffmpeg missing
            raise SourceError(f"ffmpeg could not be run: {exc}") from exc
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise SourceError(f"ffmpeg exited {proc.returncode}: {proc.stderr.strip()}")
        tmp.replace(path)

    def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        fmt: str,
        merge_container: str,
        progress: ProgressCallback,
        should_abort: AbortCheck,
        subtitle_langs: tuple[str, ...] = (),
        auto_subtitles: bool = False,
    ) -> DownloadResult:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled, DownloadError

        dest_dir.mkdir(parents=True, exist_ok=True)

        def hook(d: dict[str, Any]) -> None:
            if should_abort():
                raise DownloadCancelled("aborted by outriggarr")
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes")
                if total and done is not None:
                    progress(min(100.0, 100.0 * done / total))

        opts: dict[str, Any] = {
            "format": fmt,
            "merge_output_format": merge_container,
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "progress_hooks": [hook],
        }
        if subtitle_langs:
            opts.update(subtitle_opts(subtitle_langs, auto_subtitles))
        info = None
        try:
            with yt_dlp.YoutubeDL(self._opts(opts)) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadCancelled as exc:
            # yt-dlp reuses DownloadCancelled for its own stop conditions (download
            # archive hits, max-downloads); only OUR hook's abort is an abort.
            if "aborted by outriggarr" in str(exc):
                raise DownloadAborted(str(exc)) from exc
            raise SourceError(f"yt-dlp stopped: {exc}") from exc
        except DownloadError as exc:
            raise SourceError(str(exc)) from exc
        except OSError as exc:
            # YoutubeDL.__exit__ saves the cookie jar; a read-only cookies file raises
            # here AFTER a successful download. Keep the download, report the problem.
            if info is None:
                raise SourceError(f"yt-dlp could not run: {exc!r}") from exc
            log.warning("yt-dlp could not save state after the download: %s", exc)
        except Exception as exc:  # bad format/option strings raise inside YoutubeDL()
            raise SourceError(f"yt-dlp could not run: {exc!r}") from exc
        if info is None:
            raise SourceError(f"yt-dlp returned no info for {url}")
        return _result_from_info(info, dest_dir)


def subtitle_opts(langs: tuple[str, ...], auto: bool) -> dict[str, Any]:
    """yt-dlp options that fetch the wanted caption tracks and convert them to .srt."""
    return {
        "writesubtitles": True,
        "writeautomaticsub": bool(auto),
        "subtitleslangs": list(langs),
        "subtitlesformat": "srt/best",
        "postprocessors": [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}],
    }


def subtitle_sidecars(dest_dir: Path, video_id: str) -> tuple[Path, ...]:
    """The .srt files yt-dlp wrote for this video: <id>.<lang>.srt."""
    return tuple(sorted(p for p in dest_dir.glob(f"{video_id}.*.srt") if p.is_file()))


def ffmpeg_language_command(src: Path, dst: Path, language: str) -> list[str]:
    """Remux `src` to `dst` copying every stream, tagging all audio streams."""
    return [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-map",
        "0",
        "-c",
        "copy",
        "-metadata:s:a",
        f"language={language}",
        str(dst),
    ]


def videos_from_info(info: dict[str, Any]) -> list[VideoRef]:
    """Normalise a flat yt-dlp info dict (playlist or single video) to VideoRefs.
    Nested playlists (a channel's tabs) are skipped; M4 handles channels."""
    if info.get("_type") == "playlist":
        out: list[VideoRef] = []
        for i, e in enumerate(info.get("entries") or [], start=1):
            if not e or e.get("_type") == "playlist" or not e.get("id"):
                continue
            out.append(_ref(e, e.get("playlist_index") or i))
        return out
    if not info.get("id"):
        raise SourceError("yt-dlp returned an entry without an id")
    return [_ref(info, None)]


_UNAVAILABLE_TITLES = re.compile(r"^\[(private|deleted|unavailable) video\]$", re.IGNORECASE)


def _ref(e: dict[str, Any], index: int | None) -> VideoRef:
    vid = str(e["id"])
    url = e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={vid}"
    title = str(e.get("title") or vid)
    if _UNAVAILABLE_TITLES.match(title):
        title = vid  # the app's convention for a dead entry: title == id
    duration = e.get("duration")
    return VideoRef(
        id=vid,
        title=title,
        url=str(url),
        duration=int(duration) if duration else None,
        playlist_index=int(index) if index is not None else None,
        upload_date=str(e["upload_date"]) if e.get("upload_date") else None,
    )


def _result_from_info(info: dict[str, Any], dest_dir: Path | None = None) -> DownloadResult:
    downloads = info.get("requested_downloads") or []
    filepath = downloads[0].get("filepath") if downloads else None
    if not filepath:
        raise SourceError("yt-dlp finished but reported no output file")
    path = Path(filepath)
    height = info.get("height")
    if height is None and downloads:
        height = downloads[0].get("height")
    video_id = str(info.get("id") or "")
    return DownloadResult(
        path=path,
        height=int(height) if height is not None else None,
        ext=path.suffix.lstrip("."),
        title=str(info.get("title") or ""),
        video_id=video_id,
        subtitles=subtitle_sidecars(dest_dir, video_id) if dest_dir and video_id else (),
    )
