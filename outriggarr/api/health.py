import os
import shutil

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from outriggarr import __version__

router = APIRouter()


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
        # False means downloads will fail: fix the mount's ownership (see entrypoint.sh)
        "staging_writable": staging.is_dir() and os.access(staging, os.W_OK),
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
