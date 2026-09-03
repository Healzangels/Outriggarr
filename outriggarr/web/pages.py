"""HTML pages. Thin: every action calls the same functions the JSON API uses."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from outriggarr import __version__
from outriggarr.api.connections import (
    ConnectionIn,
    create_connection,
    delete_connection,
    test_connection,
    update_connection,
)
from outriggarr.api.dates import progress_map as date_progress_map
from outriggarr.api.dates import start_date_fetch
from outriggarr.api.deps import ArrFactoryDep, DbSession, RunnerDepsDep
from outriggarr.api.health import cooloff_status, staging_writable
from outriggarr.api.jobs import (
    CANCELLABLE,
    DELETABLE,
    RETRYABLE,
    cancel_job,
    confirm_job,
    delete_job,
    retry_job,
)
from outriggarr.api.library import library_cache
from outriggarr.api.matches import RecheckProgress, progress_of, start_recheck, unchecked_count
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
from outriggarr.causes import likely_cause
from outriggarr.db.models import (
    Connection,
    ConnectionKind,
    Job,
    JobStatus,
    Subscription,
    utcnow,
)
from outriggarr.matcher import OPTIONAL_STRATEGIES, length_mismatch, mmss, normalise_title
from outriggarr.settings import (
    DEFAULTS,
    FORMAT_PRESETS,
    MERGE_CONTAINERS,
    all_settings,
    get_setting,
    preset_for,
)
from outriggarr.source import cookies_state, pot_provider_ready

router = APIRouter(include_in_schema=False)
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Changes whenever a shipped static file does (mtimes are set when the image is built),
# so versioned asset URLs can be cached as immutable.
STATIC_TOKEN = format(
    max(f.stat().st_mtime_ns for f in STATIC_DIR.iterdir() if f.is_file()) % 10**12, "x"
)


def static_url(name: str) -> str:
    return f"/static/{name}?v={STATIC_TOKEN}"


templates.env.globals.update(
    static=static_url,
    RETRYABLE=RETRYABLE,
    CANCELLABLE=CANCELLABLE,
    DELETABLE=DELETABLE,
    JobStatus=JobStatus,
    app_version=__version__,
)


def day_label(dt: datetime | None) -> str:
    """'Today' / 'Yesterday' / 'Mon 1 Sep' (with the year once it is not this one), for
    grouping a long list by day so it reads as a diary, not a wall."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    today = datetime.now(UTC).date()
    day = dt.date()
    if day == today:
        return "Today"
    if (today - day).days == 1:
        return "Yesterday"
    return dt.strftime("%a %-d %b" if day.year == today.year else "%a %-d %b %Y")


