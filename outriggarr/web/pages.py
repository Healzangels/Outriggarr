"""HTML pages. Thin: every action calls the same functions the JSON API uses."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from outriggarr.api.connections import (
    ConnectionIn,
    create_connection,
    delete_connection,
    test_connection,
    update_connection,
)
from outriggarr.api.deps import ArrFactoryDep, DbSession, RunnerDepsDep
from outriggarr.api.jobs import CANCELLABLE, RETRYABLE, cancel_job, retry_job
from outriggarr.api.library import library_cache
from outriggarr.api.settings import update_settings
from outriggarr.api.subscriptions import (
    OverrideByUrlIn,
    OverrideIn,
    SubscriptionIn,
    create_subscription,
    delete_override,
    delete_subscription,
    run_scan,
    set_override,
    set_override_by_url,
    update_subscription,
)
from outriggarr.arr.base import ArrError
from outriggarr.db.models import Connection, ConnectionKind, Job, JobStatus, Subscription
from outriggarr.matcher import OPTIONAL_STRATEGIES
from outriggarr.settings import DEFAULTS, MERGE_CONTAINERS, all_settings

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


@router.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


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


# ---- Series / subscriptions -------------------------------------------------------


def _sonarr(session: DbSession) -> Connection | None:
    return session.scalar(
        select(Connection)
        .where(Connection.kind == ConnectionKind.sonarr, Connection.enabled)
        .order_by(Connection.id)
    )


def _subscribed(session: DbSession, connection_id: int) -> dict[int, Subscription]:
    subs = session.scalars(select(Subscription).where(Subscription.connection_id == connection_id))
    return {s.series_id: s for s in subs}


async def _series_list(conn: Connection, arr_factory) -> list:
    return await library_cache.get((conn.id, "series"), arr_factory(conn).series)


@router.get("/series")
def series_page(request: Request, session: DbSession) -> HTMLResponse:
    conn = _sonarr(session)
    subs = list(session.scalars(select(Subscription).order_by(Subscription.title)))
    return templates.TemplateResponse(
        request, "series.html", {"connection": conn, "subscriptions": subs}
    )


@router.get("/series/rows")
async def series_rows(
    request: Request,
    session: DbSession,
    arr_factory: ArrFactoryDep,
    q: Annotated[str, Query()] = "",
) -> HTMLResponse:
    conn = _sonarr(session)
    rows: list = []
    error = None
    if conn is not None:
        try:
            series = await _series_list(conn, arr_factory)
        except ArrError as exc:
            series, error = [], str(exc)
        needle = q.strip().lower()
        if needle:
            hits = [s for s in series if needle in s.title.lower()]
            hits.sort(key=lambda s: (not s.title.lower().startswith(needle), s.title.lower()))
            rows = hits[:50]
        subscribed = _subscribed(session, conn.id)
    else:
        subscribed = {}
    return templates.TemplateResponse(
        request,
        "partials/series_rows.html",
        {"rows": rows, "q": q, "subscribed": subscribed, "error": error, "connection": conn},
    )


def _subscription_form_context(sub: Subscription | None) -> dict:
    return {
        "sub": sub,
        "strategies": sorted(OPTIONAL_STRATEGIES, key=("regex", "title", "date").index),
        "chosen": set(sub.strategies) if sub else {"title"},
    }


@router.get("/series/{series_id}/subscribe")
async def subscribe_form(
    request: Request, series_id: int, session: DbSession, arr_factory: ArrFactoryDep
) -> HTMLResponse:
    conn = _sonarr(session)
    if conn is None:
        return RedirectResponse("/series", status_code=302)
    existing = _subscribed(session, conn.id).get(series_id)
    if existing is not None:
        return RedirectResponse(f"/subscriptions/{existing.id}", status_code=302)
    title = ""
    try:
        hit = next((s for s in await _series_list(conn, arr_factory) if s.id == series_id), None)
        title = hit.title if hit else ""
    except ArrError:
        pass
    return templates.TemplateResponse(
        request,
        "subscribe.html",
        {
            "connection": conn,
            "series_id": series_id,
            "title": title,
            "error": None,
            **_subscription_form_context(None),
        },
    )


def _form_to_body(
    connection_id: int,
    series_id: int,
    source_url: str,
    format: str,
    strategies: list[str],
    date_tolerance_days: int,
    date_offset_days: int,
    title_regex: str,
    enabled: bool,
) -> SubscriptionIn:
    return SubscriptionIn(
        connection_id=connection_id,
        series_id=series_id,
        source_url=source_url,
        format=format,
        strategies=strategies,
        date_tolerance_days=date_tolerance_days,
        date_offset_days=date_offset_days,
        title_regex=title_regex,
        enabled=enabled,
    )


@router.post("/series/{series_id}/subscribe")
async def subscribe_submit(
    request: Request,
    series_id: int,
    session: DbSession,
    arr_factory: ArrFactoryDep,
    source_url: Annotated[str, Form()] = "",
    format: Annotated[str, Form()] = "",
    strategies: Annotated[list[str] | None, Form()] = None,
    date_tolerance_days: Annotated[int, Form()] = 2,
    date_offset_days: Annotated[int, Form()] = 0,
    title_regex: Annotated[str, Form()] = "",
) -> HTMLResponse:
    conn = _sonarr(session)
    if conn is None:
        return RedirectResponse("/series", status_code=302)
    try:
        body = _form_to_body(
            conn.id,
            series_id,
            source_url,
            format,
            strategies,
            date_tolerance_days,
            date_offset_days,
            title_regex,
            True,
        )
        sub = await create_subscription(session, arr_factory, body)
    except Exception as exc:  # validation / 409 / 502: show it on the form
        detail = getattr(exc, "detail", None) or str(exc)
        return templates.TemplateResponse(
            request,
            "subscribe.html",
            {
                "connection": conn,
                "series_id": series_id,
                "title": "",
                "error": detail,
                **_subscription_form_context(None),
            },
            status_code=400,
        )
    return RedirectResponse(f"/subscriptions/{sub.id}", status_code=303)


@router.get("/subscriptions/{subscription_id}")
def subscription_page(request: Request, subscription_id: int, session: DbSession) -> HTMLResponse:
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        return RedirectResponse("/series", status_code=302)
    jobs = list(
        session.scalars(
            select(Job)
            .where(Job.subscription_id == sub.id)
            .order_by(Job.created_at.desc())
            .limit(20)
        )
    )
    return templates.TemplateResponse(
        request,
        "subscription.html",
        {"jobs": jobs, "error": None, **_subscription_form_context(sub)},
    )


def _preview_response(request: Request, session: DbSession, report, notice: str | None = None):
    sub = session.get(Subscription, report.subscription_id)
    return templates.TemplateResponse(
        request, "partials/preview.html", {"sub": sub, "report": report, "notice": notice}
    )


@router.get("/subscriptions/{subscription_id}/preview")
async def subscription_preview(
    request: Request, subscription_id: int, session: DbSession, deps: RunnerDepsDep
) -> HTMLResponse:
    report = await run_scan(deps, subscription_id, dry_run=True)
    return _preview_response(request, session, report)


@router.get("/subscriptions/{subscription_id}/episodes")
async def subscription_episodes(
    request: Request, subscription_id: int, session: DbSession, arr_factory: ArrFactoryDep
) -> HTMLResponse:
    """Sonarr's view of every episode of the series, with the job that covers each."""
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        return RedirectResponse("/series", status_code=302)
    error = None
    try:
        episodes = await arr_factory(sub.connection).episodes(sub.series_id)
    except ArrError as exc:
        episodes, error = [], str(exc)
    jobs_by_episode: dict[int, Job] = {}
    for job in session.scalars(
        select(Job)
        .where(Job.connection_id == sub.connection_id, Job.series_id == sub.series_id)
        .order_by(Job.created_at)
    ):
        for eid in job.episode_ids or []:
            jobs_by_episode[int(eid)] = job  # latest job wins
    now = datetime.now(UTC)
    seasons: dict[int, list[dict]] = {}
    for e in episodes:
        if e.has_file:
            state = "file"
        elif not e.monitored:
            state = "unmonitored"
        elif e.air_date_utc is None or e.air_date_utc > now:
            state = "unaired"
        else:
            state = "missing"
        seasons.setdefault(e.season_number, []).append(
            {"ep": e, "state": state, "job": jobs_by_episode.get(e.id)}
        )
    ordered = []
    for season in sorted(seasons, reverse=True):
        rows = sorted(seasons[season], key=lambda r: r["ep"].episode_number)
        ordered.append(
            {
                "season": season,
                "rows": rows,
                "files": sum(1 for r in rows if r["state"] == "file"),
                "missing": sum(1 for r in rows if r["state"] == "missing"),
                "total": len(rows),
            }
        )
    return templates.TemplateResponse(
        request,
        "partials/episodes.html",
        {"sub": sub, "seasons": ordered, "error": error},
    )


