from fastapi import APIRouter, Request
from sqlalchemy import text

from outriggarr import __version__

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    with request.app.state.session_factory() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok", "version": __version__}