def ago(dt: datetime | None) -> str:
    """'3 min ago' / 'in 2 h' — coarse, for tables; the exact time goes in a title attr."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    future = delta.total_seconds() < 0
    secs = abs(int(delta.total_seconds()))
    if secs < 60:
        return "in under a minute" if future else "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            n = secs // size
            return f"in {n} {unit}" if future else f"{n} {unit} ago"
    return "just now"


templates.env.filters["ago"] = ago
templates.env.filters["day_label"] = day_label


def _tooling(request: Request) -> dict:
    from yt_dlp.version import __version__ as ytdlp_version

    staging = request.app.state.settings.staging_dir
    with request.app.state.session_factory() as session:
        cookies_path = get_setting(session, "cookies_path")
    deps = getattr(request.app.state, "runner_deps", None)
    return {
        "youtube_session": cookies_state(cookies_path),
        "youtube_cooloff": cooloff_status(getattr(deps, "cooloff", None)),
        "yt_dlp": ytdlp_version,
        "js_runtime": next((r for r in ("deno", "node", "bun") if shutil.which(r)), None),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "po_token_provider": pot_provider_ready(request.app.state.settings.pot_server_home),
        "staging_writable": staging_writable(staging),
    }


_original_template_response = templates.TemplateResponse


def _render(request: Request, name: str, context: dict, **kw):
    """Every page gets the footer context; partials ignore it."""
    if not name.startswith("partials/"):
        context = {"tooling": _tooling(request), **context}
    return _original_template_response(request, name, context, **kw)


templates.TemplateResponse = _render  # type: ignore[method-assign]

ACTIVE = (JobStatus.queued, JobStatus.downloading, JobStatus.importing)
ACTIVITY_LIMIT = 200
FILTERS = {
    "all": None,
    "active": ACTIVE,
    "failed": (JobStatus.failed,),
    "done": (JobStatus.done, JobStatus.cancelled),
}


def counts_for(session: DbSession) -> dict[str, int]:
    return {
        f: (
            session.scalar(select(func.count()).select_from(Job).where(Job.status.in_(sts)))
            if sts
            else session.scalar(select(func.count()).select_from(Job))
        )
        or 0
        for f, sts in FILTERS.items()
    }


def _jobs(session: DbSession, view: str) -> list[Job]:
    # ordered by the same moment each row shows (finished, else created), so the day
    # dividers run monotonically down the page
    when = func.coalesce(Job.finished_at, Job.created_at)
    q = select(Job).order_by(when.desc(), Job.id.desc())
    statuses = FILTERS.get(view)
    if statuses:
        q = q.where(Job.status.in_(statuses))
    return list(session.scalars(q.limit(ACTIVITY_LIMIT)))


def _rows(
    request: Request, session: DbSession, view: str, notice: str | None = None
) -> HTMLResponse:
    jobs = _jobs(session, view)
    total = counts_for(session).get(view, len(jobs))
    return templates.TemplateResponse(
        request,
        "partials/jobs_table.html",
        {
            "jobs": jobs,
            "view": view,
            "notice": notice,
            "total": total,
            "limit": ACTIVITY_LIMIT,
            "causes": _causes(session, jobs),
        },
    )


def _causes(session: Session, jobs: list[Job]) -> dict[int, str]:
    """A plain-words reading of each failed job's error, keyed by job id."""
    state = cookies_state(get_setting(session, "cookies_path"))
    out: dict[int, str] = {}
    for job in jobs:
        if job.error and job.status is not JobStatus.done:
            cause = likely_cause(job.error, youtube_session=state)
            if cause:
                out[job.id] = cause
    return out


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
    counts = counts_for(session)
    jobs = _jobs(session, view)
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "jobs": jobs,
            "view": view,
            "filters": list(FILTERS),
            "counts": counts,
            "total": counts.get(view, len(jobs)),
            "limit": ACTIVITY_LIMIT,
            "causes": _causes(session, jobs),
        },
    )


REVIEW_LIMIT = 500
MATCHES_PAGE = 100  # the "all" view shows this many unless asked for everything
RISK_ORDER = ("date", "regex", "unknown", "contains", "exact", "override")  # riskiest first


def _tier_inferred(job: Job) -> str:
    """Jobs from before matched_by was recorded: a pin on the subscription names the
    video outright; otherwise the titles say exact/contains; otherwise the subscription's
    other enabled strategy is the only way it could have been paired ("unknown" only
    when regex and date were both on)."""
    sub = job.subscription
    if sub is not None and any(o.video_id == job.video_id for o in sub.overrides):
        return "override"
    label = job.target_label or ""
    want = normalise_title(label.split(" - ", 1)[1] if " - " in label else "")
    have = normalise_title(job.video_title or "")
    if want and want == have:
        return "exact"
    if want and f" {want} " in f" {have} ":
        return "contains"
    others = [s for s in (sub.strategies if sub is not None else []) if s in ("regex", "date")]
    return others[0] if len(others) == 1 else "unknown"