@router.post("/subscriptions/{subscription_id}/scan")
async def subscription_scan(
    request: Request, subscription_id: int, session: DbSession, deps: RunnerDepsDep
) -> HTMLResponse:
    report = await run_scan(deps, subscription_id, dry_run=False)
    n = len(report.created_job_ids)
    notice = f"Scan done: {n} job(s) queued." if not report.error else None
    return _preview_response(request, session, report, notice)


@router.post("/subscriptions/{subscription_id}/overrides")
async def subscription_add_override(
    request: Request,
    subscription_id: int,
    session: DbSession,
    deps: RunnerDepsDep,
    season: Annotated[int, Form()],
    episode: Annotated[int, Form()],
    video_id: Annotated[str, Form()] = "",
    video_url: Annotated[str, Form()] = "",
) -> HTMLResponse:
    video_url, video_id = video_url.strip(), video_id.strip()
    try:
        if video_url:
            row = await set_override_by_url(
                session,
                deps.source,
                subscription_id,
                OverrideByUrlIn(url=video_url, season=season, episode=episode),
            )
            notice = f"Override set for {row.video_title or row.video_id} (from URL)."
        elif video_id:
            set_override(
                session, subscription_id, video_id, OverrideIn(season=season, episode=episode)
            )
            notice = f"Override set for {video_id}."
        else:
            notice = "Pick a video or paste a URL."
    except Exception as exc:
        notice = "Override not set: " + str(getattr(exc, "detail", None) or exc)
    report = await run_scan(deps, subscription_id, dry_run=True)
    return _preview_response(request, session, report, notice)


