import logging
import os
import shutil
import stat
from pathlib import Path

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from outriggarr import __version__
from outriggarr.source import pot_provider_ready

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


@router.get("/health")
def health(request: Request, response: Response) -> dict[str, object]:
    """200 when downloads can work; 503 "degraded" with the reasons when they cannot."""
    with request.app.state.session_factory() as session:
        session.execute(text("SELECT 1"))
    from yt_dlp.version import __version__ as ytdlp_version

    staging = request.app.state.settings.staging_dir
    tasks = getattr(request.app.state, "background_tasks", {}) or {}
    # a task that was never started (tests, --no-worker) is neither alive nor dead
    liveness = {name: (not t.done()) for name, t in tasks.items() if t is not None}
    body: dict[str, object] = {
        "status": "ok",
        "version": __version__,
        "yt_dlp": ytdlp_version,
        "js_runtime": next((r for r in ("deno", "node", "bun") if shutil.which(r)), None),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        # off = age-gated videos top out at 480p (YouTube wants a proof-of-origin token)
        "po_token_provider": pot_provider_ready(request.app.state.settings.pot_server_home),
        # False means downloads will fail: fix the mount's ownership (see entrypoint.sh)
        "staging_writable": staging_writable(staging),
        "worker_alive": liveness.get("worker"),
        "scheduler_alive": liveness.get("scheduler"),
    }
    problems = [k for k in ("ffmpeg", "staging_writable") if not body[k]]
    problems += [k for k, alive in liveness.items() if alive is False]
    if problems:
        body["status"] = "degraded"
        body["problems"] = problems
        response.status_code = 503
    return body
