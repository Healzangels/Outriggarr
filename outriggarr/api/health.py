import shutil

from fastapi import APIRouter, Request
from sqlalchemy import text

from outriggarr import __version__

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    with request.app.state.session_factory() as session:
        session.execute(text("SELECT 1"))
    from yt_dlp.version import __version__ as ytdlp_version

    return {
        "status": "ok",
        "version": __version__,
        "yt_dlp": ytdlp_version,
        "js_runtime": next((r for r in ("deno", "node", "bun") if shutil.which(r)), None),
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }
