"""VideoSource protocol + the yt-dlp implementation. The only module that imports yt_dlp."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

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
    # "3 years ago" / "2 days ago": what the listing page says, when it says anything.
    # A guess to the unit, never a date: shown with a ~, never matched on.
    approx_age: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    height: int | None
    ext: str
    title: str
    video_id: str
    subtitles: tuple[Path, ...] = ()  # .srt sidecars written next to `path`
    audio_language: str | None = None  # ISO 639-2, as the source declared it; None = unknown


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


# yt-dlp "info" lines that mean the download is poorer than asked for
DEGRADED_NOTICE = re.compile(
    r"age-restricted|po token|proof of origin|missing subtitles|some formats may be missing",
    re.IGNORECASE,
)


class _YtDlpLogger:
    def __init__(self, expected_login: bool = False) -> None:
        # On a first, cookie-less attempt a "sign in" error is expected and answered by
        # a signed-in retry: log it as debug, not as an alarming ERROR line.
        self._expected_login = expected_login

    def debug(self, msg: str) -> None:
        # yt-dlp routes its normal progress/info lines through debug() too. A few of
        # them mean a quieter download than asked for; those deserve a WARNING line.
        if msg.startswith("[debug] "):
            return
        if DEGRADED_NOTICE.search(msg):
            log.warning("%s", msg)
        else:
            log.debug("%s", msg)

    def info(self, msg: str) -> None:
        log.info("%s", msg)

    def warning(self, msg: str) -> None:
        # A known, harmless notice on signed-in sessions: some web_embedded formats lack
        # a URL; other clients still provide them. One line per video would drown the log.
        if "SABR-only streaming experiment" in msg:
            log.debug("%s", msg)
            return
        log.warning("%s", msg)

    def error(self, msg: str) -> None:
        if self._expected_login and NEEDS_LOGIN.search(msg):
            log.debug("%s", msg)
            return
        log.error("%s", msg)


REMUX_TIMEOUT_SECONDS = 3600.0

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
        r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))"
        r"(?:/featured)?/?(?:[?#].*)?$",
        url.strip(),
    )
    return f"{m.group(1)}/videos" if m else url.strip()


OptsProvider = Callable[[], dict[str, Any]]
HttpGet = Callable[..., Any]  # httpx.get's shape; injectable for tests

# archive.org: yt-dlp downloads any item, but it cannot list a *collection* (a set of
# items such as archive.org/details/scam_school). The site's search API can, with each
# item's title and date — and the date there is the original air date, which makes date
# matching strong. Every item is then a normal yt-dlp download.
ARCHIVE_DETAILS = re.compile(
    r"^https?://(?:www\.)?archive\.org/details/([A-Za-z0-9._@-]+)/?(?:[?#].*)?$"
)
ARCHIVE_ROWS = 1000  # search API page size
ARCHIVE_MAX_ITEMS = 5000  # a collection is listed whole, like a playlist, up to this
ARCHIVE_MEDIATYPES = frozenset({"movies"})


def archive_identifier(url: str) -> str | None:
    m = ARCHIVE_DETAILS.match(url.strip())
    return m.group(1) if m else None


def strip_collection_prefix(title: str, collection_title: str | None) -> str:
    """Collections number their items after the collection's own name: "Scam School
    194: The Amazing iCard". Without that prefix the item title equals the episode
    title and matches exactly, which vouches for the pairing."""
    if not collection_title or not collection_title.strip():
        return title
    pattern = rf"^\s*{re.escape(collection_title.strip())}\s*(?:[-–:#]?\s*\d+)?\s*[:\-–|]\s*"
    stripped = re.sub(pattern, "", title, count=1, flags=re.IGNORECASE).strip()
    return stripped or title


YOUTUBE_SIGNIN_COOKIE = "LOGIN_INFO"  # present on .youtube.com only for a signed-in account
# yt-dlp's wording when a video needs an account: age gate, bot check, private or
# members-only videos. Anything else is not worth a second, signed-in attempt.
NEEDS_LOGIN = re.compile(
    r"sign in to confirm|sign in to|log ?in required|login required|private video|"
    r"this video is private|members[- ]only|join this channel",
    re.IGNORECASE,
)

# YouTube's wording when it has had enough of the session for a while, relayed verbatim
# by yt-dlp: "This content isn't available, try again later. The current session has
# been rate-limited by YouTube for up to an hour." HTTP 429 is the same answer from any host.
RATE_LIMITED = re.compile(
    r"rate[- ]limited|rate limit|try again later|http error 429|too many requests",
    re.IGNORECASE,
)
_HOSTS_IN_TEXT = re.compile(r"https?://([^/\s'\"]+)|\b(archive\.org)\b", re.IGNORECASE)


def is_rate_limited(message: str) -> bool:
    """YouTube's wall, the one that pauses everything. A 429 that names another host
    (archive.org's search, a caption CDN) is that request's own problem: the job's
    ordinary retry ladder, not an hour's pause for every scan and download."""
    if RATE_LIMITED.search(message) is None:
        return False
    hosts = {(a or b).lower() for a, b in _HOSTS_IN_TEXT.findall(message)}
    return not hosts or any("youtu" in h or "googlevideo" in h for h in hosts)


# Answers that time will not change: the video is gone, walled off, or the request
# itself is wrong. Retrying them for a day only delays the news; the job fails at once
# and Retry by hand is there once something has changed (a cookies file, the format
# string, the owner's mind). A bot check is deliberately NOT here: it is the address
# being busy, and it passes again by itself.
PERMANENT_FAILURE = re.compile(
    r"video unavailable|video is unavailable|has been removed|no longer available|"
    r"account associated with this video has been terminated|this video is private|"
    r"private video|members[- ]only|join this channel|sign in to confirm your age|"
    r"age[- ]restricted|not available in your country|not made this video available|"
    r"blocked it in your country|not available from your location|drm protected|"
    r"requires payment|unsupported url|is a playlist or channel, not a single video|"
    # a 404/410 on the video PAGE is final; one on a data URL is an expired manifest
    r"unable to download webpage: http error 40[4]|unable to download webpage: http error 410|"
    r"is not a valid url",
    re.IGNORECASE,
)


def is_permanent_failure(message: str) -> bool:
    """Whether a download error is one no retry will fix (see PERMANENT_FAILURE). The bot
    check and the rate-limit answer are outside that regex by construction; the tests
    pin both as retryable, so the regex cannot quietly grow to swallow them."""
    return PERMANENT_FAILURE.search(message) is not None


@dataclass
class CoolOff:
    """A process-wide pause of source work after a rate-limit answer: the queue, the
    scheduler and the background fetches all wait it out, instead of each burning an
    attempt against the same wall. 15 min first, doubling per consecutive hit, capped
    at an hour (YouTube's own figure); any successful download resets it."""

    base_seconds: float = 900.0
    cap_seconds: float = 3600.0
    clock: Callable[[], float] = time.monotonic
    until: float = 0.0
    strikes: int = 0
    message: str | None = None
    hit_at: float = 0.0  # when the current pause began; a download older than that proves nothing

    def hit(self, message: str) -> float:
        """Record a rate-limit answer; returns how long the pause is, in seconds. Answers
        that land while a pause is already in force (four fetches in flight all hit the
        same wall) are one strike, not four: the pause escalates only when the last one
        turned out too short."""
        if self.active():
            self.message = self.message or message
            return self.remaining()
        if self.until and self.clock() - self.until > self.cap_seconds:
            self.strikes = 0  # the last pause was long over: start the ladder again
        wait = min(self.base_seconds * 2**self.strikes, self.cap_seconds)
        self.strikes += 1
        self.hit_at = self.clock()
        self.until = self.hit_at + wait
        self.message = message
        return wait

    def clear(self, since: float | None = None) -> None:
        """A download succeeded: the source serves us again. One that STARTED before
        the wall went up (`since` < `hit_at`) was already streaming through it and says
        nothing about the wall, so it leaves the pause and the ladder alone."""
        if since is not None and self.hit_at and since < self.hit_at:
            return
        self.until = 0.0
        self.strikes = 0
        self.message = None

    def remaining(self) -> float:
        return max(0.0, self.until - self.clock())

    def active(self) -> bool:
        return self.remaining() > 0


def has_signin_cookie(netscape: str) -> bool:
    """True when a Netscape cookie jar carries YouTube's sign-in cookie."""
    for line in netscape.splitlines():
        line = line.removeprefix("#HttpOnly_")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and "youtube.com" in parts[0] and parts[5] == YOUTUBE_SIGNIN_COOKIE:
            return True
    return False


def cookies_state(path: str | Path | None) -> str:
    """'none' (no cookies file configured), 'unreadable', 'signed in', or 'signed out': a
    jar without the sign-in cookie, because the export was never signed in or because
    YouTube ended the session (it does when a browser keeps using the same account)."""
    if not path:
        return "none"
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return "unreadable"
    return "signed in" if has_signin_cookie(text) else "signed out"


def pot_provider_ready(server_home: Path | None) -> bool:
    """True when the bgutil script and a Node runtime are both present: yt-dlp can then
    fetch PO tokens, which YouTube requires for the best formats of some videos (every
    age-gated one, for a signed-in session)."""
    if server_home is None:
        return False
    runtime = shutil.which("node") or shutil.which("deno")  # the plugin accepts either
    return (server_home / "build" / "generate_once.js").is_file() and runtime is not None


class YtDlpSource:
    """`extra_opts` is called per operation and merged LAST over our options, so the
    operator's passthrough (cookies, SponsorBlock, rate limits…) always wins."""

    def __init__(
        self,
        extra_opts: OptsProvider | None = None,
        pot_server_home: Path | None = None,
        http_get: HttpGet | None = None,
    ) -> None:
        self._extra = extra_opts or (lambda: {})
        self._pot_home = pot_server_home
        self._http_get = http_get or httpx.get

    # ---- archive.org collections (the only source yt-dlp cannot list itself)
    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            r = self._http_get(url, params=params, timeout=30, headers={"User-Agent": "Outriggarr"})
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise SourceError(f"archive.org: {exc}") from exc
        except ValueError as exc:
            raise SourceError(f"archive.org: {url} did not return JSON") from exc
        return data if isinstance(data, dict) else {}

    def _archive_collection(self, url: str) -> list[VideoRef] | None:
        """The items of an archive.org collection, newest first; None when the URL is
        not an archive.org collection (a single item goes through yt-dlp as usual)."""
        ident = archive_identifier(url)
        if ident is None:
            return None
        meta = self._get_json(f"https://archive.org/metadata/{ident}").get("metadata") or {}
        if meta.get("mediatype") != "collection":
            return None
        collection_title = str(meta.get("title") or "")
        refs: list[VideoRef] = []
        page = 1
        while len(refs) < ARCHIVE_MAX_ITEMS:
            data = self._get_json(
                "https://archive.org/advancedsearch.php",
                {
                    "q": f"collection:{ident}",
                    "fl[]": ["identifier", "title", "date", "mediatype"],
                    "rows": ARCHIVE_ROWS,
                    "page": page,
                    "sort[]": "date desc",
                    "output": "json",
                },
            )
            docs = (data.get("response") or {}).get("docs") or []
            for d in docs:
                if not isinstance(d, dict):
                    continue
                d = {k: (v[0] if isinstance(v, list) and v else v) for k, v in d.items()}
                if d.get("mediatype") not in ARCHIVE_MEDIATYPES or not d.get("identifier"):
                    continue
                date = str(d.get("date") or "")[:10].replace("-", "")
                refs.append(
                    VideoRef(
                        id=str(d["identifier"]),
                        title=strip_collection_prefix(
                            str(d.get("title") or d["identifier"]), collection_title
                        ),
                        url=f"https://archive.org/details/{d['identifier']}",
                        duration=None,
                        playlist_index=len(refs) + 1,
                        upload_date=date if len(date) == 8 and date.isdigit() else None,
                    )
                )
            if len(docs) < ARCHIVE_ROWS:
                break
            page += 1
        return refs[:ARCHIVE_MAX_ITEMS]

    def _cookies_configured(self) -> bool:
        return bool(self._extra().get("cookiefile"))

    def _with_login_fallback(self, url: str, attempt: Callable[[bool], Any]) -> Any:
        """Run without the cookies first, with them only when YouTube asks for a sign-in.
        A signed-in session is needed for age-gated videos alone; used everywhere it
        can get the account put on YouTube's "SABR-only" experiment, after which the
        web clients offer nothing above 360p — seen live: 1080p without cookies, 360p
        with. Using the session only on demand also keeps it from rotating away."""
        try:
            return attempt(False)
        except SourceError as exc:
            if not (self._cookies_configured() and NEEDS_LOGIN.search(str(exc))):
                raise
            log.info("%s: YouTube wants a sign-in; retrying with the cookies file", url)
            return attempt(True)

    def _opts(self, base: dict[str, Any], *, cookies: bool = True) -> dict[str, Any]:
        from outriggarr.settings import RESERVED_YTDLP_KEYS

        extra = {k: v for k, v in self._extra().items() if k not in RESERVED_YTDLP_KEYS}
        configured = extra.get("cookiefile")
        if not cookies:
            extra.pop("cookiefile", None)
        if configured and not os.access(configured, os.R_OK):
            # yt-dlp would silently run without cookies and fail with an unrelated
            # bot-check/age-gate message; say what actually went wrong.
            raise SourceError(f"cookies file {configured!r} is not readable by the app user")
        # YouTube hands out its best formats only to clients that present a proof-of-origin
        # token; the bgutil plugin mints one through its Node script when told where it is.
        # The operator's own extractor_args are merged per extractor on top of ours.
        args: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in (base.get("extractor_args") or {}).items()
        }
        # flat channel/playlist entries then carry a timestamp read off "3 years ago"
        args["youtubetab"] = {"approximate_date": ["1"], **args.get("youtubetab", {})}
        if pot_provider_ready(self._pot_home):
            args["youtubepot-bgutilscript"] = {
                "server_home": [str(self._pot_home)],
                **args.get("youtubepot-bgutilscript", {}),
            }
        theirs = extra.pop("extractor_args", None)
        if isinstance(theirs, dict):
            for ie, kv in theirs.items():
                args[ie] = {**args.get(ie, {}), **kv} if isinstance(kv, dict) else kv
        logger = _YtDlpLogger(expected_login=bool(configured) and not cookies)
        merged = {**base, **extra, "logger": logger}  # operator wins, except reserved
        if args:
            merged["extractor_args"] = args
        return merged

    @contextlib.contextmanager
    def _private_cookie_jar(self, opts: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """yt-dlp rewrites its cookie jar when it closes. It gets a private copy, so it
        can never write an old session back over the operator's file: a download that
        spanned a re-export did exactly that, live, and the new cookies were lost.
        Rotated cookies are copied back only while the operator's file is untouched."""
        original = opts.get("cookiefile")
        if not original:
            yield opts
            return
        before = os.stat(original)
        was_signed_in = has_signin_cookie(Path(original).read_text(errors="replace"))
        fd, private = tempfile.mkstemp(prefix="outriggarr-cookies-", suffix=".txt")
        os.close(fd)
        try:
            shutil.copyfile(original, private)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(private)
            raise
        try:
            yield {**opts, "cookiefile": private}
        finally:
            try:
                after = os.stat(original)
                unchanged = (after.st_mtime_ns, after.st_size) == (
                    before.st_mtime_ns,
                    before.st_size,
                )
                signed_out = was_signed_in and not has_signin_cookie(
                    Path(private).read_text(errors="replace")
                )
                if not unchanged:
                    # normal with parallel runs: a sibling wrote its rotation first
                    log.debug("cookies file %s changed while yt-dlp ran; keeping it", original)
                elif signed_out:
                    # YouTube cleared the sign-in during this run (it invalidates a session
                    # a browser is also using). Keep the operator's export as it was, so
                    # what happened stays visible, and say what to do.
                    log.warning(
                        "YouTube signed the cookie session out during this run; %s is kept as "
                        "exported. Export again from a private window signed in to YouTube, "
                        "then close that window so the browser cannot rotate the session.",
                        original,
                    )
                elif os.access(original, os.W_OK) and os.path.getsize(private) > 0:
                    staged = f"{original}.{os.getpid()}.tmp"
                    shutil.copyfile(private, staged)
                    os.chmod(staged, before.st_mode & 0o777)
                    os.replace(staged, original)  # atomic: a concurrent run sees old or new
            except OSError as exc:
                log.warning("could not write rotated cookies back to %s: %s", original, exc)
                with contextlib.suppress(OSError, NameError):
                    os.unlink(staged)  # a half-written copy beside the export helps nobody
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(private)

    def _extract(self, url: str, opts: dict[str, Any]) -> dict[str, Any]:
        import yt_dlp
        from yt_dlp.utils import DownloadError

        def attempt(with_cookies: bool) -> dict[str, Any]:
            try:
                with (
                    self._private_cookie_jar(self._opts(opts, cookies=with_cookies)) as ydl_opts,
                    yt_dlp.YoutubeDL(ydl_opts) as ydl,
                ):
                    info = ydl.extract_info(url, download=False)
            except SourceError:
                raise  # ours already (an unreadable cookies file): not "could not run"
            except DownloadError as exc:
                raise SourceError(str(exc)) from exc
            except Exception as exc:  # a bad format/option string raises inside YoutubeDL()
                raise SourceError(f"yt-dlp could not run: {exc!r}") from exc
            if info is None:
                raise SourceError(f"yt-dlp returned no info for {url}")
            return info

        return self._with_login_fallback(url, attempt)

    def resolve(self, url: str) -> list[VideoRef]:
        collection = self._archive_collection(url)
        if collection is not None:
            return collection
        # A pasted watch URL that also carries &list= is the video, not the playlist;
        # a bare channel URL lists its uploads tab rather than its shelves.
        return videos_from_info(
            self._extract(channel_videos_url(url), {**_FLAT_OPTS, "noplaylist": True})
        )

    def list_recent(self, url: str, limit: int) -> list[VideoRef]:
        collection = self._archive_collection(url)
        if collection is not None:
            return collection  # finite and owner-curated, like a playlist: listed whole
        target = channel_videos_url(url)
        opts = dict(_FLAT_OPTS)
        if target != url.strip() or "/videos" in target:
            # Channel uploads are newest-first: the first N are the newest N. A few extra
            # cover a premiere or a live stream pinned at the top, which are left out.
            opts["playlistend"] = limit + 5
        # A playlist is in whatever order its owner chose; list it whole (flat listing is
        # cheap) so a newest-last playlist still surfaces its newest entries.
        return videos_from_info(self._extract(target, opts))[
            : limit if "playlistend" in opts else None
        ]

    def fetch_info(self, url: str) -> VideoRef:
        # flat: a playlist/channel URL pasted here must not extract every entry
        info = self._extract(
            url,
            {
                "skip_download": True,
                "quiet": True,
                "noplaylist": True,
                "extract_flat": "in_playlist",
            },
        )
        videos = videos_from_info(info)
        if len(videos) != 1:
            raise SourceError(f"{url} is not a single video ({len(videos)} entries listed)")
        return videos[0]

    def tag_audio_language(self, path: Path, language: str) -> None:
        tmp = path.with_name(f"{path.stem}.lang{path.suffix}")
        try:
            proc = subprocess.run(
                ffmpeg_language_command(path, tmp, language),
                capture_output=True,
                text=True,
                timeout=REMUX_TIMEOUT_SECONDS,
            )
        except OSError as exc:  # ffmpeg missing
            raise SourceError(f"ffmpeg could not be run: {exc}") from exc
        except subprocess.TimeoutExpired as exc:  # a stream copy never takes this long
            tmp.unlink(missing_ok=True)
            raise SourceError(
                f"ffmpeg gave up after {int(REMUX_TIMEOUT_SECONDS // 60)} min of remuxing"
            ) from exc
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

        finished_formats = 0

        def hook(d: dict[str, Any]) -> None:
            nonlocal finished_formats
            if should_abort():
                raise DownloadCancelled("aborted by outriggarr")
            status = d.get("status")
            if status == "finished":
                finished_formats += 1
                return
            if status != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes")
            if not total or done is None:
                return
            # bestvideo+bestaudio is two downloads, each reported 0→100 by yt-dlp: fold
            # them into one monotonic figure so the UI never drops back and the stall
            # guard sees the audio stream as progress
            formats = len((d.get("info_dict") or {}).get("requested_formats") or ()) or 1
            share = min(1.0, done / total)
            pct = min(
                100.0, 100.0 * (finished_formats + share) / max(formats, finished_formats + 1)
            )
            try:
                progress(pct)
            except Exception:  # a hiccup writing progress must not discard the download
                log.warning("progress callback failed", exc_info=True)

        opts: dict[str, Any] = {
            "format": fmt,
            "merge_output_format": merge_container,
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "playlistend": 1,  # a playlist URL given as a video: one entry at most, then refused
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "progress_hooks": [hook],
        }
        if subtitle_langs:
            opts.update(subtitle_opts(subtitle_langs, auto_subtitles))

        def attempt(with_cookies: bool) -> DownloadResult:
            info = None
            try:
                with (
                    self._private_cookie_jar(self._opts(opts, cookies=with_cookies)) as ydl_opts,
                    yt_dlp.YoutubeDL(ydl_opts) as ydl,
                ):
                    info = ydl.extract_info(url, download=True)
            except DownloadCancelled as exc:
                # yt-dlp reuses DownloadCancelled for its own stop conditions (download
                # archive hits, max-downloads); only OUR hook's abort is an abort.
                if "aborted by outriggarr" in str(exc):
                    raise DownloadAborted(str(exc)) from exc
                raise SourceError(f"yt-dlp stopped: {exc}") from exc
            except SourceError:
                raise  # ours already (an unreadable cookies file): not "could not run"
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

        return self._with_login_fallback(url, attempt)


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
    """Remux `src` to `dst` copying the video, every audio stream and any subtitles,
    tagging all audio streams. Not `-map 0`: 2008-era archive.org mp4s carry RTP hint
    tracks (data) and an mjpeg cover track, which an mp4 output refuses to write, and
    the file then went into the library untagged."""
    return [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a",
        "-map",
        "0:s?",
        "-dn",
        "-c",
        "copy",
        "-metadata:s:a",
        f"language={language}",
        str(dst),
    ]


def skip_reason(entry: dict[str, Any]) -> str | None:
    """Why a listed entry is not worth offering: it cannot be downloaded yet (a scheduled
    premiere: the title is there, the video is not, and a job would fail and retry for a
    day), or is not an episode (a stream in progress; a Short). Members-only entries stay
    listed: the cookies file may well be a member's, and cookies are used on demand."""
    status = entry.get("live_status")
    if status == "is_upcoming":
        return "upcoming premiere"
    if status == "is_live":
        return "live stream in progress"
    if "/shorts/" in str(entry.get("url") or ""):
        return "Short"
    return None


def videos_from_info(info: dict[str, Any]) -> list[VideoRef]:
    """Normalise a flat yt-dlp info dict (playlist or single video) to VideoRefs.
    Nested playlists (a channel's tabs) are skipped; M4 handles channels. Entries that
    are not (yet) a downloadable episode are left out (`skip_reason`) and counted in
    the log; a single pasted video is never second-guessed."""
    if info.get("_type") == "playlist":
        out: list[VideoRef] = []
        skipped: dict[str, int] = {}
        for i, e in enumerate(info.get("entries") or [], start=1):
            if not e or e.get("_type") == "playlist" or not e.get("id"):
                continue
            why = skip_reason(e)
            if why:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            out.append(_ref(e, e.get("playlist_index") or i))
        if skipped:
            log.info(
                "listing %s: left out %s",
                info.get("id"),
                ", ".join(f"{n} x {why}" for why, n in skipped.items()),
            )
        return out
    if not info.get("id"):
        raise SourceError("yt-dlp returned an entry without an id")
    return [_ref(info, None)]


_UNAVAILABLE_TITLES = re.compile(r"^\[(private|deleted|unavailable) video\]$", re.IGNORECASE)


def relative_age(timestamp: int | float | None, now: float | None = None) -> str | None:
    """ "3 years ago" from an approximate timestamp yt-dlp derived from that very text
    (now minus N units), so the largest whole unit gives the wording back."""
    if not timestamp:
        return None
    secs = (now if now is not None else time.time()) - float(timestamp)
    if secs < 0:
        return None
    # yt-dlp turns "N units ago" into now − N units rounded to the NEAREST day (hour for
    # hours), so the way back is to round the same way — half-up, as yt-dlp does, not
    # Python's half-to-even; flooring read every age after noon UTC one unit short
    days = int(secs / 86400 + 0.5)
    if days >= 365:
        n, unit = days // 365, "year"
    elif days >= 28:
        n, unit = max(1, int(days / 30.44 + 0.5)), "month"
    elif days >= 7:
        n, unit = days // 7, "week"
    elif days >= 1:
        n, unit = days, "day"
    else:
        hours = int(secs / 3600 + 0.5)
        if hours < 1:
            return "today"
        n, unit = hours, "hour"
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def _ref(e: dict[str, Any], index: int | None) -> VideoRef:
    vid = str(e["id"])
    url = e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={vid}"
    title = str(e.get("title") or vid)
    if _UNAVAILABLE_TITLES.match(title):
        title = vid  # the app's convention for a dead entry: title == id
    duration = e.get("duration")
    upload_date = e.get("upload_date")
    return VideoRef(
        id=vid,
        title=title,
        url=str(url),
        duration=int(duration) if duration else None,
        playlist_index=int(index) if index is not None else None,
        # a flat entry's timestamp is yt-dlp's reading of "N units ago" (approximate_date)
        approx_age=None if upload_date else relative_age(e.get("timestamp")),
        upload_date=str(e["upload_date"]) if e.get("upload_date") else None,
    )


# BCP-47 primary subtags → ISO 639-2 (the 3-letter codes ffmpeg writes and Plex reads).
ISO_639_2: dict[str, str] = {
    "en": "eng", "ja": "jpn", "ko": "kor", "zh": "chi", "es": "spa", "fr": "fre", "de": "ger",
    "it": "ita", "pt": "por", "ru": "rus", "ar": "ara", "hi": "hin", "nl": "dut", "sv": "swe",
    "no": "nor", "nb": "nob", "da": "dan", "fi": "fin", "pl": "pol", "tr": "tur", "th": "tha",
    "vi": "vie", "id": "ind", "ms": "may", "tl": "tgl", "cs": "cze", "el": "gre", "he": "heb",
    "iw": "heb", "hu": "hun", "ro": "rum", "uk": "ukr", "fa": "per", "bn": "ben", "ta": "tam",
    "te": "tel", "ur": "urd", "ca": "cat", "sk": "slo", "bg": "bul", "hr": "hrv", "sr": "srp",
    "sl": "slv", "lt": "lit", "lv": "lav", "et": "est", "is": "ice", "ga": "gle", "cy": "wel",
    "eu": "baq", "gl": "glg", "af": "afr", "sw": "swa", "ka": "geo", "hy": "arm", "mk": "mac",
    "sq": "alb", "my": "bur", "km": "khm", "lo": "lao", "ne": "nep", "si": "sin", "mn": "mon",
    "bo": "tib", "la": "lat", "yi": "yid",
}  # fmt: skip
_NO_LANGUAGE = frozenset({"und", "zxx", "mul", "mis"})  # "undetermined" is not a language


def iso639_2(tag: str | None) -> str | None:
    """'ja' / 'ja-JP' / 'jpn' → 'jpn'; anything unknown or undetermined → None."""
    if not tag:
        return None
    primary = str(tag).strip().lower().replace("_", "-").split("-")[0]
    if len(primary) == 3 and primary.isalpha():
        return None if primary in _NO_LANGUAGE else primary
    return ISO_639_2.get(primary)


def detected_audio_language(info: dict[str, Any]) -> str | None:
    """The language the source declares for the audio track yt-dlp chose (YouTube sets it
    per audio track, and prefers the original track on dubbed videos), as ISO 639-2."""
    for f in info.get("requested_formats") or []:  # merged video+audio: the audio one
        if f.get("acodec") not in (None, "none") and f.get("language"):
            return iso639_2(str(f["language"]))
    for d in info.get("requested_downloads") or []:  # a single progressive format
        if d.get("language"):
            return iso639_2(str(d["language"]))
    return iso639_2(info.get("language"))


def _result_from_info(info: dict[str, Any], dest_dir: Path | None = None) -> DownloadResult:
    if info.get("_type") == "playlist":  # a job wants one file; retrying would not change that
        raise SourceError(
            f"{info.get('webpage_url') or info.get('id')} is a playlist or channel, not a "
            "single video"
        )
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
        audio_language=detected_audio_language(info),
    )
