# Outriggarr — design draft v0.3

YouTube → Sonarr/Radarr bridge. Name: an outrigger is the float rigged alongside a hull to keep it stable — this app rides alongside the *arr stack and never replaces it.

**Changes from v0.1:** adds a web GUI, Radarr as a second target, manual grabs and playlist mapping, and moves configuration from YAML into a database because the GUI edits it. The core pipeline (stage → *arr manual import) is unchanged.

**Changes from v0.2 (M0 implementation):** name settled; the job dedupe key is a stored `target_key` column rather than a constraint over the raw target ids; runtime paths/port come from `OUTRIGGARR_*` env vars.

## Goal

Sonarr and Radarr stay the management platforms: they decide what is wanted (monitoring), acceptable quality, naming, folder layout, and notifications. This app is a companion that fills their *wanted* lists from YouTube (or any yt-dlp site) and hands finished files back through their own import APIs.

"Monitored in this app" means *a Sonarr series has a source attached here*. Which episodes are actually wanted still comes from Sonarr's monitoring. The app never has its own notion of a monitored episode, and it never writes into a Sonarr/Radarr root folder — only into a staging directory both containers can see.

## Scope

**v1**

- Web GUI: connections, series subscriptions, manual grab (single URL or playlist), activity/queue.
- Sonarr: automatic (subscriptions) and manual.
- Radarr: manual only — paste a URL, pick the movie. Channels do not map to movie lists in any reliable way.
- One instance each of Sonarr and Radarr.

**Later (explicitly not v1)**

- Radarr auto-matching; multiple *arr instances; built-in auth; notifications; a public API key for automation; Torznab/download-client emulation so grabs show in Sonarr's queue; quality upgrades (cutoff-unmet).

## Prior art

- `ryakel/stream-harvestarr` (maintained fork of `whatdaybob/sonarr_youtubedl`; GPL-3.0): config.yml only, no GUI, downloads straight into the Sonarr root and rescans, requires the TVDB title to match the upload title. Features worth matching: per-series format, air-date offset for pre-release series, cookies.txt, rate-limit backoff, subtitles, regex. Reading it for ideas is fine; copying code would put this project under GPL.
- `MegaR/Tuberr`, `derekantrican/subarr`, Tube Archivist, TubeSync (already running on CMacServer): YouTube-first tools that either replace Sonarr or feed Plex directly. Sonarr is not in the loop.
- Sonarr issue #6445: TVDB carries YouTube-native shows and Sonarr will add them with "YouTube" as the network, but has no way to download them. That gap is this project.

Differentiators: files go through the *arr import pipeline (rename, quality, notifications); imports carry explicit episode/movie IDs so no filename parsing is load-bearing; a GUI with match preview so bad matches are caught before download.

## Architecture

One container, one process:

```
Browser ──► FastAPI (HTML pages + JSON API) ──► SQLite (/config/app.db)
                                                    ▲
                     Worker (background task) ──────┘
                       ├─ Scheduler: scan subscriptions → create jobs
                       └─ Runner: job → yt-dlp → /staging → ArrClient.manual_import()
```

The KISS insight: **everything is a Job**. A subscription scan, a pasted URL, and a mapped playlist all produce the same `Job(target, video)` row, and one runner processes them. Subscriptions are just job factories.

### Why not the alternatives

| | Chosen: pull/reconcile + GUI | Torznab indexer + SABnzbd emulation |
|---|---|---|
| Who initiates | App polls wanted lists; user queues manual jobs | Sonarr searches and grabs |
| *arr-side config | API key + shared staging mount | Add an indexer and a download client per instance |
| Benefit | Small surface; GUI gives queue visibility without emulating anything | Grabs show in Sonarr's own queue |
| Risk | App owns scheduling/retries | Two protocol emulations to keep compatible; on-demand searches need pre-indexed channels |

Deferred, not rejected. Source, matcher, and runner carry over unchanged if it is added.

## Domain model