def review_entry(job: Job) -> dict:
    """A pairing needs a look while nothing vouches for it: not an exact title or a
    pin, not a length that agrees with the runtime, and not the operator's confirmation.
    A length that contradicts the runtime always needs a look until confirmed."""
    tier = job.matched_by or _tier_inferred(job)
    reason = length_mismatch(job.target_runtime, job.video_duration)
    evidence = job.video_duration is not None and bool(job.target_runtime)
    if job.reviewed_at is not None:
        state = "confirmed"
    elif reason:
        state = "length mismatch"
    elif tier in ("exact", "override"):
        state = "vouched"
    elif evidence:
        state = "length ok"
    elif job.video_duration is None:
        state = "unchecked"
    else:
        state = "no runtime"
    return {
        "job": job,
        "tier": tier,
        "inferred": job.matched_by is None,
        "reason": reason,
        "state": state,
        "video_length": mmss(job.video_duration) if job.video_duration else None,
        "needs_look": state in ("length mismatch", "unchecked", "no runtime"),
    }


def _matches_context(
    session: Session,
    view: str | None,
    notice: str | None = None,
    progress: RecheckProgress | None = None,
    show_all: bool = False,
) -> dict:
    progress = progress or RecheckProgress()
    jobs = session.scalars(
        select(Job)
        .where(Job.subscription_id.is_not(None))
        .options(selectinload(Job.subscription).selectinload(Subscription.overrides))
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(REVIEW_LIMIT)
    ).all()
    entries = [review_entry(j) for j in jobs]
    counts = {"review": sum(1 for e in entries if e["needs_look"]), "all": len(entries)}
    if view not in ("review", "all"):
        # land where the work is; with nothing to look at, show everything
        view = "review" if counts["review"] else "all"
    if view == "review":
        entries = [e for e in entries if e["needs_look"]]
    entries.sort(key=lambda e: (not e["reason"], RISK_ORDER.index(e["tier"])))  # stable
    total = len(entries)
    if view == "all" and not show_all:
        entries = entries[:MATCHES_PAGE]  # the riskiest first, then the newest
    # the recheck summary reads like a notice: shown while it runs, then once when done
    show_progress = progress.running or (progress.finished_at is not None and not progress.reported)
    if show_progress and not progress.running:
        progress.reported = True
    return {
        "entries": entries,
        "view": view,
        "counts": counts,
        "total": total,
        "show_all": show_all,
        "notice": notice,
        "progress": progress,
        "progress_text": progress.summary() if show_progress else "",
        "unchecked": unchecked_count(session),
    }


