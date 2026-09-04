import logging
import os
import shutil
import sqlite3
import stat
from pathlib import Path

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from outriggarr import __version__
from outriggarr.settings import get_setting
from outriggarr.source import cookies_state, js_runtime, pot_provider_ready

log = logging.getLogger(__name__)
router = APIRouter()

_last_staging_state: bool | None = None


def staging_writable(staging: Path) -> bool:
    """The one probe behind /health and the page footer. Every change of answer is
    logged with what stat saw (or the OS error), so a red "NOT writable" footer has a
    reason in the container log: a re-created bind source owned by root, a chmod, or
    the mount itself answering with an error for a moment."""
    global _last_staging_state
    try:
        st = staging.stat()
        ok = stat.S_ISDIR(st.st_mode) and os.access(staging, os.W_OK)
        detail = f"mode={stat.filemode(st.st_mode)} owner={st.st_uid}:{st.st_gid}"
    except OSError as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    if ok != _last_staging_state:
        (log.info if ok else log.warning)(
            "staging %s is %swritable as uid %d (%s)",
            staging,
            "" if ok else "NOT ",
            os.geteuid(),
            detail,
        )
        _last_staging_state = ok
    return ok


def cooloff_status(cooloff) -> dict[str, object] | None:
    """The shared rate-limit pause, for /health and the footer; None when not paused."""
    if cooloff is None or not cooloff.active():
        return None
    return {"remaining_seconds": int(cooloff.remaining()), "message": cooloff.message}


def _write_lock_free(engine) -> bool:
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA busy_timeout=2000")
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("ROLLBACK")
        except sqlite3.OperationalError:
            return False
        finally:
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    finally:
        raw.close()
    return True


@router.get("/health")
def health(request: Request, response: Response) -> dict[str, object]:
    """200 when downloads can work; 503 "degraded" with the reasons when they cannot."""
    with request.app.state.session_factory() as session:
        session.execute(text("SELECT 1"))
        cookies_path = get_setting(session, "cookies_path")
    # a reader never blocks under WAL, so SELECT 1 cannot see a wedged writer; ask for
    # the write lock for two seconds and give it straight back
    write_lock_ok = _write_lock_free(request.app.state.engine)
    from yt_dlp.version import __version__ as ytdlp_version

    staging = request.app.state.settings.staging_dir
    tasks = getattr(request.app.state, "background_tasks", {}) or {}
    # a task that was never started (tests, --no-worker) is neither alive nor dead
    liveness = {name: (not t.done()) for name, t in tasks.items() if t is not None}
    body: dict[str, object] = {
        "status": "ok",
        "version": __version__,
        "yt_dlp": ytdlp_version,
        "js_runtime": js_runtime(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        # off = age-gated videos top out at 480p (YouTube wants a proof-of-origin token)
        "po_token_provider": pot_provider_ready(request.app.state.settings.pot_server_home)
        and not getattr(request.app.state, "pot_probe", None),
        # none / unreadable / signed in / signed out — "signed out" means age-gated videos
        # will fail until the cookies file is exported again
        "youtube_session": cookies_state(cookies_path),
        # set while a rate-limit answer has the queue, the scans and the fetches paused
        "youtube_cooloff": cooloff_status(
            getattr(getattr(request.app.state, "runner_deps", None), "cooloff", None)
        ),
        # False means downloads will fail: fix the mount's ownership (see entrypoint.sh)
        "staging_writable": staging_writable(staging),
        "worker_alive": liveness.get("worker"),
        "scheduler_alive": liveness.get("scheduler"),
    }
    body["write_lock"] = write_lock_ok
    body["po_token_probe"] = getattr(request.app.state, "pot_probe", None)
    problems = [k for k in ("ffmpeg", "staging_writable", "write_lock") if not body[k]]
    problems += [k for k, alive in liveness.items() if alive is False]
    if getattr(request.app.state, "worker_note", None):
        problems.append("instance_lock")  # another instance holds the database: no loops here
    if problems:
        body["status"] = "degraded"
        body["problems"] = problems
        response.status_code = 503
    return body