| Table | Fields (essentials) |
|---|---|
| `connection` | id, kind (`sonarr`/`radarr`), name, url, api_key, staging_path_remote (how that *arr sees `/staging`), enabled |
| `subscription` | id, connection_id, series_id (Sonarr's), tvdb_id, title (snapshot for display), source_url, format (nullable → global default), strategies, date_tolerance_days, date_offset_days, title_regex, enabled, last_scan_at |
| `override` | subscription_id, video_id, season, episode |
| `job` | id, connection_id, target_kind (`episode`/`movie`), series_id, episode_ids (JSON), movie_id, target_key, video_id, video_url, video_title, status, progress_pct, staged_path, error, attempts, next_retry_at, created_at, finished_at |
| `setting` | key, value — scan interval, concurrency, default format, merge container, yt-dlp extra opts (JSON), cookies path |

Job status: `queued → downloading → importing → done | failed | cancelled`. Unique constraint on `(connection_id, target_key, video_id)` for dedupe, where `target_key` is derived from the target ids (`episode:<series_id>:<sorted episode ids>` or `movie:<movie_id>`) so SQLite has a scalar column to constrain instead of a JSON list.

## Job pipeline

1. **Download**: yt-dlp (library, not subprocess) into `/staging/<job-id>/<parseable name>.<ext>`, video+audio merged by ffmpeg. Progress hook writes `progress_pct` to the job row.
2. **Import** via `ArrClient` (one interface, two implementations):
   - `GET /api/v3/manualimport?folder=<staging_path_remote>/<job-id>&filterExistingFiles=true`
   - Take the returned entry; set `seriesId` + `episodeIds` (Sonarr) or `movieId` (Radarr), `quality`, `languages`.
   - `POST /api/v3/command {"name": "ManualImport", "files": [...], "importMode": "move"}`
   - Poll `GET /api/v3/command/{id}` until completed; surface rejection reasons from the GET step verbatim in the job's `error`.
3. The *arr moves and renames the file into the library and fires its own notifications.
4. Remove the job's staging folder.

`DownloadedEpisodesScan` / `DownloadedMoviesScan` (single call, path + importMode) still exist on `/api/v3/command` but depend on the filename parser — keep as a fallback switch only. The un-versioned `/api/command` path is gone in Sonarr v4; everything is `/api/v3/`.

Runner: one download at a time by default (YouTube throttling), configurable. yt-dlp is blocking, so it runs in a thread pool under the asyncio worker.

## Scheduler (subscriptions)

Every `scan_interval` for each enabled subscription:

1. `GET /api/v3/wanted/missing` from Sonarr, filtered to the subscription's `series_id`.
2. Flat-list the newest N videos from `source_url` (IDs + titles, no per-video fetch).
3. Match (below). Create a job per match; skip anything already queued/done for that target.

## Matching

Applied in order; the first strategy yielding exactly one candidate wins. Zero or several → fall through; if all fall through, the episode is skipped and shown as *unmatched* in the GUI so the user can add an override.

1. **Override** — `video_id → SxxExx`, set from the GUI. Always wins.
2. **Title** — normalise both sides (lowercase, strip punctuation, collapse whitespace, strip `Ep. 5` / `#5` / `Episode 5` prefixes); equality, then containment.
3. **Air date** — upload date within `date_tolerance_days` of `airDateUtc + date_offset_days`. Flat listings usually lack upload dates, so this fetches per-video info only for still-unmatched candidates and only if the strategy is enabled for the subscription.

Optional `title_regex` with named groups `season`/`episode` for channels that number their own uploads.

**Match preview** (GUI, dry run) shows the wanted episodes and what each strategy would pick, before anything is queued. This is the main defence against the title-drift problem prior art suffers from.

## Staging filename and quality

`{Series Title} - S{ss}E{ee} - {Episode Title} [WEBDL-{res}p].{ext}` for episodes, `{Movie Title} ({Year}) [WEBDL-{res}p].{ext}` for movies. Parseable as a safety net; not load-bearing.

Quality from downloaded height: ≥2160 → WEBDL-2160p, ≥1080 → WEBDL-1080p, ≥720 → WEBDL-720p, else WEBDL-480p. The target's quality profile must allow it or the import is rejected (and the GUI says so).

## GUI screens (v1)

| Screen | What it does |
|---|---|
| **Settings → Connections** | Add/edit Sonarr and Radarr: URL, API key, remote staging path. *Test* button (hits `/api/v3/system/status`, checks the reported `appName` matches the connection kind, and checks the staging path is visible via `/api/v3/filesystem`). `/filesystem` returns an empty listing for a missing directory, identical to an empty one, so the check lists the parent and looks for the staging directory in it. |
| **Settings → Downloads** | Scan interval, concurrency, default yt-dlp format, container, cookies file, extra yt-dlp options (JSON passthrough — one escape hatch instead of a setting per feature). |
| **Series** | Table of Sonarr's series pulled live, with a *subscribed* indicator. Subscribe → form: source URL, format override, strategies, tolerance/offset, regex. Detail view: wanted episodes, match preview, unmatched list with "set override", *Scan now*. |
| **Grab** | Paste a video or playlist URL → flat-resolve → table of videos. For each: pick a target (Sonarr series → season/episode picker, or Radarr movie search). Playlist helper: "start at S01E01 and number sequentially" bulk-fill, editable per row. *Queue* creates jobs. |
| **Activity** | Queue (progress bars), history, failed with error text and *Retry* / *Cancel*. |

Optional: tag subscribed series in Sonarr with an `outriggarr` tag so they are visible from Sonarr's side. Cheap, but not required — later.

## JSON API

The pages are thin; every action is a JSON endpoint so the UI can be replaced or scripted later without touching the worker.

```
GET/POST/PUT/DELETE  /api/connections            POST /api/connections/{id}/test
GET                  /api/connections/{id}/series (live from Sonarr)   /movies (live from Radarr)
GET/POST/PUT/DELETE  /api/subscriptions          POST /api/subscriptions/{id}/scan
GET                  /api/subscriptions/{id}/preview
PUT/DELETE           /api/subscriptions/{id}/overrides/{video_id}
POST                 /api/resolve  {url}  → list of videos (flat)
POST                 /api/jobs     [{target, video}]        GET /api/jobs?status=
POST                 /api/jobs/{id}/retry   /cancel
GET/PUT              /api/settings
```

## Components

| Module | Responsibility |
|---|---|
| `db/` | SQLAlchemy 2.x models + Alembic migrations. SQLite file in `/config`. |
| `arr/base.py` | `ArrClient` protocol: `status()`, `wanted(series_id=None)`, `quality_definitions()`, `path_visible(path)` (M1); `manual_import_candidates(folder)`, `manual_import(files)`, `command(id)` (M2). Errors raise `ArrError` whose message carries the request and the verbatim response body. |
| `arr/sonarr.py`, `arr/radarr.py` | Implementations over a shared `arr/common.py` HTTP base. Differences are confined here (`episodeIds` vs `movieId`, `wanted/missing` shapes). Sonarr v4 has no `seriesId` filter on `wanted/missing`, so the client pages the whole list and filters. |
| `source.py` | `VideoSource` protocol: `list_recent(url, limit)`, `resolve(url)` (video or playlist → videos), `fetch_info(video_id)`, `download(video_id, dest, opts, progress_cb)`. `YtDlpSource` implements it. |
| `matcher.py` | Pure functions over episodes + videos + overrides. No I/O. |
| `naming.py` | Staging filename + quality mapping. |
| `worker/scheduler.py` | Subscription scans → jobs. |
| `worker/runner.py` | Job state machine: download → import → cleanup; retry/backoff. |
| `api/` | FastAPI routers for the JSON API. |
| `web/` | Page routes + templates. |
| `main.py` | App factory; starts the worker as a background task; SIGTERM-clean. |

Dependency direction: `web → api → db/arr/source/matcher`; `worker → db/arr/source/matcher`. `web` never touches the worker directly — it writes rows, the worker picks them up.

## Frontend choice

| | Jinja2 + HTMX (+ Alpine.js for the picker) | React/Vite SPA |
|---|---|---|
| Build step | None — one Python container | Node build stage in the Dockerfile |
| Fit for these screens | Tables, forms, polling progress: HTMX's sweet spot | Better for the playlist-mapping grid if it grows complex |
| Cost | Server renders HTML; the JSON API still exists for scripting | Two codebases, API contract to keep in sync |

Recommendation: HTMX for v1 with a small CSS framework (Pico or similar, dark theme). The JSON API is the boundary, so swapping the front end later is a contained change. Revisit if the Grab screen's mapping grid becomes painful.

## Auth and exposure

No built-in auth in v1. Deploy behind NPM + Authentik like the other *arr companions on the stack.

Benefit/risk of the GUI vs v0.1's headless daemon: it adds one HTTP port on the Docker bridge. Exposure beyond that is the same as any *arr app — Sonarr/Radarr API keys sit in the SQLite file under `/config`, readable by anyone with that path. Keys can alternatively be supplied as env vars and the DB stores only a reference, if that matters. Nothing else listens; outbound is *arr APIs + YouTube.

## Failure handling

- **yt-dlp errors** (throttle, age gate, geo, extractor breakage): job → `failed` with the message, backoff 1h → 6h → 24h, capped attempts, other jobs continue. Extractor breakage is usually a yt-dlp release away: opt-in `pip install -U yt-dlp` on container start.
- **Import rejected** (quality not in profile, path not visible): staged file kept; rejection reasons shown on the job; *Retry* after fixing config.
- **Unmatched**: visible per subscription in the GUI; rescanned each interval (flat listing is cheap).
- **Idempotency**: job dedupe on `(connection, target, video)`; re-check the wanted list right before import so a file Sonarr got elsewhere is not double-imported.

## Deployment

- `python:3.12-slim` + ffmpeg. `PUID`/`PGID` so staged files are importable by the *arr user.
- Volumes: `/config` (DB, cookies), `/staging` (also mounted in Sonarr and Radarr from the same host path). Paths are overridable with `OUTRIGGARR_CONFIG_DIR` / `OUTRIGGARR_STAGING_DIR`; the DB URL with `OUTRIGGARR_DATABASE_URL`.
- One published port for the GUI (`OUTRIGGARR_PORT`, default 8080 inside the container). Same bridge as Sonarr/Radarr; reaches them by container name.
- Migrations run in-process at startup; the `alembic` CLI reads the same env vars.

## Stack

Python 3.12, FastAPI, SQLAlchemy 2.x + Alembic, `yt-dlp` (library), `httpx`, Jinja2 + HTMX. `pytest` with fake `ArrClient` and `VideoSource`; matcher and naming tested as pure functions.

## Open questions

1. ~~Name.~~ Outriggarr.
2. Actual host path for staging, and how it is mounted into Sonarr and Radarr.
3. Are the target series already on TVDB with full episode lists? If not, that is the first blocker, not code.
4. HTMX vs React — accept the recommendation, or is there a preference?
5. Sonarr tag on subscribed series — wanted in v1 or later?
6. SponsorBlock segment removal as a default (`extra_opts`), given iSponsorBlockTV already runs on the playback side?

## Build order

Each milestone is independently useful and ends with a confirmation step. The integration risk is in M2, so it comes before any UI.

| # | Milestone | Done when |
|---|---|---|
| M0 | Skeleton | Repo layout per CLAUDE.md; FastAPI boots; DB + first Alembic migration (`connection`, `setting`, `job`); `/health`; Dockerfile builds and runs. |
| M1 | Connections | `ArrClient` protocol; Sonarr and Radarr `status()`, `quality_definitions()`, `wanted()`; connections CRUD + *Test* over the JSON API against the real instances. |
| M2 | Job pipeline, headless | Runner: download → manual import → cleanup, with retry/backoff. Proven by posting one job for a real wanted Sonarr episode via the JSON API and seeing the file land in the library, renamed by Sonarr, with the staging folder empty. Same for one Radarr movie. |
| M3 | Grab + Activity screens | Paste URL/playlist → resolve → target picker (series/season/episode, movie search) → bulk-fill → queue. Activity with progress, retry, cancel. |
| M4 | Subscriptions | `matcher.py` (pure, tested); scheduler; overrides; Series screen with match preview and unmatched list; `Scan now`. |
| M5 | Operational polish | PUID/PGID; cookies file; yt-dlp extra opts; opt-in yt-dlp self-update; optional Sonarr tag; README. |

Answer the open questions above before M1 (real staging mount) and before M3 (HTMX vs React). Nothing in "Later" gets built without a design change first.