@router.get("/matches")
def matches_page(
    request: Request,
    session: DbSession,
    view: Annotated[str | None, Query()] = None,
    limit: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    ctx = _matches_context(
        session, view, progress=progress_of(request.app), show_all=limit == "all"
    )
    return templates.TemplateResponse(request, "matches.html", ctx)


def _matches_partial(
    request: Request,
    session: DbSession,
    view: str | None,
    notice: str | None,
    show_all: bool = False,
):
    ctx = _matches_context(
        session, view, notice, progress=progress_of(request.app), show_all=show_all
    )
    return templates.TemplateResponse(request, "partials/matches_content.html", ctx)


@router.get("/matches/content")
def matches_content(
    request: Request,
    session: DbSession,
    view: Annotated[str | None, Query()] = None,
    limit: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """The table plus the recheck status; polled while a recheck runs."""
    return _matches_partial(request, session, view, None, show_all=limit == "all")


@router.post("/matches/recheck")
async def matches_recheck(
    request: Request, session: DbSession, view: Annotated[str | None, Query()] = None
) -> HTMLResponse:
    start_recheck(request.app)
    return _matches_partial(request, session, view, None)


@router.post("/matches/{job_id}/confirm")
def matches_confirm(
    request: Request, session: DbSession, job_id: int, view: Annotated[str | None, Query()] = None
) -> HTMLResponse:
    job = confirm_job(session, job_id, confirmed=True)
    return _matches_partial(request, session, view, f"Confirmed: {job.target_label}.")


@router.post("/matches/{job_id}/unconfirm")
def matches_unconfirm(
    request: Request, session: DbSession, job_id: int, view: Annotated[str | None, Query()] = None
) -> HTMLResponse:
    job = confirm_job(session, job_id, confirmed=False)
    return _matches_partial(request, session, view, f"Back on the list: {job.target_label}.")


@router.post("/matches/confirm-all")
def matches_confirm_all(
    request: Request, session: DbSession, view: Annotated[str | None, Query()] = None
) -> HTMLResponse:
    listed = [e["job"] for e in _matches_context(session, "review")["entries"]]
    now = utcnow()
    for job in listed:
        job.reviewed_at = now
    session.commit()
    return _matches_partial(request, session, view, f"Confirmed {len(listed)} pairings.")


@router.get("/activity/rows")
def activity_rows(
    request: Request, session: DbSession, view: Annotated[str, Query()] = "all"
) -> HTMLResponse:
    return _rows(request, session, view if view in FILTERS else "all")


@router.post("/activity/jobs/{job_id}/retry")
def activity_retry(
    request: Request, job_id: int, session: DbSession, view: Annotated[str, Query()] = "all"
) -> HTMLResponse:
    try:
        retry_job(session, job_id)
    except HTTPException as exc:
        return _rows(request, session, view, notice=str(exc.detail))
    return _rows(request, session, view)


@router.post("/activity/jobs/{job_id}/delete")
def activity_delete(
    request: Request,
    job_id: int,
    session: DbSession,
    deps: RunnerDepsDep,
    view: Annotated[str, Query()] = "all",
) -> HTMLResponse:
    try:
        delete_job(session, job_id, deps.staging_dir)
    except HTTPException as exc:
        return _rows(request, session, view, notice=str(exc.detail))
    return _rows(request, session, view, notice=f"Job {job_id} deleted.")


@router.post("/activity/jobs/{job_id}/cancel")
def activity_cancel(
    request: Request, job_id: int, session: DbSession, view: Annotated[str, Query()] = "all"
) -> HTMLResponse:
    try:
        cancel_job(session, job_id)
    except HTTPException as exc:
        return _rows(request, session, view, notice=str(exc.detail))
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


def _subscription_form_context(sub: Subscription | None, session: Session) -> dict:
    return {
        "sub": sub,
        "scan_video_limit": get_setting(session, "scan_video_limit"),
        "strategies": sorted(OPTIONAL_STRATEGIES, key=("regex", "title", "date").index),
        "chosen": set(sub.strategies) if sub else {"title"},
        "format_presets": FORMAT_PRESETS,
        "format_preset": preset_for(sub.format) if sub and sub.format else None,
        "global_format_preset": preset_for(get_setting(session, "default_format")),
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
            **_subscription_form_context(None, session),
        },
    )


def _form_to_body(
    connection_id: int,
    series_id: int,
    sources: str,
    format: str,
    strategies: list[str],
    date_tolerance_days: int,
    date_offset_days: int,
    title_regex: str,
    enabled: bool,
    video_limit: str = "",
    audio_language: str = "",
    auto_download: str = "future",
    title_require: str = "",
) -> SubscriptionIn:
    video_limit = video_limit.strip()
    if video_limit and not video_limit.isdigit():
        raise ValueError("Videos to list must be a whole number, or blank for the global setting")
    return SubscriptionIn(
        video_limit=int(video_limit) if video_limit else None,
        audio_language=audio_language,
        auto_download=auto_download,
        title_require=title_require,
        connection_id=connection_id,
        series_id=series_id,
        sources=sources.splitlines(),
        format=format,
        strategies=strategies or [],
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
    sources: Annotated[str, Form()] = "",
    format: Annotated[str, Form()] = "",
    strategies: Annotated[list[str] | None, Form()] = None,
    date_tolerance_days: Annotated[int, Form()] = 2,
    date_offset_days: Annotated[int, Form()] = 0,
    title_regex: Annotated[str, Form()] = "",
    video_limit: Annotated[str, Form()] = "",
    audio_language: Annotated[str, Form()] = "",
    auto_download: Annotated[str, Form()] = "future",
    title_require: Annotated[str, Form()] = "",
) -> HTMLResponse:
    conn = _sonarr(session)
    if conn is None:
        return RedirectResponse("/series", status_code=302)
    try:
        body = _form_to_body(
            conn.id,
            series_id,
            sources,
            format,
            strategies,
            date_tolerance_days,
            date_offset_days,
            title_regex,
            True,
            video_limit,
            audio_language,
            auto_download,
            title_require,
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
                **_subscription_form_context(None, session),
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
        {"jobs": jobs, "error": None, **_subscription_form_context(sub, session)},
    )


def _date_fetch_context(request: Request, sub: Subscription, report=None) -> dict:
    p = date_progress_map(request.app).get(sub.id)
    show = p is not None and (p.running or (p.finished_at is not None and not p.reported))
    if show and p is not None and not p.running:
        p.reported = True  # the finished summary reads like a notice: once
    undated = (
        sum(1 for v in report.videos if not v.get("upload_date") and v["title"] != v["id"])
        if report is not None
        else None
    )
    return {
        "date_progress": p,
        "date_progress_text": p.summary() if show and p else "",
        "undated": undated,
    }


def _preview_response(request: Request, session: DbSession, report, notice: str | None = None):
    sub = session.get(Subscription, report.subscription_id)
    # "this source may not carry the series" is only a fair reading for a subscription
    # that has never matched anything: one job on record is enough to drop the hint
    has_history = bool(
        session.scalar(select(Job.id).where(Job.subscription_id == report.subscription_id).limit(1))
    )
    return templates.TemplateResponse(
        request,
        "partials/preview.html",
        {
            "sub": sub,
            "report": report,
            "notice": notice,
            "has_history": has_history,
            **_date_fetch_context(request, sub, report),
        },
    )


@router.post("/subscriptions/{subscription_id}/dates")
async def subscription_fetch_dates(
    request: Request, subscription_id: int, session: DbSession
) -> HTMLResponse:
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subscription not found")
    start_date_fetch(request.app, subscription_id)
    return templates.TemplateResponse(
        request, "partials/date_fetch.html", {"sub": sub, **_date_fetch_context(request, sub)}
    )


@router.get("/subscriptions/{subscription_id}/dates/status")
async def subscription_fetch_dates_status(
    request: Request, subscription_id: int, session: DbSession
) -> HTMLResponse:
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subscription not found")
    return templates.TemplateResponse(
        request, "partials/date_fetch.html", {"sub": sub, **_date_fetch_context(request, sub)}
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
    return await _episodes_response(request, sub, session, arr_factory)


@router.post("/subscriptions/{subscription_id}/episodes/jobs/{job_id}/clear")
async def subscription_clear_job(
    request: Request,
    subscription_id: int,
    job_id: int,
    session: DbSession,
    arr_factory: ArrFactoryDep,
    deps: RunnerDepsDep,
) -> HTMLResponse:
    """The red ✗ on a missing episode whose job is history (the file was deleted in
    Sonarr after the import): delete that job here rather than hunting it in Activity.
    A cancelled or terminally failed job is history too, and clearing it also puts the
    episode back on offer."""
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        return RedirectResponse("/series", status_code=302)
    try:
        delete_job(session, job_id, deps.staging_dir)
        notice = f"Cleared job #{job_id}."
    except HTTPException as exc:
        notice = f"Job #{job_id} not cleared: {exc.detail}"
    return await _episodes_response(request, sub, session, arr_factory, notice)


async def _episodes_response(
    request: Request,
    sub: Subscription,
    session: Session,
    arr_factory,
    notice: str | None = None,
) -> HTMLResponse:
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
        {"sub": sub, "seasons": ordered, "error": error, "notice": notice},
    )


@router.post("/subscriptions/{subscription_id}/scan")
async def subscription_scan(
    request: Request, subscription_id: int, session: DbSession, deps: RunnerDepsDep
) -> HTMLResponse:
    """Scan now = refresh the preview. Nothing is queued; that is the Download button."""
    report = await run_scan(deps, subscription_id, dry_run=True)
    notice = None
    if not report.error:
        notice = (
            f"Scan done: {len(report.matches)} matched, {len(report.unmatched)} unmatched, "
            f"{len(report.skipped_existing)} already have jobs. Nothing queued."
        )
    return _preview_response(request, session, report, notice)


@router.post("/subscriptions/{subscription_id}/download")
async def subscription_download(
    request: Request,
    subscription_id: int,
    session: DbSession,
    deps: RunnerDepsDep,
    episode_id: Annotated[list[int] | None, Form()] = None,
    all: Annotated[str, Form()] = "",
    selected: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Download = a manual scan: every current match, or only the selected episodes,
    whatever the auto-download policy says."""
    ids = None if all or not selected else set(episode_id or [])
    if ids is not None and not ids:
        report = await run_scan(deps, subscription_id, dry_run=True)
        return _preview_response(request, session, report, "Nothing selected.")
    report = await run_scan(deps, subscription_id, dry_run=False, manual=True, episode_ids=ids)
    notice = None
    if not report.error:
        n = len(report.created_job_ids)
        notice = f"Queued {n} job(s)." if n else "Nothing to queue: no new matches."
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
    try:
        delete_override(session, subscription_id, video_id)
        notice = f"Override removed for {video_id}."
    except HTTPException as exc:
        notice = str(exc.detail)
    report = await run_scan(deps, subscription_id, dry_run=True)
    return _preview_response(request, session, report, notice)


@router.post("/subscriptions/{subscription_id}/edit")
async def subscription_edit(
    request: Request,
    subscription_id: int,
    session: DbSession,
    arr_factory: ArrFactoryDep,
    sources: Annotated[str, Form()] = "",
    format: Annotated[str, Form()] = "",
    strategies: Annotated[list[str] | None, Form()] = None,
    date_tolerance_days: Annotated[int, Form()] = 2,
    date_offset_days: Annotated[int, Form()] = 0,
    title_regex: Annotated[str, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    video_limit: Annotated[str, Form()] = "",
    audio_language: Annotated[str, Form()] = "",
    auto_download: Annotated[str, Form()] = "future",
    title_require: Annotated[str, Form()] = "",
) -> HTMLResponse:
    sub = session.get(Subscription, subscription_id)
    if sub is None:
        return RedirectResponse("/series", status_code=302)
    try:
        body = _form_to_body(
            sub.connection_id,
            sub.series_id,
            sources,
            format,
            strategies,
            date_tolerance_days,
            date_offset_days,
            title_regex,
            enabled is not None,
            video_limit,
            audio_language,
            auto_download,
            title_require,
        )
        await update_subscription(session, arr_factory, subscription_id, body)
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return templates.TemplateResponse(
            request,
            "subscription.html",
            {"jobs": [], "error": detail, **_subscription_form_context(sub, session)},
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
    settings = all_settings(session)
    return {
        "connections": list(session.scalars(select(Connection).order_by(Connection.id))),
        "settings": settings,
        "defaults": DEFAULTS,
        "format_presets": FORMAT_PRESETS,
        "default_preset": preset_for(settings["default_format"]),
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
    if data.get("_notify_form"):  # unchecked boxes are simply absent from the POST
        for k in ("notify_on_failed", "notify_on_scan_error", "notify_on_done"):
            changes[k] = "1" if data.get(k) == "1" else "0"
    try:
        update_settings(session, changes)
    except Exception as exc:
        session.rollback()
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


@router.post("/settings/notify/test")
async def settings_notify_test(request: Request, session: DbSession, deps: RunnerDepsDep):
    from outriggarr.api.settings import notify_test

    try:
        result = await notify_test(session, deps)
        text = (
            "✓ sent"
            if result["sent"]
            else "✗ no target accepted the message (check the URLs / logs)"
        )
    except Exception as exc:
        text = "✗ " + str(getattr(exc, "detail", None) or exc)
    return HTMLResponse(f'<span class="{"ok" if text.startswith("✓") else "warn"}">{text}</span>')


@router.post("/settings/connections")
async def settings_connection_create(request: Request, session: DbSession) -> HTMLResponse:
    data = await _read_form(request)
    try:
        create_connection(_connection_body(data), session)
    except Exception as exc:
        session.rollback()
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
        session.rollback()
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
        session.rollback()
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
