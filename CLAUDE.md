# CLAUDE.md — Outriggarr

Companion app that fills Sonarr/Radarr wanted lists from YouTube via yt-dlp and hands files back through their manual-import APIs. Read `DESIGN.md` before starting any milestone; it is the source of truth for scope and architecture.

## Principles (apply, don't recite)

- **KISS** — the simplest thing that works. One process, one SQLite file, one job pipeline for every source of work.
- **YAGNI** — build only what the current milestone needs. Anything under "Later" in DESIGN.md is out of bounds until the design changes. No speculative abstractions, feature flags, or config for hypothetical needs.
- **SOLID** — each module has one job (see layout). Depend on the `ArrClient` and `VideoSource` protocols, not on Sonarr/Radarr/yt-dlp directly, so tests use fakes. Sonarr/Radarr differences live only in `arr/`.

## Hard rules

- Never write into a Sonarr/Radarr root folder. The only write location is `/staging/<job-id>/`.
- Every *arr call goes through `arr/`; every yt-dlp call through `source.py`; every DB access through `db/`. No exceptions in web or worker code.
- All *arr endpoints are `/api/v3/...`. The un-versioned `/api/command` does not exist in Sonarr v4.
- Surface *arr and yt-dlp error text verbatim on the job (`job.error`); never swallow or paraphrase it.
- Do not invent paths, hostnames, ports, or container names. Docs and examples use placeholders; runtime values come from settings/env. Ask when a real value is needed.
- Before any change that affects deployment (new port, new mount, new outbound dependency, storing a secret), state the benefit and the risk — especially whether it adds exposure — before making it.
- Stop at milestone boundaries and confirm before starting the next one.

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.x + Alembic (SQLite) · yt-dlp as a library · httpx · Jinja2 + HTMX (+ Alpine.js only where a form needs client state) · pytest · ruff.

## Layout

```
outriggarr/
  main.py          app factory, starts worker task, SIGTERM handling
  settings.py      env + DB-backed settings access
  db/              models.py, session.py, migrations/
  arr/             base.py (ArrClient protocol), sonarr.py, radarr.py
  source.py        VideoSource protocol + YtDlpSource
  matcher.py       pure functions: episodes × videos × overrides → matches
  naming.py        staging filename + quality mapping (pure)
  worker/          scheduler.py (subscriptions → jobs), runner.py (job state machine)
  api/             FastAPI routers (JSON)
  web/             page routes, templates/, static/
tests/             fakes for ArrClient and VideoSource; no network in tests
Dockerfile · pyproject.toml · DESIGN.md · CLAUDE.md
```

Dependency direction: `web → api → db/arr/source/matcher`; `worker → db/arr/source/matcher`. `web` never calls the worker; it writes rows and the worker picks them up.

## Commands

```
uv sync                     # install (dev extras included)
uv run pytest               # tests
uv run ruff check . && uv run ruff format .
uv run alembic revision --autogenerate -m "..."   # after model changes
uv run uvicorn outriggarr.main:app --reload       # dev server
docker build -t outriggarr .
```

## Conventions

- Type hints everywhere; dataclasses or Pydantic models at boundaries, plain functions inside.
- `matcher.py` and `naming.py` are pure and fully unit-tested; that is where correctness matters most.
- Job status values are the enum in `db/models.py`; do not compare against string literals elsewhere.
- Logging via stdlib `logging`, one logger per module, no print.
- Templates render server-side; HTMX for partial updates and polling. No JS build step.
- Keep `DESIGN.md` current: if an implementation decision contradicts it, change the doc in the same commit and say so.
