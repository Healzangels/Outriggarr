"""VideoSource protocol + the yt-dlp implementation. The only module that imports yt_dlp."""

from __future__ import annotations

import logging
import re
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

    def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        fmt: str,
        merge_container: str,
        progress: ProgressCallback,
        should_abort: AbortCheck,
    ) -> DownloadResult:
        """Blocking. Downloads `url` into `dest_dir` (created if needed) and returns the
        final merged file. Raises SourceError / DownloadAborted."""
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


class YtDlpSource:
    def _extract(self, url: str, opts: dict[str, Any]) -> dict[str, Any]:
        import yt_dlp
        from yt_dlp.utils import DownloadError

        try:
            with yt_dlp.YoutubeDL({**opts, "logger": _YtDlpLogger()}) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as exc:
            raise SourceError(str(exc)) from exc
        if info is None:
            raise SourceError(f"yt-dlp returned no info for {url}")
        return info

    def resolve(self, url: str) -> list[VideoRef]:
        return videos_from_info(self._extract(url, _FLAT_OPTS))

    def list_recent(self, url: str, limit: int) -> list[VideoRef]:
        info = self._extract(channel_videos_url(url), {**_FLAT_OPTS, "playlistend": limit})
        return videos_from_info(info)[:limit]

    def fetch_info(self, url: str) -> VideoRef:
        info = self._extract(url, {"skip_download": True, "quiet": True, "noplaylist": True})
        (video,) = videos_from_info(info)
        return video

    def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        fmt: str,
        merge_container: str,
        progress: ProgressCallback,
        should_abort: AbortCheck,
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
            "logger": _YtDlpLogger(),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadCancelled as exc:
            raise DownloadAborted(str(exc)) from exc
        except DownloadError as exc:
            raise SourceError(str(exc)) from exc
        if info is None:
            raise SourceError(f"yt-dlp returned no info for {url}")
        return _result_from_info(info)


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


def _ref(e: dict[str, Any], index: int | None) -> VideoRef:
    vid = str(e["id"])
    url = e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={vid}"
    duration = e.get("duration")
    return VideoRef(
        id=vid,
        title=str(e.get("title") or vid),
        url=str(url),
        duration=int(duration) if duration else None,
        playlist_index=int(index) if index is not None else None,
        upload_date=str(e["upload_date"]) if e.get("upload_date") else None,
    )


def _result_from_info(info: dict[str, Any]) -> DownloadResult:
    downloads = info.get("requested_downloads") or []
    filepath = downloads[0].get("filepath") if downloads else None
    if not filepath:
        raise SourceError("yt-dlp finished but reported no output file")
    path = Path(filepath)
    height = info.get("height")
    if height is None and downloads:
        height = downloads[0].get("height")
    return DownloadResult(
        path=path,
        height=int(height) if height is not None else None,
        ext=path.suffix.lstrip("."),
        title=str(info.get("title") or ""),
        video_id=str(info.get("id") or ""),
    )
