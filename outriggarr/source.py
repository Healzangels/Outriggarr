"""VideoSource protocol + the yt-dlp implementation. The only module that imports yt_dlp."""

from __future__ import annotations

import logging
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
class DownloadResult:
    path: Path
    height: int | None
    ext: str
    title: str
    video_id: str


ProgressCallback = Callable[[float], None]
AbortCheck = Callable[[], bool]


class VideoSource(Protocol):
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


class YtDlpSource:
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