@router.post("/subscriptions/{subscription_id}/overrides/{video_id}/delete")
async def subscription_delete_override(
    request: Request, subscription_id: int, video_id: str, session: DbSession, deps: RunnerDepsDep
) -> HTMLResponse:
    delete_override(session, subscription_id, video_id)
    report = await run_scan(deps, subscription_id, dry_run=True)
    return _preview_response(request, session, report, f"Override removed for {video_id}.")


@router.post("/subscriptions/{subscription_id}/edit")
async def subscription_edit(
    request: Request,
    subscription_id: int,
    session: DbSession,
    arr_factory: ArrFactoryDep,
    source_url: Annotated[str, Form()] = "",
    format: Annotated[str, Form()] = "",
    strategies: Annotated[list[str] | None, Form()] = None,
    date_tolerance_days: Annotated[int, Form()] = 2,
    date_offset_days: Annotated[int, Form()] = 0,
    title_regex: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        return RedirectResponse("/series", status_code=302)
    try:
        body = _form_to_body(
            sub.connection_id,
            sub.series_id,
            source_url,
            format,
            strategies,
            date_tolerance_days,
            date_offset_days,
            title_regex,
            enabled is not None,
        )
        await update_subscription(session, arr_factory, subscription_id, body)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return templates.TemplateResponse(
            request,
            "subscription.html",
            {"jobs": [], "error": detail, **_subscription_form_context(sub)},
            status_code=400,
        )
    return RedirectResponse(f"/subscriptions/{subscription_id}", status_code=303)


@router.post("/subscriptions/{subscription_id}/delete")
async def subscription_delete(
    subscription_id: int, session: DbSession, arr_factory: ArrFactoryDep
) -> RedirectResponse:
    await delete_subscription(session, arr_factory, subscription_id)
    return RedirectResponse("/series", status_code=303)


# ---- Settings ----------------------------------------------------------------------


def _settings_context(session: DbSession, **extra) -> dict:
    return {
        "connections": list(session.scalars(select(Connection).order_by(Connection.id))),
        "settings": all_settings(session),
        "defaults": DEFAULTS,
        "containers": MERGE_CONTAINERS,
        "kinds": [k.value for k in ConnectionKind],
        "conn_error": None,
        "settings_error": None,
        "notice": None,
        **extra,
    }


@router.get("/settings")
def settings_page(request: Request, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", _settings_context(session))


async def _read_form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


@router.post("/settings/downloads")
async def settings_downloads_post(request: Request, session: DbSession) -> HTMLResponse:
    data = await _read_form(request)
    changes = {k: data.get(k, "") for k in DEFAULTS if k in data}
    try:
        update_settings(session, changes)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(session, settings_error=detail),
            status_code=400,
        )
    return RedirectResponse("/settings?saved=downloads", status_code=303)


def _connection_body(data: dict[str, str]) -> ConnectionIn:
    return ConnectionIn(
        kind=data.get("kind", ""),
        name=data.get("name", ""),
        url=data.get("url", ""),
        api_key=data.get("api_key", ""),
        staging_path_remote=data.get("staging_path_remote", ""),
        enabled=data.get("enabled") is not None,
    )


@router.post("/settings/connections")
async def settings_connection_create(request: Request, session: DbSession) -> HTMLResponse:
    data = await _read_form(request)
    try:
        create_connection(_connection_body(data), session)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(session, conn_error=detail),
            status_code=400,
        )
    return RedirectResponse("/settings?saved=connection", status_code=303)


@router.post("/settings/connections/{connection_id}")
async def settings_connection_update(
    request: Request, connection_id: int, session: DbSession
) -> HTMLResponse:
    data = await _read_form(request)
    try:
        conn = session.get(Connection, connection_id)
        if conn is not None and not data.get("api_key"):
            data["api_key"] = conn.api_key  # blank field keeps the stored key
        update_connection(connection_id, _connection_body(data), session)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(session, conn_error=detail),
            status_code=400,
        )
    return RedirectResponse("/settings?saved=connection", status_code=303)


@router.post("/settings/connections/{connection_id}/delete")
def settings_connection_delete(request: Request, connection_id: int, session: DbSession):
    try:
        delete_connection(connection_id, session)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(session, conn_error=detail),
            status_code=400,
        )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/connections/{connection_id}/test")
async def settings_connection_test(
    request: Request, connection_id: int, session: DbSession, arr_factory: ArrFactoryDep
) -> HTMLResponse:
    result = await test_connection(connection_id, session, arr_factory)
    return templates.TemplateResponse(
        request, "partials/connection_test.html", {"result": result, "connection_id": connection_id}
    )


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
