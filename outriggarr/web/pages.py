"""HTML pages. Thin: every action calls the same functions the JSON API uses."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from outriggarr.api.deps import DbSession
from outriggarr.api.jobs import CANCELLABLE, RETRYABLE, cancel_job, retry_job
from outriggarr.db.models import Connection, Job, JobStatus

router = APIRouter(include_in_schema=False)
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals.update(RETRYABLE=RETRYABLE, CANCELLABLE=CANCELLABLE, JobStatus=JobStatus)

ACTIVE = (JobStatus.queued, JobStatus.downloading, JobStatus.importing)
FILTERS = {
    "all": None,
    "active": ACTIVE,
    "failed": (JobStatus.failed,),
    "done": (JobStatus.done, JobStatus.cancelled),
}


def _jobs(session: DbSession, view: str) -> list[Job]:
    q = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
    statuses = FILTERS.get(view)
    if statuses:
        q = q.where(Job.status.in_(statuses))
    return list(session.scalars(q.limit(200)))


def _rows(request: Request, session: DbSession, view: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/jobs_table.html", {"jobs": _jobs(session, view), "view": view}
    )


@router.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/activity", status_code=302)


@router.get("/activity")
def activity(
    request: Request, session: DbSession, view: Annotated[str, Query()] = "all"
) -> HTMLResponse:
    view = view if view in FILTERS else "all"
    return templates.TemplateResponse(
        request,
        "activity.html",
        {"jobs": _jobs(session, view), "view": view, "filters": list(FILTERS)},
    )


@router.get("/activity/rows")
def activity_rows(
    request: Request, session: DbSession, view: Annotated[str, Query()] = "all"
) -> HTMLResponse:
    return _rows(request, session, view if view in FILTERS else "all")


@router.post("/activity/jobs/{job_id}/retry")
def activity_retry(
    request: Request, job_id: int, session: DbSession, view: Annotated[str, Query()] = "all"
) -> HTMLResponse:
    retry_job(session, job_id)
    return _rows(request, session, view)


@router.post("/activity/jobs/{job_id}/cancel")
def activity_cancel(
    request: Request, job_id: int, session: DbSession, view: Annotated[str, Query()] = "all"
) -> HTMLResponse:
    cancel_job(session, job_id)
    return _rows(request, session, view)


@router.get("/grab")
def grab(request: Request, session: DbSession) -> HTMLResponse:
    connections = list(
        session.scalars(select(Connection).where(Connection.enabled).order_by(Connection.id))
    )
    return templates.TemplateResponse(
        request,
        "grab.html",
        {
            "connections": connections,
            "connection_ids": [c.id for c in connections],
            "kinds": {c.id: c.kind.value for c in connections},
        },
    )
