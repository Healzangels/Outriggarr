from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.orm import Session

from outriggarr.api.deps import DbSession
from outriggarr.settings import DEFAULTS, all_settings, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])


def update_settings(session: Session, changes: dict[str, str]) -> dict[str, str]:
    """Validate every change before writing any; 422 names the offending key."""
    unknown = [k for k in changes if k not in DEFAULTS]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unknown setting(s) {unknown}; known: {sorted(DEFAULTS)}",
        )
    try:
        for key, value in changes.items():
            set_setting(session, key, str(value))
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    session.commit()
    return all_settings(session)


@router.get("")
def get_settings(session: DbSession) -> dict[str, str]:
    return all_settings(session)


@router.put("")
def put_settings(body: dict[str, str], session: DbSession) -> dict[str, str]:
    return update_settings(session, body)
